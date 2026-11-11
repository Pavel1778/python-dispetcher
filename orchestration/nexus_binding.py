"""D-2 project execution binding: repository links and dispatch authorization.

This module owns two things:

1. :class:`ProjectRepositoryService` — the explicit many-to-many link between a
   project and a repository. A repository may be shared by several projects; a
   project may use several repositories. This link is what authorizes a project
   to run work against a repository.

2. :func:`authorize_dispatch` — the single authorization gate used by the
   dispatcher, the web API, the CLI and MCP before any conversation is started.
   It re-checks the *current* state of project, task, workflow, repository and
   branch. A global repository allowlist alone is never treated as project
   authorization.

Legacy tasks with no ``project_id`` keep working exactly as before, but the
legacy path can never be used to reach a *different* project's repository: a
project-scoped task must use a linked repository, and a repository used by a
project-scoped task is checked against that project, not the global allowlist.
"""

from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore, StoreError, Task
from .nexus_projects import _text


class AuthorizationError(StoreError):
    """A dispatch was refused by the project/repository authorization gate."""


class ProjectRepositoryService:
    """Project ↔ repository links (many-to-many)."""

    def __init__(self, store: PostgresStore):
        self.store = store

    def link(self, principal: str, project_id: str, repository_full_name: str) -> dict:
        """Link an allowlisted repository to a project the principal owns.

        The repository must exist in the global allowlist (so a project cannot
        bind a name that was never discovered/approved). The project must exist
        and the principal must be its owner.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s AND role='owner'",
                (project_id, principal),
            )
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute("SELECT id, full_name, default_branch, enabled "
                        "FROM repositories WHERE full_name=%s", (repository_full_name,))
            repo = cur.fetchone()
            if repo is None:
                raise NotFound("repository not in the allowlist")
            cur.execute(
                "INSERT INTO nexus_project_repositories"
                "(project_id, repository_id, created_by) VALUES (%s,%s,%s) "
                "ON CONFLICT (project_id, repository_id) DO NOTHING "
                "RETURNING project_id, repository_id, created_at",
                (project_id, repo["id"], principal),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT project_id, repository_id, created_at "
                            "FROM nexus_project_repositories "
                            "WHERE project_id=%s AND repository_id=%s",
                            (project_id, repo["id"]))
                row = cur.fetchone()
            self._event(cur, project_id, principal, "project.repository_linked",
                        repo["id"])
            out = dict(row)
            out["full_name"] = repo["full_name"]
            out["default_branch"] = repo["default_branch"]
            out["enabled"] = repo["enabled"]
            return out

    def unlink(self, principal: str, project_id: str, repository_full_name: str) -> bool:
        """Remove a link. Fails while a non-terminal task still uses it.

        Unlinking a repository out from under a running task would let that task
        keep editing a checkout the project no longer owns. Refuse until the work
        is terminal; the operator can cancel the task first.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s AND role='owner'",
                (project_id, principal),
            )
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute("SELECT id FROM repositories WHERE full_name=%s",
                        (repository_full_name,))
            repo = cur.fetchone()
            if repo is None:
                raise NotFound("repository not linked to this project")
            cur.execute(
                "SELECT 1 FROM tasks WHERE project_id=%s AND repository_id=%s "
                "AND state IN ('queued','assigned','running','verifying','recovering',"
                "'blocked') LIMIT 1",
                (project_id, repo["id"]),
            )
            if cur.fetchone() is not None:
                raise AuthorizationError(
                    "repository has in-flight tasks in this project; cancel them first")
            cur.execute(
                "DELETE FROM nexus_project_repositories "
                "WHERE project_id=%s AND repository_id=%s",
                (project_id, repo["id"]),
            )
            removed = cur.rowcount > 0
            if removed:
                self._event(cur, project_id, principal,
                            "project.repository_unlinked", repo["id"])
            return removed

    def list(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s", (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute(
                "SELECT r.id, r.full_name, r.provider, r.default_branch, "
                "       r.allowed_branches, r.enabled, pr.created_at AS linked_at "
                "FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "WHERE pr.project_id=%s ORDER BY r.full_name",
                (project_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def linked(self, project_id: str, repository_full_name: str) -> bool:
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "WHERE pr.project_id=%s AND r.full_name=%s",
                (project_id, repository_full_name),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))


def _membership_ok(cur: Any, project_id: str, principal: str) -> bool:
    cur.execute("SELECT 1 FROM nexus_project_members WHERE project_id=%s AND principal=%s",
                (project_id, principal))
    return cur.fetchone() is not None


