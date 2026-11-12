"""Account-aware dispatch: pick an executor, fail over, and hand off context.

The lab scheduler dispatches a task through an executor. This module sits
between them: it chooses *which* OpenHands account runs a task (through the
pool policy), records the choice on the task, and — when an account fails —
switches to another one and passes the context the new executor needs to
continue rather than restart.

Two facts drive the design:

* A Cloud conversation cannot be moved between accounts. A different account is
  a different sandbox and a different key, so "continue on account B" means
  *start a new conversation on B* and tell it what has already happened. The
  hand-off context is that message; it is built from the durable run state, not
  from anything the old conversation said.
* The old executor must not keep writing. A conversation whose account is gone
  is not polled again (the task's ``tried_accounts`` excludes it), and the
  dispatch is account-scoped, so nothing reattaches to the abandoned sandbox.

Context passed across a hand-off (the list the task text asks for):

  project id, task id, the original goal text, the plan (all task keys and
  titles), which tasks are already ``completed_verified`` and which remain,
  the repository and branch, the last observed Git commit, the previous
  executor's final report, the last error, and the acceptance criteria.

Nothing here is a secret: the context is derived from rows the caller may read.

Which accounts a task may be given at all is decided in two layers:

* a project that has its **own list of executors** (``nexus_executors``) is
  served from that list and nothing else - in order under ``failover`` (the
  primary while it is usable, and again as soon as it is back), by load under
  ``distribute``;
* a project without a list is served by its owner's account pool, as before;
  and when the owner has stored no account at all, ``select_for_task`` returns
  ``None``: the task runs under the server's own key.
"""
from __future__ import annotations


from .cloud_store import PostgresStore
from .nexus_executors import ProjectExecutorService
from .nexus_lab_store import LabStore
from .openhands_pool import AccountPool, PoolError


class NoUsableAccount(RuntimeError):
    """Every account in the pool is excluded or unusable."""


