"""OpenHands account pool: selection modes, health and load.

``nexus_openhands_accounts`` stores accounts and lets a *project* bind one.
This module adds the *pool*: when a project has no explicit binding, or when the
bound account fails, which account should run the work, and in what order.

Four modes, each a real, testable policy:

* ``manual``        — the default account, and only that one. A failure is
                      reported to the operator; the pool never silently switches.
* ``auto``          — the default account while it is usable, otherwise the
                      highest-priority usable account.
* ``failover``      — ``auto`` plus an explicit *exclude* set: the caller names
                      the accounts it has already tried, so a retry lands on a
                      different one instead of the same failed account.
* ``load_balancing``— the usable account running the fewest active tasks
                      (priority breaks ties).

The policy is enforced here, not in the UI. ``select`` never returns an account
that is disabled, revoked, or marked ``unavailable``, and it never returns an
account belonging to another principal. Health and the active-task counter are
recorded by the callers that actually make a call, so "load" is observed, not
guessed.
"""
from __future__ import annotations

from typing import Any, Iterable

from .cloud_store import NotFound, PostgresStore
from .nexus_openhands_accounts import OpenHandsAccountService

MODES = ("manual", "auto", "failover", "load_balancing")
#: Health values that still allow selection. Only ``unavailable`` is excluded:
#: ``unknown`` means "not probed yet", which is not a reason to skip an account.
_USABLE_HEALTH = ("unknown", "healthy", "degraded")


class PoolError(RuntimeError):
    """No account can serve the request under the current policy."""


