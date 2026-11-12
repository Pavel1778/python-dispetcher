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


#: "Leave this field as it is" - distinct from ``None``, which clears it.
_KEEP = object()

_LINK_COLUMNS = (
    "r.id, r.full_name, r.provider, r.default_branch, r.allowed_branches, "
    "r.enabled AS repository_enabled, pr.created_at AS linked_at, pr.github_account_id, "
    "pr.is_primary, pr.label, pr.enabled, pr.branch, pr.role_note, pr.access, "
    "pr.checked_at, a.login AS account_login, a.state AS account_state, "
    "a.label AS account_label, p.github_account_id AS project_account_id")


class ProjectRepositoryService:
    """A project's repositories: several, each with its own GitHub account.

    A link (``nexus_project_repositories``) is the only thing that authorizes a
    project to work on a repository. The global allowlist is still required (a
    project cannot bind a name that was never discovered), but it is never
    sufficient.

    What a link says besides "this repository belongs to this project":

    * **which GitHub account** reads and checks it (``github_account_id``). A
      link without one uses the project's account, and without that the
      server's own token - the rule from before links carried an account. So
      ``account-A/frontend`` and ``account-B/backend`` can sit in one project;
    * **which one is the main repository** (``is_primary``): the one a task
      that names no repository works on. It is the repository the project is
      bound to (``nexus_projects.repository_id``); the two are changed together
      and cannot disagree;
    * **the branch** its tasks work on (empty: the repository's default) and
      **what it is for** (``role_note``) - both given to the planner and to
      the agents;
    * **whether it is switched on** for this project. A link that is off stays
      in the list and authorizes nothing;
    * **what the last check found**: can the account see it, may it push, is
      it private.

    Only an owner of the project changes its links, and only with GitHub
    accounts of their own. A member may read them. No token is ever part of
    what this class returns.
    """

    def __init__(self, store: PostgresStore):
        self.store = store

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _owner(cur: Any, project_id: str, principal: str) -> None:
        cur.execute(
            "SELECT 1 FROM nexus_project_members "
            "WHERE project_id=%s AND principal=%s AND role='owner'",
            (project_id, principal))
        if cur.fetchone() is None:
            raise NotFound("project not found")

    @staticmethod
    def _account(cur: Any, principal: str, account_id: str) -> None:
        """*account_id* must be the caller's own: a project cannot be pointed
        at another person's GitHub account."""
        cur.execute("SELECT 1 FROM nexus_github_accounts WHERE id=%s AND principal=%s",
                    (account_id, principal))
        if cur.fetchone() is None:
            raise NotFound("GitHub account not found")

    @staticmethod
    def _repository(cur: Any, full_name: str) -> dict:
        cur.execute("SELECT id, full_name, default_branch, allowed_branches, enabled "
                    "FROM repositories WHERE full_name=%s", ((full_name or "").strip(),))
        repo = cur.fetchone()
        if repo is None:
            raise NotFound("repository not in the allowlist")
        return dict(repo)

    @staticmethod
    def _branch_ok(repo: dict, branch: str) -> None:
        allowed = repo["allowed_branches"] or []
        if allowed and branch not in allowed:
            raise ProjectBindingError(
                f"branch {branch} is not in the repository's allowed branches")
        if not allowed and branch != repo["default_branch"]:
            raise ProjectBindingError(
                f"branch {branch} is not allowed (default branch only)")

    @classmethod
    def _make_primary(cls, cur: Any, project_id: str, repo: dict, branch: str) -> None:
        """One main repository per project, and the project bound to it."""
        cur.execute("UPDATE nexus_project_repositories SET is_primary=false, updated_at=now() "
                    "WHERE project_id=%s AND is_primary AND repository_id<>%s",
                    (project_id, repo["id"]))
        cur.execute("UPDATE nexus_project_repositories SET is_primary=true, updated_at=now() "
                    "WHERE project_id=%s AND repository_id=%s", (project_id, repo["id"]))
        cur.execute("UPDATE nexus_projects SET repository_id=%s, selected_branch=%s, "
                    "updated_at=now() WHERE id=%s",
                    (repo["id"], branch or repo["default_branch"], project_id))

    @staticmethod
    def _view(row: Any) -> dict:
        item = dict(row)
        for key in ("id", "github_account_id", "project_account_id"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        item["repository_id"] = item["id"]
        # The branch tasks of this repository work on.
        item["working_branch"] = item.get("branch") or item["default_branch"]
        # Whose credentials read and check it, in words the interface shows.
        if item.get("github_account_id"):
            item["identity"] = "account"
        elif item.get("project_account_id"):
            item["identity"] = "project_account"
        else:
            item["identity"] = "server_token"
        # Work may run on it only when the link and the repository are both on.
        item["usable"] = bool(item["enabled"] and item["repository_enabled"])
        return item

    def _one(self, cur: Any, project_id: str, repository_id: Any) -> dict:
        cur.execute(
            f"SELECT {_LINK_COLUMNS} FROM nexus_project_repositories pr "
            "JOIN repositories r ON r.id = pr.repository_id "
            "JOIN nexus_projects p ON p.id = pr.project_id "
            "LEFT JOIN nexus_github_accounts a ON a.id = pr.github_account_id "
            "WHERE pr.project_id=%s AND pr.repository_id=%s", (project_id, repository_id))
        row = cur.fetchone()
        if row is None:
            raise NotFound("repository not linked to this project")
        return self._view(row)

    # --------------------------------------------------------------- changing
    def link(self, principal: str, project_id: str, repository_full_name: str, *,
             account_id: Any = _KEEP, label: Any = _KEEP,
             make_primary: bool = False, branch: Any = _KEEP,
             role_note: Any = _KEEP, enabled: Any = _KEEP) -> dict:
        """Link an allowlisted repository to a project the principal owns.

        The repository must exist in the global allowlist (so a project cannot
        bind a name that was never discovered/approved). Linking one that is
        already linked changes only what is given: a second "link" never
        silently takes a repository's account or its place as the main one
        away.

        *account_id* is the connected GitHub account that reads this
        repository; it must be the caller's own. *make_primary* makes this the
        project's main repository; a project's first repository becomes that
        by itself.
        """
        with self.store._tx() as cur:
            self._owner(cur, project_id, principal)
            repo = self._repository(cur, repository_full_name)
            if account_id is not _KEEP and account_id is not None:
                self._account(cur, principal, account_id)
            cur.execute(
                "INSERT INTO nexus_project_repositories(project_id, repository_id, created_by) "
                "VALUES (%s,%s,%s) ON CONFLICT (project_id, repository_id) DO NOTHING",
                (project_id, repo["id"], principal))
            created = cur.rowcount > 0
            self._apply(cur, project_id, repo, account_id=account_id, label=label,
                        branch=branch, role_note=role_note, enabled=enabled)
            cur.execute("SELECT count(*) AS n FROM nexus_project_repositories "
                        "WHERE project_id=%s", (project_id,))
            only = cur.fetchone()["n"] == 1
            if make_primary or only:
                link = self._one(cur, project_id, repo["id"])
                self._make_primary(cur, project_id, repo, link["branch"])
            if created:
                self._event(cur, project_id, principal, "project.repository_linked",
                            repo["id"])
            return self._one(cur, project_id, repo["id"])

    def _apply(self, cur: Any, project_id: str, repo: dict, *, account_id: Any,
               label: Any, branch: Any, role_note: Any, enabled: Any) -> None:
        sets, params = [], []
        if account_id is not _KEEP:
            sets.append("github_account_id=%s")
            params.append(account_id)
            # What was found with the old identity says nothing about the new.
            sets += ["access=NULL", "checked_at=NULL"]
        if label is not _KEEP:
            sets.append("label=%s")
            params.append(_text(label or "", "label", maximum=120, required=False))
        if branch is not _KEEP:
            branch = _text(branch or "", "branch", maximum=250, required=False)
            if branch:
                self._branch_ok(repo, branch)
            sets.append("branch=%s")
            params.append(branch)
        if role_note is not _KEEP:
            sets.append("role_note=%s")
            params.append(_text(role_note or "", "role_note", maximum=500, required=False))
        if enabled is not _KEEP:
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false")
            sets.append("enabled=%s")
            params.append(enabled)
        if not sets:
            return
        cur.execute(
            f"UPDATE nexus_project_repositories SET {', '.join(sets)}, updated_at=now() "
            "WHERE project_id=%s AND repository_id=%s", (*params, project_id, repo["id"]))

    def update(self, principal: str, project_id: str, repository_full_name: str, *,
               account_id: Any = _KEEP, label: Any = _KEEP, branch: Any = _KEEP,
               role_note: Any = _KEEP, enabled: Any = _KEEP) -> dict:
        """Change a link: its account, its branch, its note, its switch. Only
        what is given is changed. Switching the main repository off is
        refused: choose another main repository first."""
        with self.store._tx() as cur:
            self._owner(cur, project_id, principal)
            repo = self._repository(cur, repository_full_name)
            link = self._one(cur, project_id, repo["id"])
            if account_id is not _KEEP and account_id is not None:
                self._account(cur, principal, account_id)
            if enabled is False and link["is_primary"]:
                raise ProjectBindingError(
                    "the main repository cannot be switched off; make another one main first")
            self._apply(cur, project_id, repo, account_id=account_id, label=label,
                        branch=branch, role_note=role_note, enabled=enabled)
            if link["is_primary"] and branch is not _KEEP:
                # The project is bound to its main repository's branch.
                cur.execute("UPDATE nexus_projects SET selected_branch=%s, updated_at=now() "
                            "WHERE id=%s", (branch or repo["default_branch"], project_id))
            self._event(cur, project_id, principal, "project.repository_changed", repo["id"])
            return self._one(cur, project_id, repo["id"])

    def set_primary(self, principal: str, project_id: str,
                    repository_full_name: str) -> dict:
        """Make one linked repository the project's main one. Explicit action."""
        with self.store._tx() as cur:
            self._owner(cur, project_id, principal)
            try:
                repo = self._repository(cur, repository_full_name)
            except NotFound:
                raise NotFound("repository not linked to this project") from None
            link = self._one(cur, project_id, repo["id"])
            if not link["usable"]:
                raise ProjectBindingError(
                    "a repository that is switched off cannot be the main one")
            self._make_primary(cur, project_id, repo, link["branch"])
            self._event(cur, project_id, principal, "project.repository_primary", repo["id"])
            return self._one(cur, project_id, repo["id"])

    def set_account(self, principal: str, project_id: str, repository_full_name: str,
                    account_id: str | None) -> dict:
        """Change which GitHub account reads one linked repository; ``None``
        returns it to the project's account (or the server's token)."""
        try:
            return self.update(principal, project_id, repository_full_name,
                               account_id=account_id)
        except NotFound as e:
            if "allowlist" in str(e):
                raise NotFound("repository not linked to this project") from None
            raise

    def unlink(self, principal: str, project_id: str, repository_full_name: str) -> bool:
        """Remove a link. Fails while a non-terminal task still uses it.

        Unlinking a repository out from under a running task would let that task
        keep editing a checkout the project no longer owns. Refuse until the work
        is terminal; the operator can cancel the task first.
        """
        with self.store._tx() as cur:
            self._owner(cur, project_id, principal)
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
            # The same for an autonomous run: a task at work, or one that a
            # live run will still start, on this repository.
            cur.execute(
                "SELECT 1 FROM nexus_run_tasks t JOIN nexus_runs r ON r.id = t.run_id "
                "WHERE t.project_id=%s AND t.repository=%s "
                "AND (t.state IN ('dispatched','running','verifying') "
                "     OR (t.state IN ('pending','waiting') "
                "         AND r.state IN ('running','paused','waiting_for_approval'))) LIMIT 1",
                (project_id, repository_full_name))
            if cur.fetchone() is not None:
                raise AuthorizationError(
                    "an autonomous run still has tasks on this repository; cancel it first")
            cur.execute(
                "DELETE FROM nexus_project_repositories "
                "WHERE project_id=%s AND repository_id=%s RETURNING is_primary",
                (project_id, repo["id"]),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if row["is_primary"]:
                # The project is no longer bound to a repository it does not
                # have. Nothing else is promoted in its place: which repository
                # is the main one is a person's choice.
                cur.execute("UPDATE nexus_projects SET repository_id=NULL, "
                            "selected_branch=NULL, updated_at=now() "
                            "WHERE id=%s AND repository_id=%s", (project_id, repo["id"]))
            self._event(cur, project_id, principal, "project.repository_unlinked", repo["id"])
            return True

    # ---------------------------------------------------------------- reading
    def list(self, principal: str, project_id: str) -> list[dict]:
        """The project's linked repositories, the main one first.

        Each row names the GitHub account that reads it (``github_account_id``,
        ``account_login``), whether it is the main one, its branch and note,
        its switch and what the last access check found. An account that was
        revoked is reported by its state, never hidden: the operator needs to
        see that a repository is no longer readable.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s", (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            return self._links(cur, project_id)

    def _links(self, cur: Any, project_id: str) -> list[dict]:
        cur.execute(
            f"SELECT {_LINK_COLUMNS} FROM nexus_project_repositories pr "
            "JOIN repositories r ON r.id = pr.repository_id "
            "JOIN nexus_projects p ON p.id = pr.project_id "
            "LEFT JOIN nexus_github_accounts a ON a.id = pr.github_account_id "
            "WHERE pr.project_id=%s ORDER BY pr.is_primary DESC, r.full_name",
            (project_id,))
        return [self._view(row) for row in cur.fetchall()]

    def context(self, project_id: str) -> list[dict]:
        """The repositories work may run on, for the planner and the agents:
        ``[{full_name, branch, is_primary, role_note}]``, the main one first.
        Read by the loop, which is not a member; nothing here is a secret."""
        with self.store._tx() as cur:
            return [{"full_name": link["full_name"], "branch": link["working_branch"],
                     "is_primary": link["is_primary"], "role_note": link["role_note"],
                     "label": link["label"]}
                    for link in self._links(cur, project_id) if link["usable"]]

    def account_for_repository(self, project_id: str, repository_full_name: str
                               ) -> tuple[str | None, str | None]:
        """The ``(principal, account_id)`` that reads a repository of a project,
        or ``(None, None)`` for the server's own token.

        The link's own account when it has one; otherwise the project's
        account; otherwise none. Read by the worker and the verifier, which are
        not project members: it returns the identity only, never a token. The
        account is returned whatever its state: a revoked one is the caller's
        error to raise, never a reason to read as somebody else.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT a.principal, a.id AS account_id "
                "FROM nexus_project_repositories pr "
                "JOIN repositories r ON r.id = pr.repository_id "
                "JOIN nexus_github_accounts a ON a.id = pr.github_account_id "
                "WHERE pr.project_id=%s AND r.full_name=%s",
                (project_id, repository_full_name))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT a.principal, a.id AS account_id FROM nexus_projects p "
                    "JOIN nexus_github_accounts a ON a.id = p.github_account_id "
                    "WHERE p.id=%s", (project_id,))
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

    # --------------------------------------------------------------- checking
    def record_access(self, principal: str, project_id: str, repository_full_name: str,
                      access: dict) -> dict:
        """Store what a check of one repository found (see ``check_access``)."""
        import json

        with self.store._tx() as cur:
            cur.execute(
                "SELECT 1 FROM nexus_project_members "
                "WHERE project_id=%s AND principal=%s", (project_id, principal))
            if cur.fetchone() is None:
                raise NotFound("project not found")
            repo = self._repository(cur, repository_full_name)
            cur.execute(
                "UPDATE nexus_project_repositories SET access=%s::jsonb, checked_at=now() "
                "WHERE project_id=%s AND repository_id=%s",
                (json.dumps(access, ensure_ascii=False, default=str), project_id, repo["id"]))
            if cur.rowcount == 0:
                raise NotFound("repository not linked to this project")
            return self._one(cur, project_id, repo["id"])

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))