def repository_refusal(cur: Any, project_id: str | None, repository: str,
                       branch: str | None) -> str | None:
    """Why work may not run against *repository* / *branch*; ``None`` if it may.

    The one statement of the repository rule, shared by every path that starts
    a conversation: the repository is in the allowlist, it is linked to the
    project (when the work is project-scoped), it is enabled, and the branch is
    one the allowlist permits (an empty list there means "default branch only").
    """
    cur.execute(
        "SELECT id, full_name, default_branch, allowed_branches, enabled "
        "FROM repositories WHERE full_name=%s", (repository,))
    repo_row = cur.fetchone()
    if repo_row is None:
        return f"repository {repository} is not in the allowlist"
    if project_id is not None:
        cur.execute(
            "SELECT 1 FROM nexus_project_repositories pr "
            "WHERE pr.project_id=%s AND pr.repository_id=%s",
            (project_id, repo_row["id"]))
        if cur.fetchone() is None:
            return f"repository {repository} is not linked to project {project_id}"
    if not repo_row["enabled"]:
        return f"repository {repository} is disabled"
    if branch:
        allowed = repo_row["allowed_branches"] or []
        if allowed and branch not in allowed:
            return f"branch {branch} is not allowed"
        if not allowed and branch != repo_row["default_branch"]:
            return f"branch {branch} is not allowed (default branch only)"
    return None


def authorize_run_task(store: PostgresStore, *, project_id: str, repository: str,
                       branch: str) -> None:
    """The dispatch gate for the autonomous lab. Raises :class:`AuthorizationError`.

    A run task lives in ``nexus_run_tasks``, not in ``tasks``, so it cannot go
    through :func:`authorize_dispatch`; the repository rule is the same one.
    Called when a run is started (so a refusal reaches the person asking) and
    again immediately before each conversation is created (so a link removed or
    a repository disabled in between still stops the work).

    A task with no repository has nothing to authorize, exactly as a legacy task
    with no ``selected_repository``.
    """
    with store._tx() as cur:
        cur.execute("SELECT 1 FROM nexus_projects WHERE id=%s", (project_id,))
        if cur.fetchone() is None:
            raise AuthorizationError("the run's project no longer exists")
        if repository:
            refusal = repository_refusal(cur, project_id, repository, branch or None)
            if refusal:
                raise AuthorizationError(refusal)


def authorize_dispatch(store: PostgresStore, *, task: Task,
                       principal: str | None = None) -> dict[str, Any]:
    """Re-check every dispatch precondition for *task*. Raise on any failure.

    *principal* is ``None`` for the system dispatcher. When a principal is given
    (web / CLI / MCP), membership in the task's project is also required.

    Returns a small context dict (project_id, repository, branch) for logging.
    The checks, in order:

    1. The project exists (when the task is project-scoped).
    2. The task belongs to that project (the task's own project_id).
    3. The workflow, when present, is in the same project.
    4. The repository is explicitly linked to the project (project-scoped tasks).
    5. The repository is enabled.
    6. The branch is permitted.
    7. The principal has project rights (when a principal is given).
    """
    with store._tx() as cur:
        project_id = str(task.project_id) if getattr(task, "project_id", None) else None

        # 1 + 2. project exists and the task is in it.
        if project_id is not None:
            cur.execute("SELECT id FROM nexus_projects WHERE id=%s", (project_id,))
            if cur.fetchone() is None:
                raise AuthorizationError("task project no longer exists")
            cur.execute("SELECT project_id FROM tasks WHERE id=%s", (task.id,))
            row = cur.fetchone()
            if row is None:
                raise AuthorizationError("task no longer exists")
            if (str(row["project_id"]) if row["project_id"] else None) != project_id:
                raise AuthorizationError("task project changed since it was queued")

        # 3. workflow shares the project.
        if task.workflow_id is not None:
            cur.execute("SELECT project_id FROM workflows WHERE id=%s", (task.workflow_id,))
            wrow = cur.fetchone()
            if wrow is not None:
                wf_project = str(wrow["project_id"]) if wrow["project_id"] else None
                if project_id is not None and wf_project is not None and wf_project != project_id:
                    raise AuthorizationError("task and workflow are in different projects")

        # 4-6. repository authorization.
        repo_full = task.selected_repository
        if repo_full is not None:
            refusal = repository_refusal(cur, project_id, repo_full, task.selected_branch)
            if refusal:
                raise AuthorizationError(refusal)

        # 7. principal rights.
        if principal is not None and project_id is not None:
            if not _membership_ok(cur, project_id, principal):
                raise AuthorizationError("principal is not a member of the task project")

        return {"project_id": project_id, "repository": repo_full,
                "branch": task.selected_branch}


class ProjectBindingError(StoreError):
    """A project binding change was refused."""