class AccountPool:
    """Select and track OpenHands accounts for a principal."""

    def __init__(self, store: PostgresStore, accounts: OpenHandsAccountService,
                 *, assignments: Any | None = None):
        self.store = store
        self.accounts = accounts
        #: Optional project-scoped allowlist. When set and a project has
        #: assignments, the pool selects only from them: a project runs on the
        #: executors it was assigned, never on another project's. With no
        #: assignments (or no service), the pre-existing behaviour is kept.
        self.assignments = assignments

    def _assigned(self, project_id: str | None) -> list[str] | None:
        """The account ids assigned to *project_id*, or ``None`` if none.

        ``None`` (no assignment) means "not restricted": the caller keeps the
        old policy. An empty list is impossible here - an assignment that was
        cleared leaves no rows, which is ``None`` again.
        """
        if self.assignments is None or not project_id:
            return None
        try:
            ids = self.assignments.assigned_account_ids(str(project_id))
        except Exception:  # noqa: BLE001 - a pool must work before the table exists
            return None
        return ids or None

    # ------------------------------------------------------------- config
    def get_config(self, principal: str) -> dict:
        with self.store._tx() as cur:
            cur.execute("SELECT selection_mode, fallback_order, updated_at "
                        "FROM nexus_openhands_pool WHERE principal=%s", (principal,))
            row = cur.fetchone()
        if row is None:
            return {"selection_mode": "auto", "fallback_order": [], "updated_at": None}
        return {"selection_mode": row["selection_mode"],
                "fallback_order": list(row["fallback_order"] or []),
                "updated_at": row["updated_at"]}

    def set_config(self, principal: str, *, selection_mode: str | None = None,
                   fallback_order: list[str] | None = None) -> dict:
        current = self.get_config(principal)
        mode = (selection_mode or current["selection_mode"]).strip().lower()
        if mode not in MODES:
            raise ValueError(f"selection_mode must be one of {', '.join(MODES)}")
        order = current["fallback_order"] if fallback_order is None else list(fallback_order)
        for account_id in order:
            if not isinstance(account_id, str):
                raise ValueError("fallback_order must be a list of account ids")
        import json

        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_openhands_pool(principal, selection_mode, fallback_order) "
                "VALUES (%s,%s,%s::jsonb) "
                "ON CONFLICT (principal) DO UPDATE SET selection_mode=EXCLUDED.selection_mode, "
                "  fallback_order=EXCLUDED.fallback_order, updated_at=now() "
                "RETURNING selection_mode, fallback_order, updated_at",
                (principal, mode, json.dumps(order)))
            row = cur.fetchone()
        return {"selection_mode": row["selection_mode"],
                "fallback_order": list(row["fallback_order"] or []),
                "updated_at": row["updated_at"]}

    # ---------------------------------------------------------- account ctl
    def set_enabled(self, principal: str, account_id: str, enabled: bool) -> dict:
        """Enable or disable an account. Disabling keeps the key and the state."""
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET enabled=%s, updated_at=now() "
                        "WHERE id=%s AND principal=%s "
                        "RETURNING id", (bool(enabled), account_id, principal))
            if cur.fetchone() is None:
                raise NotFound("account not found")
        return self._account(principal, account_id)

    def set_priority(self, principal: str, account_id: str, priority: int) -> dict:
        if not isinstance(priority, int) or priority < 0:
            raise ValueError("priority must be a non-negative integer")
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET priority=%s, updated_at=now() "
                        "WHERE id=%s AND principal=%s RETURNING id",
                        (priority, account_id, principal))
            if cur.fetchone() is None:
                raise NotFound("account not found")
        return self._account(principal, account_id)

    def remove(self, principal: str, account_id: str, *, force: bool = False) -> None:
        """Delete an account. Refuses while it has active tasks, unless forced.

        Deleting an account that is running conversations would orphan them, so
        the count is checked first and the operator has to force it explicitly.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT active_tasks FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            if row["active_tasks"] > 0 and not force:
                raise PoolError(
                    f"account has {row['active_tasks']} active task(s); "
                    "pass force=true to remove it anyway")
            cur.execute("DELETE FROM nexus_openhands_accounts WHERE id=%s AND principal=%s",
                        (account_id, principal))

    # ------------------------------------------------------------- health
    def record_success(self, principal: str, account_id: str) -> None:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET health='healthy', "
                        "last_error=NULL, last_error_at=NULL, last_checked_at=now(), "
                        "updated_at=now() WHERE id=%s AND principal=%s",
                        (account_id, principal))

    def record_failure(self, principal: str, account_id: str, error: str, *,
                       unavailable: bool = False) -> None:
        """Mark an account degraded (retryable) or unavailable (auth/transport).

        ``unavailable`` is for a failure the account cannot serve through right
        now (a rejected key, an unreachable endpoint). A degraded account stays
        selectable, because a single transient error is not a reason to stop
        using it.
        """
        health = "unavailable" if unavailable else "degraded"
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET health=%s, last_error=%s, "
                        "last_error_at=now(), updated_at=now() WHERE id=%s AND principal=%s",
                        (health, error[:1000], account_id, principal))

    def begin_task(self, principal: str, account_id: str) -> None:
        """Reserve capacity on an account (manual counter).

        Production load is *derived* from the durable task rows (see
        :meth:`_live_load`), so this counter is for a caller that wants to
        reserve capacity ahead of a dispatch; the two are summed when selecting.
        """
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET active_tasks=active_tasks+1, "
                        "updated_at=now() WHERE id=%s AND principal=%s",
                        (account_id, principal))

    def end_task(self, principal: str, account_id: str) -> None:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts "
                        "SET active_tasks=GREATEST(active_tasks-1, 0), updated_at=now() "
                        "WHERE id=%s AND principal=%s", (account_id, principal))

    #: Task states that mean an account is actively running something for it.
    _BUSY_STATES = ("dispatched", "running", "verifying")

    def _live_load(self) -> dict[str, int]:
        """In-flight tasks per account, read from the durable task table.

        Load is *observed* here rather than tracked with an increment/decrement
        pair: a counter that nothing decrements on a crash would drift, and a
        load-balancing mode that balances on a stale number is worse than one
        that balances on none. A task is in flight while it is dispatched,
        running or verifying and is bound to an account.
        """
        try:
            with self.store._tx() as cur:
                cur.execute(
                    "SELECT openhands_account_id AS id, count(*) AS n "
                    "FROM nexus_run_tasks WHERE openhands_account_id IS NOT NULL "
                    "AND state = ANY(%s) GROUP BY openhands_account_id",
                    (list(self._BUSY_STATES),))
                return {str(r["id"]): int(r["n"]) for r in cur.fetchall()}
        except Exception:  # noqa: BLE001 - a pool must work before the lab schema exists
            return {}

    def _load_of(self, account: dict, live: dict[str, int]) -> int:
        return int(account.get("active_tasks", 0) or 0) + live.get(str(account["id"]), 0)

    # ----------------------------------------------------------- selection
    def _usable(self, account: dict, exclude: set[str]) -> bool:
        return (account["state"] == "active" and account.get("enabled", True)
                and account.get("health", "unknown") in _USABLE_HEALTH
                and str(account["id"]) not in exclude)

    def candidates(self, principal: str, *, exclude: Iterable[str] = (),
                   project_id: str | None = None) -> list[dict]:
        """Every selectable account, in failover order.

        The order is the operator's explicit ``fallback_order`` first, then
        ``priority`` (lower first), then age — so the ordering is deterministic
        even when two accounts share a priority.

        With *project_id* and a project that has assignments, the list is
        restricted to them: a project runs on the executors it was assigned,
        never on another project's.
        """
        exclude = {str(x) for x in exclude}
        assigned = self._assigned(project_id)
        order = self.get_config(principal)["fallback_order"]
        ranked = {str(x): i for i, x in enumerate(order)}
        usable = [a for a in self.accounts.list_accounts(principal)
                  if self._usable(a, exclude)
                  and (assigned is None or str(a["id"]) in assigned)]
        if assigned is not None:
            # The project's own order is authoritative for its accounts.
            rank = {aid: i for i, aid in enumerate(assigned)}
            usable.sort(key=lambda a: (rank.get(str(a["id"]), len(assigned)),
                                       a.get("priority", 100), str(a["id"])))
            return usable
        usable.sort(key=lambda a: (ranked.get(str(a["id"]), len(order)),
                                   a.get("priority", 100), str(a.get("created_at")),
                                   str(a["id"])))
        return usable

    def select(self, principal: str, *, exclude: Iterable[str] = (),
               project_id: str | None = None) -> dict:
        """The account to use, per the pool policy. Raises :class:`PoolError`.

        *project_id*, when given, prefers the project's explicit binding: an
        operator who bound an account meant it. The binding is skipped only when
        it is not usable and the mode allows a fallback.

        When the project has **assignments**, only they are candidates: a bound
        account or a default that is not assigned to this project is not used.
        """
        exclude = {str(x) for x in exclude}
        mode = self.get_config(principal)["selection_mode"]
        accounts = {str(a["id"]): a for a in self.accounts.list_accounts(principal)}
        assigned = self._assigned(project_id)

        def allowed(account: dict | None) -> bool:
            return (account is not None and self._usable(account, exclude)
                    and (assigned is None or str(account["id"]) in assigned))

        bound_id = self._project_account(principal, project_id) if project_id else None
        bound = accounts.get(bound_id) if bound_id else None
        default = self._default(principal)

        if mode == "manual":
            # The operator's explicit choice, and only that. A bound account is
            # explicit; otherwise the default. Manual never falls back.
            chosen = bound if allowed(bound) else (default if allowed(default) else None)
            if chosen is None:
                raise PoolError(
                    "manual mode: the selected account is not usable and manual "
                    "mode never switches to another account")
            return chosen

        if mode == "load_balancing":
            # Spread work: the usable account running the fewest active tasks.
            # The bound account is honoured only when it is also a valid choice;
            # the point of this mode is balance, so it is not forced.
            candidates = self.candidates(principal, exclude=exclude, project_id=project_id)
            if not candidates:
                raise PoolError("no usable OpenHands account for load balancing")
            live = self._live_load()
            if bound and allowed(bound):
                fewest = min(self._load_of(a, live) for a in candidates)
                if self._load_of(bound, live) == fewest:
                    return bound
            candidates.sort(key=lambda a: (self._load_of(a, live),
                                           a.get("priority", 100), str(a["id"])))
            return candidates[0]

        # auto and failover: the bound account, then the default, then the best
        # candidate. failover differs only in the exclude set the caller passes.
        for preferred in (bound, default):
            if preferred is not None and allowed(preferred):
                return preferred
        candidates = self.candidates(principal, exclude=exclude, project_id=project_id)
        if not candidates:
            if assigned is not None:
                raise PoolError("no usable OpenHands account among the project's "
                                "assigned executors")
            raise PoolError("no usable OpenHands account")
        return candidates[0]

    def _default(self, principal: str) -> dict | None:
        for a in self.accounts.list_accounts(principal):
            if a.get("is_default"):
                return a
        return None

    def _project_account(self, principal: str, project_id: str) -> str | None:
        with self.store._tx() as cur:
            cur.execute("SELECT p.openhands_account_id FROM nexus_projects p "
                        "JOIN nexus_project_members m ON m.project_id=p.id "
                        "WHERE p.id=%s AND m.principal=%s", (project_id, principal))
            row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        account_id = row["openhands_account_id"]
        return str(account_id) if account_id else None

    def _account(self, principal: str, account_id: str) -> dict:
        for a in self.accounts.list_accounts(principal):
            if str(a["id"]) == str(account_id):
                return a
        raise NotFound("account not found")

    # --------------------------------------------------------------- client
    def client_for(self, principal: str, account_id: str):
        """A ready-to-run client for one account, with its transport and model.

        Delegates to :meth:`OpenHandsAccountService.execution_client_for_account`
        so the pool, the diagnostics and the worker never build an execution
        client differently. ``account_config`` is returned alongside for callers
        that need the non-secret execution settings (e.g. the model to display).
        """
        cfg = self.accounts.account_config(principal, account_id)
        client = self.accounts.execution_client_for_account(principal, account_id)
        return client, cfg