def check_access(client: Any, full_name: str, branch: str) -> dict:
    """Ask GitHub, as *client*, what it may do with one repository.

    Returns ``{"readable", "push", "admin", "private", "default_branch",
    "branch_exists", "error"}``. A repository the identity cannot see is not an
    exception here: it is ``readable: False`` with the reason, which is the
    answer the person checking wants to read.
    """
    from .github_client import GitHubAuthError, GitHubError, GitHubUnavailable

    out: dict[str, Any] = {"readable": False, "push": False, "admin": False, "private": None,
                           "default_branch": None, "branch": branch,
                           "branch_exists": None, "error": None}
    try:
        info = client.repository_access(full_name)
    except GitHubAuthError as e:
        out["error"] = f"GitHub rejected the credentials: {e}"
        return out
    except GitHubUnavailable as e:
        out["error"] = f"GitHub is unavailable: {e}"
        return out
    except (GitHubError, ValueError) as e:
        out["error"] = ("the repository was not found, or this identity cannot see it"
                        if "404" in str(e) else str(e))
        return out
    out.update(readable=True, push=bool(info.get("push")), admin=bool(info.get("admin")),
               private=bool(info.get("private")), default_branch=info.get("default_branch"))
    if branch:
        try:
            out["branch_exists"] = client.default_branch_sha(full_name, branch) is not None
        except (GitHubError, ValueError):
            out["branch_exists"] = False
    return out


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
            "SELECT pr.enabled FROM nexus_project_repositories pr "
            "WHERE pr.project_id=%s AND pr.repository_id=%s",
            (project_id, repo_row["id"]))
        link = cur.fetchone()
        if link is None:
            return f"repository {repository} is not linked to project {project_id}"
        if not link["enabled"]:
            return f"repository {repository} is switched off for project {project_id}"
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
            # The repository a project is bound to is its main one, switched
            # on, on the bound branch: the link and the binding say the same.
            cur.execute("UPDATE nexus_project_repositories SET is_primary=false, "
                        "updated_at=now() WHERE project_id=%s AND is_primary "
                        "AND repository_id<>%s", (project_id, repo["id"]))
            cur.execute("UPDATE nexus_project_repositories SET is_primary=true, enabled=true, "
                        "branch=%s, updated_at=now() WHERE project_id=%s AND repository_id=%s",
                        ("" if chosen == repo["default_branch"] else chosen,
                         project_id, repo["id"]))
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
            cur.execute("UPDATE nexus_project_repositories SET branch=%s, updated_at=now() "
                        "WHERE project_id=%s AND repository_id=%s",
                        ("" if branch == row["default_branch"] else branch,
                         project_id, row["repository_id"]))
            cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                        "VALUES (%s,%s,'project.branch_changed',%s)",
                        (project_id, principal, row["repository_id"]))
            return out
