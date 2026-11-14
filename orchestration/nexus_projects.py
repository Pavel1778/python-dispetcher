"""NEXUS project foundation, intentionally separate from legacy execution.

Authorization is part of every query, not an ID-only preflight. The existing
single-user authentication supplies the principal; no user-controlled project
or principal is trusted. Draft goals do not start workflows or agents.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_policy import SCOPE_PROJECT_ADMIN, PermissionDenied, require


def _active_run_states() -> list[str]:
    """The run states that count as "still at work". Imported lazily:
    ``nexus_lab_store`` imports this module, so a top-level import here would be
    circular."""
    from .nexus_lab_store import ACTIVE_RUN_STATES

    return list(ACTIVE_RUN_STATES)


class IdempotencyConflict(ValueError):
    """The same project's request key was reused with a different goal."""


class ProjectInUse(RuntimeError):
    """A project cannot be deleted or purged while it has an active run."""


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
        """The principal's projects, newest first.

        A project that was archived is left out unless *include_archived*; one
        that is in the trash (``deleted_at``) is left out unless *include_deleted*.
        """
        with self.store._tx() as cur:
            sql = ("SELECT p.id, p.name, p.description, p.archived_at, p.deleted_at, "
                   "  p.created_at, p.updated_at "
                   "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                   "WHERE m.principal=%s")
            if not include_deleted:
                sql += " AND p.deleted_at IS NULL"
            if not include_archived:
                sql += " AND p.archived_at IS NULL"
            sql += " ORDER BY p.created_at DESC, p.id"
            cur.execute(sql, (principal,))
            return [dict(row) for row in cur.fetchall()]

    def get(self, principal: str, project_id: str) -> dict:
        with self.store._tx() as cur:
            return self._project(cur, principal, project_id)

    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> dict:
        # A project in the trash reads as "not found" everywhere, so a deleted
        # project is invisible to every member operation without each service
        # having to remember to check.
        cur.execute("SELECT p.id, p.name, p.description, p.created_at, p.updated_at "
                    "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                    "WHERE p.id=%s AND m.principal=%s AND p.deleted_at IS NULL "
                    "FOR KEY SHARE OF m", (project_id, principal))
        row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        return dict(row)

    # ------------------------------------------------------- lifecycle
    def stats(self, principal: str, project_id: str) -> dict:
        """What is inside a project, for the deletion confirmation.

        Names only what the operator needs to decide: how many conversations,
        runs, tasks and goals, whether any run is still active, and which
        repositories are bound. Reading it changes nothing. A project in the
        trash is still readable by a member (``include_deleted``), so the trash
        can show what it holds.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT id, name, archived_at, deleted_at FROM nexus_projects p "
                        "JOIN nexus_project_members m ON m.project_id=p.id "
                        "WHERE p.id=%s AND m.principal=%s", (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")

            def count(sql: str) -> int:
                cur.execute(sql, (project_id,))
                return int(cur.fetchone()["n"])

            active_runs = count(
                "SELECT count(*) AS n FROM nexus_runs WHERE project_id=%s "
                "AND state = ANY(%s)")
            repositories = []
            cur.execute("SELECT r.full_name, pr.branch, pr.enabled "
                        "FROM nexus_project_repositories pr "
                        "JOIN repositories r ON r.id = pr.repository_id "
                        "WHERE pr.project_id=%s ORDER BY r.full_name", (project_id,))
            repositories = [{"full_name": r["full_name"], "branch": r["branch"],
                             "enabled": r["enabled"]} for r in cur.fetchall()]
            return {
                "conversations": count("SELECT count(*) AS n FROM nexus_conversations "
                                       "WHERE project_id=%s"),
                "runs": count("SELECT count(*) AS n FROM nexus_runs WHERE project_id=%s"),
                "active_runs": active_runs,
                "tasks": count("SELECT count(*) AS n FROM nexus_run_tasks WHERE project_id=%s"),
                "goals": count("SELECT count(*) AS n FROM nexus_goals WHERE project_id=%s"),
                "members": count("SELECT count(*) AS n FROM nexus_project_members "
                                 "WHERE project_id=%s"),
                "repositories": repositories,
                "has_active_runs": active_runs > 0,
            }

    def archive(self, principal: str, project_id: str, archived: bool = True) -> dict:
        """Take a project out of the active list, or put it back. Nothing is
        deleted; an archived project stays fully usable."""
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute(
                "UPDATE nexus_projects SET archived_at = CASE WHEN %s THEN now() ELSE NULL END, "
                "updated_at=now() WHERE id=%s "
                "RETURNING id, name, description, archived_at, deleted_at, created_at, updated_at",
                (bool(archived), project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            self._event(cur, project_id, principal,
                        "project.archived" if archived else "project.restored", project_id)
            return dict(row)

    def delete(self, principal: str, project_id: str) -> dict:
        """Move a project to the trash (soft delete). Reversible.

        Refused while any run of the project is still active: a run that is at
        work must be stopped or allowed to finish first, so its conversations
        are not left running against a project nobody can see. Nothing is
        removed from the database - ``restore`` brings it back, and
        ``purge`` is the separate, explicit, irreversible step.
        """
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute("SELECT id FROM nexus_projects WHERE id=%s AND deleted_at IS NULL",
                        (project_id,))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute("SELECT count(*) AS n FROM nexus_runs WHERE project_id=%s "
                        "AND state = ANY(%s)", (project_id, _active_run_states()))
            active = int(cur.fetchone()["n"])
            if active:
                raise ProjectInUse(
                    f"the project has {active} active run(s); stop them before deleting")
            cur.execute("UPDATE nexus_projects SET deleted_at=now(), archived_at=NULL, "
                        "updated_at=now() WHERE id=%s "
                        "RETURNING id, name, description, archived_at, deleted_at, "
                        "created_at, updated_at", (project_id,))
            row = cur.fetchone()
            self._event(cur, project_id, principal, "project.deleted", project_id)
            return dict(row)

    def restore(self, principal: str, project_id: str) -> dict:
        """Bring a project back from the trash."""
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute("UPDATE nexus_projects SET deleted_at=NULL, updated_at=now() "
                        "WHERE id=%s AND deleted_at IS NOT NULL "
                        "RETURNING id, name, description, archived_at, deleted_at, "
                        "created_at, updated_at", (project_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found in the trash")
            self._event(cur, project_id, principal, "project.restored", project_id)
            return dict(row)

    def purge(self, principal: str, project_id: str, *, confirm: bool) -> dict:
        """Remove a project and everything inside it for good. Irreversible.

        Refused without ``confirm`` and refused while a run is active. Only
        rows inside this project are removed; the external GitHub repositories
        it was bound to are never touched (the links go, the repositories do
        not). The rows that reference the project ``ON DELETE SET NULL``
        (evidence, legacy tasks) keep their content and lose the link, so no
        other project is affected.
        """
        if not confirm:
            raise ValueError("purging a project requires explicit confirmation")
        with self.store._tx() as cur:
            self.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            cur.execute("SELECT name FROM nexus_projects WHERE id=%s", (project_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            name = row["name"]
            cur.execute("SELECT count(*) AS n FROM nexus_runs WHERE project_id=%s "
                        "AND state = ANY(%s)", (project_id, _active_run_states()))
            active = int(cur.fetchone()["n"])
            if active:
                raise ProjectInUse(
                    f"the project has {active} active run(s); stop them before purging")
            # Counted before the delete, for the report: after it they are gone.
            counts = {}
            for table in ("nexus_conversations", "nexus_runs", "nexus_run_tasks",
                          "nexus_goals", "nexus_specs"):
                cur.execute(f"SELECT count(*) AS n FROM {table} WHERE project_id=%s",
                            (project_id,))
                counts[table] = int(cur.fetchone()["n"])
            cur.execute("DELETE FROM nexus_projects WHERE id=%s", (project_id,))
            return {"purged": True, "name": name, "removed": counts}

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
