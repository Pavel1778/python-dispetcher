"""NEXUS project foundation, intentionally separate from legacy execution.

Authorization is part of every query, not an ID-only preflight. The existing
single-user authentication supplies the principal; no user-controlled project
or principal is trusted. Draft goals do not start workflows or agents.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_policy import SCOPE_PROJECT_ADMIN, PermissionDenied, require


class IdempotencyConflict(ValueError):
    """The same project's request key was reused with a different goal."""


def _text(value: Any, field: str, *, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if "\x00" in result:
        raise ValueError(f"{field} contains an invalid character")
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def principal_for_session(username: str | None) -> str:
    """A bearer acts as the configured owner; a session uses its username.

    Without a configured owner, the legacy machine token has its own namespace.
    The caller never gets to supply this identity in an HTTP request.
    """
    return f"user:{username}" if username else "machine:default"


class ProjectService:
    def __init__(self, store: PostgresStore):
        self.store = store

    def create(self, principal: str, name: Any, description: Any = "") -> dict:
        name = _text(name, "name", maximum=120)
        description = _text(description, "description", maximum=4000, required=False)
        with self.store._tx() as cur:
            cur.execute("INSERT INTO nexus_projects(name, description) VALUES (%s, %s) "
                        "RETURNING id, name, description, created_at, updated_at", (name, description))
            project = dict(cur.fetchone())
            cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                        "VALUES (%s, %s, 'owner')", (project["id"], principal))
            self._event(cur, project["id"], principal, "project.created", project["id"])
        return project

    def list(self, principal: str, *, include_archived: bool = False,
             include_deleted: bool = False) -> list[dict]:
        """The caller's projects, newest first.

        An archived or deleted project is left out by default, so the ordinary
        picker does not have to scroll past ones nobody works in any more;
        either flag brings them back for an archive/trash view. ``deleted`` is
        the stronger state: once a project is in the trash, ``include_deleted``
        alone is enough to see it (whether or not it is also archived).
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT p.id, p.name, p.description, p.archived_at, p.deleted_at, "
                "  p.created_at, p.updated_at "
                "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                "WHERE m.principal=%s AND ("
                "  (p.deleted_at IS NULL AND (p.archived_at IS NULL OR %s))"
                "  OR (p.deleted_at IS NOT NULL AND %s)) "
                "ORDER BY p.created_at DESC, p.id",
                (principal, include_archived, include_deleted))
            return [dict(row) for row in cur.fetchall()]

    def get(self, principal: str, project_id: str) -> dict:
        with self.store._tx() as cur:
            return self._project(cur, principal, project_id)

    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> dict:
        cur.execute("SELECT p.id, p.name, p.description, p.archived_at, p.deleted_at, "
                    "  p.created_at, p.updated_at "
                    "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                    "WHERE p.id=%s AND m.principal=%s FOR KEY SHARE OF m", (project_id, principal))
        row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        return dict(row)

    @staticmethod
    def _refuse_if_deleted(cur: Any, project_id: str) -> None:
        """Refuse *new* work in a project that is in the trash.

        Called where new activity would start (a new conversation, a new
        goal): a deleted project is inert, not merely hidden, so it cannot
        grow new history while nobody is looking at it. An *archived* project
        has no such restriction - archiving only changes what the default list
        shows, exactly like archiving a conversation.
        """
        cur.execute("SELECT deleted_at FROM nexus_projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if row is not None and row["deleted_at"] is not None:
            raise ValueError("project is deleted; restore it before adding new work")

    #: Run states that count as "this project has something active in it" for
    #: the purposes of refusing a delete. Mirrors ``ACTIVE_RUN_STATES`` in the
    #: lab store; kept as a plain tuple to avoid a dependency from the project
    #: layer onto the lab execution layer for one constant.
    _ACTIVE_RUN_STATES = ("running", "paused", "waiting_for_approval", "blocked")

    def _active_runs(self, cur: Any, project_id: str) -> int:
        cur.execute("SELECT count(*) AS n FROM nexus_runs "
                    "WHERE project_id=%s AND state = ANY(%s)",
                    (project_id, list(self._ACTIVE_RUN_STATES)))
        return cur.fetchone()["n"]

    def set_archived(self, principal: str, project_id: str, archived: bool) -> dict:
        """Archive or restore a project. Reversible; never touches its data.

        An archived project drops out of the default list but is otherwise
        untouched: its conversations, runs and repository links are not
        affected, and it can still be opened and worked in. This mirrors the
        conversation archive, so the two behave the same way.
        """
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute(
                "UPDATE nexus_projects SET archived_at=CASE WHEN %s THEN now() ELSE NULL END "
                "WHERE id=%s "
                "RETURNING id, name, description, archived_at, deleted_at, created_at, updated_at",
                (bool(archived), project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            self._event(cur, project_id, principal,
                        "project.archived" if archived else "project.unarchived", project_id)
            return dict(row)

    def delete(self, principal: str, project_id: str) -> dict:
        """Soft-delete a project: move it to the trash, nothing is destroyed.

        Refused while the project has an active run - the agent working on it
        would otherwise be cut off mid-step with nothing to report. The caller
        cancels those runs first (a separate, explicit action), so a delete can
        never silently stop work the user did not ask to stop. Nothing
        cascades: the project row and all its data stay, ``deleted_at`` is a
        marker, and no other project or external repository is touched. A
        repeat delete is idempotent.
        """
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            active = self._active_runs(cur, project_id)
            if active:
                raise ValueError(
                    f"project has {active} active run(s); cancel them before deleting")
            cur.execute(
                "UPDATE nexus_projects SET deleted_at=now() WHERE id=%s AND deleted_at IS NULL "
                "RETURNING id, name, description, archived_at, deleted_at, created_at, updated_at",
                (project_id,))
            row = cur.fetchone()
            if row is None:
                # Either missing, or already deleted; re-read to tell which, so
                # a double delete stays idempotent rather than raising.
                return self._project(cur, principal, project_id)
            self._event(cur, project_id, principal, "project.deleted", project_id)
            return dict(row)

    def restore(self, principal: str, project_id: str) -> dict:
        """Undo :meth:`delete`: clear ``deleted_at``. Idempotent."""
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute(
                "UPDATE nexus_projects SET deleted_at=NULL WHERE id=%s "
                "RETURNING id, name, description, archived_at, deleted_at, created_at, updated_at",
                (project_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            self._event(cur, project_id, principal, "project.restored", project_id)
            return dict(row)

    #: Every project-scoped table, for the delete census. Each has a
    #: ``project_id`` column; the ones with ``ON DELETE CASCADE`` go with the
    #: project, the ``SET NULL`` ones (a run's ``conversation_id``, a spec's) are
    #: not project-scoped and are not listed here.
    _PROJECT_TABLES = (
        "nexus_conversations", "nexus_goals", "nexus_plans", "nexus_files",
        "nexus_workspaces", "nexus_voice_notes", "nexus_project_context",
        "nexus_memory", "nexus_runs", "nexus_run_tasks", "nexus_evidence",
        "nexus_specs", "nexus_notifications", "nexus_project_events",
        "nexus_project_repositories", "nexus_project_executors",
        "nexus_project_members",
    )

    def _census(self, cur: Any, project_id: str) -> dict:
        """Row counts a permanent delete would remove, taken on *cur*.

        Shared by :meth:`dependents` (a read) and :meth:`delete_permanently` (so
        the count and the delete are one transaction), which is why it takes the
        cursor rather than opening its own.
        """
        counts: dict[str, int] = {}
        for table in self._PROJECT_TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE project_id=%s",
                        (project_id,))
            counts[table] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM nexus_runs "
                    "WHERE project_id=%s AND state = ANY(%s)",
                    (project_id, list(self._ACTIVE_RUN_STATES)))
        counts["active_runs"] = cur.fetchone()["n"]
        return counts

    def dependents(self, principal: str, project_id: str) -> dict:
        """What a permanent delete of this project would take with it.

        A read-only census, so a destructive action can show what is at stake
        before it is confirmed. ``active_runs`` is the count that blocks the
        delete until the runs are cancelled.
        """
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            return self._census(cur, project_id)

    def delete_permanently(self, principal: str, project_id: str) -> dict:
        """Remove a project and everything that belongs to it, for good.

        The second, explicit step after :meth:`delete` (the trash), so a single
        click can never destroy a project. It is refused while the project has
        an active run - cancel first. Everything project-scoped that references
        the project with ``ON DELETE CASCADE`` goes with it: conversations and
        their messages, goals, specs, plans, files, workspaces, runs and their
        tasks, evidence, memory, notifications and events. Nothing outside the
        project is touched: repositories (the GitHub side), GitHub and OpenHands
        accounts, and the users themselves are global and stay.

        Returns ``{"deleted": id, "dependents": {...}}`` where ``dependents`` is
        the census taken in the same transaction, so the caller records exactly
        what was removed.
        """
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute("SELECT deleted_at FROM nexus_projects WHERE id=%s FOR UPDATE",
                        (project_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            if row["deleted_at"] is None:
                raise ValueError(
                    "move the project to the trash first, then delete it for good")
            active = self._active_runs(cur, project_id)
            if active:
                raise ValueError(
                    f"project has {active} active run(s); cancel them before deleting")
            census = self._census(cur, project_id)
            cur.execute("DELETE FROM nexus_projects WHERE id=%s", (project_id,))
            return {"deleted": project_id, "dependents": census}

    @staticmethod
    def role(cur: Any, principal: str, project_id: str) -> str:
        """The caller's role in a project, or ``NotFound`` if not a member."""
        cur.execute("SELECT role FROM nexus_project_members "
                    "WHERE project_id=%s AND principal=%s", (project_id, principal))
        row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        return row["role"]

    @classmethod
    def require_scope(cls, cur: Any, principal: str, project_id: str,
                      scope: str) -> str:
        """Refuse unless the caller's role in *project_id* carries *scope*.

        Membership is checked first, so a non-member gets ``NotFound`` (never a
        disclosure that the project exists), and only then is the role's scope
        consulted. This is the one RBAC gate: a service asks for a scope, it
        never hard-codes "is this person an owner".
        """
        role = cls.role(cur, principal, project_id)
        try:
            require(role, scope)
        except PermissionDenied:
            raise NotFound("project not found") from None
        return role

    def update(self, principal: str, project_id: str, changes: dict) -> dict:
        if not changes or set(changes) - {"name", "description"}:
            raise ValueError("provide name and/or description")
        name = _text(changes["name"], "name", maximum=120) if "name" in changes else None
        description = (_text(changes["description"], "description", maximum=4000, required=False)
                       if "description" in changes else None)
        with self.store._tx() as cur:
            # Only an owner may change what a project *is*.
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute("UPDATE nexus_projects p SET name=COALESCE(%s, p.name), "
                        "description=COALESCE(%s, p.description), updated_at=now() "
                        "WHERE p.id=%s "
                        "RETURNING p.id, p.name, p.description, p.created_at, p.updated_at",
                        (name, description, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            self._event(cur, project_id, principal, "project.updated", project_id)
            return dict(row)

    def create_goal(self, principal: str, project_id: str, text: Any,
                    key: str | None = None) -> tuple[dict, bool]:
        text = _text(text, "text", maximum=20000)
        if key is not None:
            key = _text(key, "Idempotency-Key", maximum=200)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            self._refuse_if_deleted(cur, project_id)
            cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, idempotency_key) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (project_id, idempotency_key) "
                        "DO NOTHING RETURNING id, project_id, text, state, created_by, created_at",
                        (project_id, text, principal, key))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT id, project_id, text, state, created_by, created_at "
                            "FROM nexus_goals WHERE project_id=%s AND idempotency_key=%s",
                            (project_id, key))
                row = cur.fetchone()
                if row["text"] != text or row["created_by"] != principal:
                    raise IdempotencyConflict("idempotency key already used for a different request")
                return dict(row), False
            self._event(cur, project_id, principal, "goal.created", row["id"])
            return dict(row), True

    def get_goal(self, principal: str, project_id: str, goal_id: str) -> dict:
        """One goal of a project the caller is a member of, or ``NotFound``."""
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT id, project_id, text, state, created_by, created_at, spec_id "
                        "FROM nexus_goals WHERE id=%s AND project_id=%s", (goal_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("goal not found")
            return dict(row)

    def list_goals(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            # ``text`` is the goal's label. A goal whose task is longer than the
            # column holds (20 000 characters) carries the beginning of it here;
            # the task itself is the specification ``spec_id`` points at.
            cur.execute("SELECT id, project_id, text, state, created_by, created_at, spec_id "
                        "FROM nexus_goals WHERE project_id=%s ORDER BY created_at DESC, id LIMIT 200",
                        (project_id,))
            return [dict(row) for row in cur.fetchall()]

    def events(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT seq, project_id, actor, kind, resource_id, created_at "
                        "FROM nexus_project_events WHERE project_id=%s ORDER BY seq DESC LIMIT 200",
                        (project_id,))
            return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))
