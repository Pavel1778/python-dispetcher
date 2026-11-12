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
    """Project ↔ repository links (many-to-many).

    A project may use **several** repositories, each read with its own connected
    GitHub account (``github_account_id`` on the link). One of them may be the
    project's **primary** repository - the one a task that names no repository
    runs against, and the one shown first. The global allowlist is still
    required (a project cannot bind a name that was never discovered), but it is
    never sufficient: only an explicit link authorizes work.
    """

    def __init__(self, store: PostgresStore):
        self.store = store

    def link(self, principal: str, project_id: str, repository_full_name: str, *,
             account_id: str | None = None, label: str = "",
             make_primary: bool = False) -> dict:
        """Link an allowlisted repository to a project the principal owns.

        The repository must exist in the global allowlist (so a project cannot
        bind a name that was never discovered/approved). The project must exist
        and the principal must be its owner.

        *account_id*, when given, is the connected GitHub account whose token
        reads this repository. It must belong to the same principal: a project
        cannot be pointed at another principal's account. A link without an
        account is read with the server token, as before. *make_primary* makes
        this the project's primary repository (clearing any previous primary).
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
            if account_id is not None:
                cur.execute("SELECT 1 FROM nexus_github_accounts "
                            "WHERE id=%s AND principal=%s", (account_id, principal))
                if cur.fetchone() is None:
                    raise NotFound("GitHub account not found")
            if make_primary:
                cur.execute("UPDATE nexus_project_repositories SET is_primary=false "
                            "WHERE project_id=%s AND is_primary", (project_id,))
            cur.execute(
                "INSERT INTO nexus_project_repositories"
                "(project_id, repository_id, created_by, github_account_id, label, "
                " is_primary) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (project_id, repository_id) DO UPDATE SET "
                "  github_account_id=EXCLUDED.github_account_id, label=EXCLUDED.label, "
                "  is_primary=EXCLUDED.is_primary "
                "RETURNING project_id, repository_id, created_at, github_account_id, "
                "  label, is_primary",
                (project_id, repo["id"], principal, account_id, label[:120], make_primary),
            )
            row = cur.fetchone()
            self._event(cur, project_id, principal, "project.repository_linked",
                        repo["id"])
            out = dict(row)
            out["full_name"] = repo["full_name"]
            out["default_branch"] = repo["default_branch"]
            out["enabled"] = repo["enabled"]
            return out

    def set_primary(self, principal: str, project_id: str,
                    repository_full_name: str) -> dict:
        """Make one linked repository the project's primary. Explicit action."""
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_project_members "
                        "WHERE project_id=%s AND principal=%s AND role='owner'",
                        (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute("SELECT id FROM repositories WHERE full_name=%s",
                        (repository_full_name,))
            repo = cur.fetchone()
            if repo is None:
                raise NotFound("repository not linked to this project")
            cur.execute("UPDATE nexus_project_repositories SET is_primary=false "
                        "WHERE project_id=%s AND is_primary", (project_id,))
            cur.execute("UPDATE nexus_project_repositories SET is_primary=true "
                        "WHERE project_id=%s AND repository_id=%s "
                        "RETURNING project_id, repository_id",
                        (project_id, repo["id"]))
            row = cur.fetchone()
            if row is None:
                raise NotFound("repository not linked to this project")
            self._event(cur, project_id, principal, "project.repository_primary",
                        repo["id"])
            return {"project_id": project_id, "repository_id": str(repo["id"]),
                    "full_name": repository_full_name, "is_primary": True}

    def set_account(self, principal: str, project_id: str, repository_full_name: str,
                    account_id: str | None) -> dict:
        """Change which GitHub account reads one linked repository."""
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_project_members "
                        "WHERE project_id=%s AND principal=%s AND role='owner'",
                        (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            if account_id is not None:
                cur.execute("SELECT 1 FROM nexus_github_accounts "
                            "WHERE id=%s AND principal=%s", (account_id, principal))
                if cur.fetchone() is None:
                    raise NotFound("GitHub account not found")
            cur.execute("SELECT id FROM repositories WHERE full_name=%s",
                        (repository_full_name,))
            repo = cur.fetchone()
            if repo is None:
                raise NotFound("repository not linked to this project")
            cur.execute("UPDATE nexus_project_repositories SET github_account_id=%s "
                        "WHERE project_id=%s AND repository_id=%s "
                        "RETURNING project_id, repository_id, github_account_id",
                        (account_id, project_id, repo["id"]))
            row = cur.fetchone()
            if row is None:
                raise NotFound("repository not linked to this project")
            self._event(cur, project_id, principal, "project.repository_account",
                        repo["id"])
            return dict(row)

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
        """The project's linked repositories, primary first.

        Each row names the GitHub account that reads it (``github_account_id``,
        ``account_login``) and whether it is the primary one. An account that
        was revoked is reported by its state, never hidden: the operator needs
        to see that a repository is no longer readable.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s", (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            cur.execute(
                "SELECT r.id, r.full_name, r.provider, r.default_branch, "
                "       r.allowed_branches, r.enabled, pr.created_at AS linked_at, "
                "       pr.github_account_id, pr.is_primary, pr.label, "
                "       a.login AS account_login, a.state AS account_state "
                "FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "LEFT JOIN nexus_github_accounts a ON a.id = pr.github_account_id "
                "WHERE pr.project_id=%s "
                "ORDER BY pr.is_primary DESC, r.full_name",
                (project_id,),
            )
            out = []
            for row in cur.fetchall():
                item = dict(row)
                if item.get("github_account_id") is not None:
                    item["github_account_id"] = str(item["github_account_id"])
                out.append(item)
            return out

    def primary_repository(self, project_id: str) -> str | None:
        """The project's primary repository (``owner/name``), or ``None``.

        The one a task that names no repository runs against. When the operator
        has not chosen one but the project has exactly one linked repository,
        that one is the primary in effect: a project with a single repository
        needs no ceremony to use it.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT r.full_name FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "WHERE pr.project_id=%s AND pr.is_primary LIMIT 1", (project_id,))
            row = cur.fetchone()
            if row is not None:
                return row["full_name"]
            cur.execute(
                "SELECT r.full_name FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "WHERE pr.project_id=%s ORDER BY r.full_name LIMIT 2", (project_id,))
            rows = cur.fetchall()
            return rows[0]["full_name"] if len(rows) == 1 else None

    def account_for_repository(self, project_id: str, repository_full_name: str
                               ) -> tuple[str | None, str | None]:
        """The (principal, account_id) that reads a linked repository, or (None, None).

        Read by the worker and the verifier, which are not project members: it
        returns the identity only, never a token. A repository with no linked
        account returns ``(None, None)``, meaning "read with the server token".
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT a.principal, a.id AS account_id "
                "FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "JOIN nexus_github_accounts a ON a.id = pr.github_account_id "
                "WHERE pr.project_id=%s AND r.full_name=%s AND a.state='active'",
                (project_id, repository_full_name))
            row = cur.fetchone()
        if row is None:
            return (None, None)
        return (str(row["principal"]), str(row["account_id"]))

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