class AccountRouter:
    """Choose an account for a task and build the failover context."""

    def __init__(self, store: PostgresStore, *, pool: AccountPool,
                 lab: LabStore, principal: str = "machine:default"):
        self.store = store
        self.pool = pool
        self.lab = lab
        self.principal = principal
        self.executors = ProjectExecutorService(store, pool.accounts)

    def _principal_for_project(self, project_id: str | None) -> str:
        """The principal whose accounts serve this project.

        Accounts are stored per principal, and the worker runs as a machine
        principal that owns none. The project's owner is the principal whose
        pool applies, so a project created by ``user:owner`` uses that user's
        accounts even when the worker is the one dispatching. With no project
        (or no owner) the configured default principal is used.
        """
        if not project_id:
            return self.principal
        with self.store._tx() as cur:
            cur.execute("SELECT principal FROM nexus_project_members "
                        "WHERE project_id=%s ORDER BY (role='owner') DESC, principal LIMIT 1",
                        (project_id,))
            row = cur.fetchone()
        return row["principal"] if row else self.principal

    def select_for_task(self, task: dict) -> dict | None:
        """The account to run *task*, never one that has failed it; ``None``
        when the task's project has no stored executor to choose from, which
        means "the server's own key".

        A project with its own list of executors is served from that list
        only (``_from_project``). Otherwise the owner's pool decides, as
        before.

        An operator's manual switch records ``preferred_account_id``; that
        account is used when it is usable, so a switch the operator asked for is
        honoured. It is only a preference: if the target is disabled, revoked or
        unusable, the normal policy applies rather than failing the task.

        Raises :class:`NoUsableAccount` when there are executors and none can
        take this task now, so the caller can say why instead of looping.

        Only ``tried_accounts`` is excluded - the accounts a start failed on or
        a conversation vanished from. The account the task last *ran* on is not:
        a result that did not verify is tried again, and with one account in
        the pool there is nowhere else to try it. (Excluding it blocked every
        second attempt with "no usable OpenHands account".)
        """
        exclude = {str(x) for x in (task.get("tried_accounts") or [])}
        project_id = str(task.get("project_id") or "") or None
        preferred = str(task.get("preferred_account_id") or "") or None
        if project_id:
            strategy, assigned = self.executors.assigned(project_id)
            if assigned:
                return self._from_project(assigned, strategy, exclude, preferred)
        principal = self._principal_for_project(project_id)
        accounts = self.pool.accounts.list_accounts(principal)
        if not accounts:
            return None
        try:
            if preferred:
                for account in accounts:
                    if str(account["id"]) == preferred and self.pool._usable(
                            account, exclude):
                        return account
            return self.pool.select(principal, exclude=exclude, project_id=project_id)
        except PoolError as e:
            raise NoUsableAccount(str(e)) from e

    def _from_project(self, assigned: list[dict], strategy: str, exclude: set[str],
                      preferred: str | None) -> dict:
        """Choose among a project's own executors.

        Usable = on the project's list and switched on there, the connection
        itself switched on, active, not marked unavailable, and not already
        tried for this task. Nothing outside the list is ever considered.
        """
        usable = [item for item in assigned
                  if item["usable"] and item["account_id"] not in exclude]
        if not usable:
            reasons = []
            for item in assigned:
                why = ("already tried for this task" if item["account_id"] in exclude
                       else "switched off for this project" if not item["project_enabled"]
                       else "switched off" if not item["enabled"]
                       else f"is {item['state']}" if item["state"] != "active"
                       else "unavailable" if item["health"] == "unavailable"
                       else "not usable")
                reasons.append(f"{item['label']}: {why}")
            raise NoUsableAccount(
                "none of the project's OpenHands executors can take the task now ("
                + "; ".join(reasons) + ")")
        if preferred:
            for item in usable:
                if item["account_id"] == preferred:
                    return item
        if strategy == "distribute" and len(usable) > 1:
            live = self.pool._live_load()
            usable.sort(key=lambda item: (live.get(item["account_id"], 0), item["position"]))
        return usable[0]

    def principal_for_task(self, task: dict) -> str:
        return self._principal_for_project(str(task.get("project_id") or "") or None)

    def _owner(self, account_id: str, task: dict | None = None) -> str:
        """Whose connection *account_id* is. Health is recorded against the
        owner; for a connection that is gone, the task's project owner stands
        in so the call is still a harmless no-op."""
        owner = self.pool.accounts.owner_of(str(account_id))
        return owner or (self.principal_for_task(task) if task else self.principal)

    def reprobe(self, task: dict | None = None) -> list[dict]:
        """Ask the executors that were written off whether they are back -
        those of *task*'s project when one is given."""
        project_id = str((task or {}).get("project_id") or "") or None
        return self.pool.accounts.reprobe_unavailable(project_id=project_id)

    def handoff_context(self, task: dict, *, previous_report: str = "",
                        last_error: str = "") -> dict:
        """The durable context to give a new executor for *task*.

        Built from the run's rows, so it is reproducible after a crash and never
        depends on the old conversation being reachable.
        """
        run_id = str(task["run_id"])
        with self.store._tx() as cur:
            cur.execute("SELECT state, iteration, max_iterations FROM nexus_runs "
                        "WHERE id=%s", (run_id,))
            run = cur.fetchone() or {}
            cur.execute(
                "SELECT task_key, title, state, repository, branch FROM nexus_run_tasks "
                "WHERE run_id=%s ORDER BY created_at, id", (run_id,))
            siblings = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT text FROM nexus_goals g JOIN nexus_runs r ON r.goal_id=g.id "
                        "WHERE r.id=%s", (run_id,))
            goal = cur.fetchone()
        plan = [{"task_key": s["task_key"], "title": s["title"], "state": s["state"]}
                for s in siblings]
        done = [s["task_key"] for s in siblings if s["state"] == "completed_verified"]
        remaining = [s["task_key"] for s in siblings
                     if s["state"] not in ("completed_verified", "failed")]
        evidence = task.get("evidence") or []
        last_commit = ""
        for item in reversed(evidence):
            if isinstance(item, dict) and item.get("remote_sha"):
                last_commit = str(item["remote_sha"])
                break
        return {
            "project_id": str(task.get("project_id") or ""),
            "task_id": str(task.get("id") or ""),
            "task_key": task.get("task_key"),
            "run_id": run_id,
            "original_goal": (goal or {}).get("text", ""),
            "task_title": task.get("title"),
            "task_description": task.get("description") or "",
            "acceptance": task.get("acceptance") or [],
            "repository": task.get("repository") or "",
            "branch": task.get("branch") or "",
            "plan": plan,
            "completed_tasks": done,
            "remaining_tasks": remaining,
            "run_state": run.get("state"),
            "iteration": run.get("iteration"),
            "max_iterations": run.get("max_iterations"),
            "last_commit": last_commit,
            "previous_report": (previous_report or "")[:4000],
            "last_error": (last_error or "")[:1000],
        }

    @staticmethod
    def handoff_message(context: dict) -> str:
        """A deterministic first message for the new executor.

        It states plainly that this is a *continuation*: the work already done
        must not be repeated, and the checkout is fresh (a new sandbox), so the
        repository has to be cloned at the named branch.
        """
        plan_lines = "\n".join(
            f"  - {t['task_key']}: {t['title']} [{t['state']}]" for t in context.get("plan", []))
        done = ", ".join(context.get("completed_tasks") or []) or "(none)"
        remaining = ", ".join(context.get("remaining_tasks") or []) or "(none)"
        criteria = "\n".join(f"  - {c}" for c in (context.get("acceptance") or [])) or "  - (none)"
        return (
            "You are continuing an interrupted task on a new executor. The previous "
            "OpenHands account is no longer available, so this is a fresh sandbox.\n\n"
            f"Project: {context.get('project_id')}\n"
            f"Run: {context.get('run_id')}  Task: {context.get('task_key')}\n"
            f"Repository: {context.get('repository') or '(none)'}  "
            f"Branch: {context.get('branch') or '(default)'}\n"
            f"Last known commit: {context.get('last_commit') or '(unknown)'}\n\n"
            f"Original goal:\n{context.get('original_goal') or '(none)'}\n\n"
            f"Plan (all tasks in this run):\n{plan_lines or '  (none)'}\n\n"
            f"Already completed and verified (do NOT redo these): {done}\n"
            f"Still to do: {remaining}\n\n"
            f"This task: {context.get('task_title')}\n{context.get('task_description') or ''}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"Previous executor's last report:\n{context.get('previous_report') or '(none)'}\n"
            f"Last error that caused the switch: {context.get('last_error') or '(none)'}\n\n"
            "Rules:\n"
            "1. The workspace is empty: clone the repository at the branch above first.\n"
            "2. Do not repeat work already completed and verified.\n"
            "3. Continue from the last known commit where possible.\n"
            "4. Run the relevant tests and report exact commits, files and results.\n"
            "5. Finish with a short report; do not claim success you did not verify."
        )

    def record(self, task: dict, *, account_id: str) -> None:
        """Persist that *task* is now bound to *account_id*."""
        self.lab.mark_running(task["id"], account_id=account_id)

    def note_failure(self, task: dict, *, account_id: str, error: str,
                     unavailable: bool) -> None:
        """Record a failed account on the task and in the pool's health."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET tried_accounts = "
                "  CASE WHEN tried_accounts ? %s::text THEN tried_accounts "
                "       ELSE tried_accounts || to_jsonb(%s::text) END, "
                "  updated_at=now() WHERE id=%s",
                (account_id, account_id, task["id"]))
        self.pool.record_failure(self._owner(account_id, task), account_id, error,
                                 unavailable=unavailable)

    def note_success(self, task: dict) -> None:
        """Mark the task's account healthy again after it produced a result.

        Without this, an account that was marked ``unavailable`` by a single
        transient failure would stay excluded from every future selection: the
        pool only ever heard bad news. A conversation that reached a terminal
        state on the account is the evidence that it works now.
        """
        account_id = task.get("openhands_account_id")
        if not account_id:
            return
        self.pool.record_success(self._owner(str(account_id), task), str(account_id))


def build_account_router(store: PostgresStore, *,
                         principal: str = "machine:default") -> AccountRouter | None:
    """The account router for the loop, or ``None`` when the database is not
    ready for one.

    The router is always built: whether a task goes to a stored account is
    decided per task (``select_for_task`` returns ``None`` for a project that
    has no executor to choose from, and the task then runs under the server's
    ``OPENHANDS_API_KEY`` exactly as before). It used to be built only when
    *principal* - ``machine:default`` - owned an account at the moment the loop
    started, so accounts added in the studio, which belong to the person who
    added them, never came into use, and nothing added later did either.
    """
    from .nexus_openhands_accounts import OpenHandsAccountService

    accounts = OpenHandsAccountService(store)
    try:
        accounts.list_accounts(principal)
    except Exception:  # noqa: BLE001 - a database that is not ready means "no pool"
        return None
    pool = AccountPool(store, accounts)
    return AccountRouter(store, pool=pool, lab=LabStore(store), principal=principal)