class ProjectBindingService:
    """The project's explicit execution binding: repository, branch, account.

    A binding is changed only through :meth:`set_repository`, never as a side
    effect of another action, so a project cannot silently switch the repository
    or branch work runs against. The repository is resolved from the allowlist by
    stable id (or by full name for the advanced manual path) and must be
    linkable; the branch must be one the repository actually has (checked by the
    caller that holds a GitHub client) or the default.
    """

    def __init__(self, store: PostgresStore):
        self.store = store

    def get(self, principal: str, project_id: str) -> dict:
        with self.store._tx() as cur:
            cur.execute(
                "SELECT p.id, p.name, p.repository_id, p.selected_branch, "
                "       p.openhands_account_id, p.github_account_id, "
                "       r.full_name, r.provider, r.default_branch, r.allowed_branches, "
                "       r.enabled "
                "FROM nexus_projects p "
                "LEFT JOIN repositories r ON r.id = p.repository_id "
                "JOIN nexus_project_members m ON m.project_id = p.id "
                "WHERE p.id=%s AND m.principal=%s",
                (project_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            return dict(row)

    def set_repository(self, principal: str, project_id: str, *,
                       repository_id: str | None = None,
                       full_name: str | None = None,
                       branch: str | None = None) -> dict:
        """Bind a repository (and branch) to a project. Explicit, never implicit.

        Exactly one of *repository_id* / *full_name* selects the repository; a
        ``None`` repository_id with no full_name unbinds. When a repository is
        given it is also linked to the project (the link is what authorizes
        dispatch), so binding and authorizing cannot drift apart.
        """
        if (repository_id is None) == (full_name is None):
            raise ValueError("provide exactly one of repository_id or full_name")
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_project_members "
                        "WHERE project_id=%s AND principal=%s AND role='owner'",
                        (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            repo = None
            if repository_id is not None:
                cur.execute("SELECT id, full_name, default_branch, allowed_branches, enabled "
                            "FROM repositories WHERE id=%s", (repository_id,))
                repo = cur.fetchone()
            elif full_name is not None:
                cur.execute("SELECT id, full_name, default_branch, allowed_branches, enabled "
                            "FROM repositories WHERE full_name=%s", (full_name.strip(),))
                repo = cur.fetchone()
            if repo is None:
                raise NotFound("repository not in the allowlist")
            if not repo["enabled"]:
                raise ProjectBindingError("repository is disabled")
            chosen = (branch or repo["default_branch"]).strip()
            if not chosen:
                raise ProjectBindingError("branch is required")
            allowed = repo["allowed_branches"] or []
            if allowed and chosen not in allowed:
                raise ProjectBindingError(
                    f"branch {chosen} is not in the repository's allowed branches")
            cur.execute(
                "INSERT INTO nexus_project_repositories(project_id, repository_id, created_by) "
                "VALUES (%s,%s,%s) ON CONFLICT (project_id, repository_id) DO NOTHING",
                (project_id, repo["id"], principal))
            cur.execute(
                "UPDATE nexus_projects SET repository_id=%s, selected_branch=%s, "
                "updated_at=now() WHERE id=%s "
                "RETURNING id, name, repository_id, selected_branch",
                (repo["id"], chosen, project_id))
            out = dict(cur.fetchone())
            out["full_name"] = repo["full_name"]
            out["default_branch"] = repo["default_branch"]
            cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                        "VALUES (%s,%s,'project.repository_bound',%s)",
                        (project_id, principal, repo["id"]))
            return out

    def set_branch(self, principal: str, project_id: str, branch: str) -> dict:
        """Change only the selected branch of an already-bound repository."""
        branch = _text(branch, "branch", maximum=255)
        with self.store._tx() as cur:
            cur.execute(
                "SELECT p.repository_id, r.allowed_branches, r.default_branch "
                "FROM nexus_projects p JOIN repositories r ON r.id = p.repository_id "
                "JOIN nexus_project_members m ON m.project_id = p.id "
                "WHERE p.id=%s AND m.principal=%s AND m.role='owner'",
                (project_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project has no repository bound")
            allowed = row["allowed_branches"] or []
            if allowed and branch not in allowed:
                raise ProjectBindingError(
                    f"branch {branch} is not in the repository's allowed branches")
            cur.execute("UPDATE nexus_projects SET selected_branch=%s, updated_at=now() "
                        "WHERE id=%s RETURNING id, repository_id, selected_branch",
                        (branch, project_id))
            out = dict(cur.fetchone())
            cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                        "VALUES (%s,%s,'project.branch_changed',%s)",
                        (project_id, principal, row["repository_id"]))
            return out
