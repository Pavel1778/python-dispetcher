"""Which OpenHands connections a project runs its tasks on.

A project may be given its own list of executors: one connection, or several
in order. The first is the **primary**; the rest are **backups**, tried in
that order when those before them cannot take work. The list is the whole of
what the project may use:

* a project **with** a list uses only the connections on it (and only those
  switched on for it). Another project's connections are never chosen for it,
  however healthy they are and however stuck its own;
* a project **without** a list keeps the rule from before lists existed: the
  account pool of its owner, or the server's own ``OPENHANDS_API_KEY`` when
  the owner has stored no connection.

Two strategies:

* ``failover`` (the default) - the first usable executor in order. A backup
  serves only while the ones before it are down, and work goes back to the
  primary as soon as it is usable again; a task that is already running stays
  where it is, because a conversation cannot move between connections.
* ``distribute`` - the usable executor with the fewest tasks at work, so that
  tasks which do not depend on each other run side by side on different
  connections. What may run side by side at all is decided elsewhere: by the
  plan's dependencies and by "one task per branch" (``nexus_lab_store``).

Only an owner of the project changes its list, and only with connections of
their own. A member may read it. The key of a connection is never part of
what this module returns.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_openhands_accounts import OpenHandsAccountService, _renumber

STRATEGIES = ("failover", "distribute")
#: More executors than this on one project is a configuration mistake, not a need.
MAX_EXECUTORS = 20

_ACCOUNT = ("a.id, a.label, a.base_url, a.transport, a.default_model, a.state, a.enabled, "
            "a.health, a.last_error, a.last_checked_at, a.health_checked_at, a.api_version, "
            "a.compatibility, a.compatibility_detail, a.principal, a.priority, "
            "a.created_at, a.active_tasks, a.is_default")


class ProjectExecutorService:
    """Read and change a project's executors; check them."""

    def __init__(self, store: PostgresStore, accounts: OpenHandsAccountService):
        self.store = store
        self.accounts = accounts

    # ---------------------------------------------------------------- reading
    def get(self, principal: str, project_id: str) -> dict:
        """The project's executors in order, its strategy, and the caller's
        connections that are not on the list (what could be added)."""
        with self.store._tx() as cur:
            role = self._role(cur, principal, project_id)
            cur.execute("SELECT executor_strategy FROM nexus_projects WHERE id=%s",
                        (project_id,))
            strategy = cur.fetchone()["executor_strategy"]
            executors = self._assigned(cur, project_id)
        taken = {item["account_id"] for item in executors}
        available = [
            {"account_id": a["id"], "label": a["label"], "state": a["state"],
             "enabled": a["enabled"], "health": a["health"], "base_url": a["base_url"],
             "transport": a["transport"]}
            for a in self.accounts.list_accounts(principal) if a["id"] not in taken]
        for item in executors:
            item.pop("principal", None)
            # As the interface sees it: ``enabled`` is the project's own switch
            # for this executor; the connection's is ``connection_enabled``.
            item["connection_enabled"] = item.pop("enabled")
            item["enabled"] = item.pop("project_enabled")
        return {"project_id": project_id, "strategy": strategy, "executors": executors,
                "available": available, "can_edit": role == "owner",
                # With no list the project runs under the older rule.
                "mode": "list" if executors else "default"}

    def assigned(self, project_id: str) -> tuple[str, list[dict]]:
        """``(strategy, executors)`` for the loop, which is not a member.
        Each executor carries its connection's standing and its owner."""
        with self.store._tx() as cur:
            cur.execute("SELECT executor_strategy FROM nexus_projects WHERE id=%s",
                        (project_id,))
            row = cur.fetchone()
            if row is None:
                return "failover", []
            return row["executor_strategy"], self._assigned(cur, project_id)

    @staticmethod
    def _assigned(cur: Any, project_id: str) -> list[dict]:
        cur.execute(
            f"SELECT e.position, e.enabled AS project_enabled, {_ACCOUNT} "
            "FROM nexus_project_executors e "
            "JOIN nexus_openhands_accounts a ON a.id = e.account_id "
            "WHERE e.project_id=%s ORDER BY e.position", (project_id,))
        out = []
        for row in cur.fetchall():
            item = dict(row)
            item["id"] = item["account_id"] = str(item["id"])
            item["role"] = "primary" if item["position"] == 0 else "fallback"
            # ``enabled`` stays the connection's own switch; the project's is
            # ``project_enabled``. An executor takes work only when both are on.
            item["usable"] = bool(
                item["project_enabled"] and item["enabled"] and item["state"] == "active"
                and item["health"] != "unavailable")
            out.append(item)
        return out

    # --------------------------------------------------------------- changing
    def set(self, principal: str, project_id: str, *, executors: Any = None,
            strategy: Any = None) -> dict:
        """Replace the project's list and/or its strategy, as one change.

        *executors* is the whole new list, in order: ``[{"account_id": ...,
        "enabled": true}]`` (a bare id is taken as enabled). The first entry is
        the primary. An empty list returns the project to the default rule.
        Every connection named must be the caller's own and not revoked; one
        that is at work for this project is not taken off the list - its tasks
        would be left on an executor the project may no longer use.
        """
        if strategy is not None and strategy not in STRATEGIES:
            raise ValueError("strategy must be failover or distribute")
        wanted: list[tuple[str, bool]] | None = None
        if executors is not None:
            if not isinstance(executors, list) or len(executors) > MAX_EXECUTORS:
                raise ValueError(f"executors must be a list of at most {MAX_EXECUTORS}")
            wanted = []
            for item in executors:
                if isinstance(item, str):
                    account_id, enabled = item, True
                elif isinstance(item, dict) and isinstance(item.get("account_id"), str):
                    account_id, enabled = item["account_id"], item.get("enabled", True)
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled must be true or false")
                else:
                    raise ValueError("each executor must name an account_id")
                if any(account_id == seen for seen, _ in wanted):
                    raise ValueError("a connection can be on the list only once")
                wanted.append((account_id, enabled))
        with self.store._tx() as cur:
            if self._role(cur, principal, project_id) != "owner":
                raise NotFound("project not found")
            cur.execute("SELECT id FROM nexus_projects WHERE id=%s FOR UPDATE", (project_id,))
            if strategy is not None:
                cur.execute("UPDATE nexus_projects SET executor_strategy=%s, updated_at=now() "
                            "WHERE id=%s", (strategy, project_id))
            if wanted is not None:
                ids = [account_id for account_id, _ in wanted]
                if ids:
                    cur.execute("SELECT id, state FROM nexus_openhands_accounts "
                                "WHERE id = ANY(%s::uuid[]) AND principal=%s", (ids, principal))
                    found = {str(r["id"]): r["state"] for r in cur.fetchall()}
                    for account_id in ids:
                        if account_id not in found:
                            raise NotFound("connection not found")
                        if found[account_id] == "revoked":
                            raise ValueError("a connection that is switched off cannot be "
                                             "an executor; switch it on first")
                cur.execute(
                    "SELECT DISTINCT e.account_id FROM nexus_project_executors e "
                    "JOIN nexus_run_tasks t ON t.openhands_account_id = e.account_id "
                    "WHERE e.project_id=%s AND t.project_id=%s "
                    "AND t.state IN ('dispatched','running','verifying') "
                    "AND NOT (e.account_id = ANY(%s::uuid[]))", (project_id, project_id, ids))
                busy = [str(r["account_id"]) for r in cur.fetchall()]
                if busy:
                    raise ValueError(
                        "a connection that is running a task of this project cannot be "
                        "taken off its list; wait for the task or cancel its run")
                cur.execute("DELETE FROM nexus_project_executors WHERE project_id=%s",
                            (project_id,))
                for position, (account_id, enabled) in enumerate(wanted):
                    cur.execute(
                        "INSERT INTO nexus_project_executors(project_id, account_id, position, "
                        "  enabled, created_by) VALUES (%s,%s,%s,%s,%s)",
                        (project_id, account_id, position, enabled, principal))
                _renumber(cur, project_id)
            cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                        "VALUES (%s,%s,'project.executors_changed',%s)",
                        (project_id, principal, project_id))
        return self.get(principal, project_id)

    def _entries(self, principal: str, project_id: str) -> list[dict]:
        """The list as ``set`` takes it, read as an owner."""
        with self.store._tx() as cur:
            if self._role(cur, principal, project_id) != "owner":
                raise NotFound("project not found")
            return [{"account_id": item["account_id"], "enabled": item["project_enabled"]}
                    for item in self._assigned(cur, project_id)]

    def set_enabled(self, principal: str, project_id: str, account_id: str,
                    enabled: bool) -> dict:
        """Switch one executor on or off for this project only."""
        entries = self._entries(principal, project_id)
        if not any(e["account_id"] == account_id for e in entries):
            raise NotFound("the connection is not an executor of this project")
        for entry in entries:
            if entry["account_id"] == account_id:
                entry["enabled"] = bool(enabled)
        return self.set(principal, project_id, executors=entries)

    def remove(self, principal: str, project_id: str, account_id: str) -> dict:
        """Take one executor off the project's list; the rest keep their order."""
        entries = self._entries(principal, project_id)
        if not any(e["account_id"] == account_id for e in entries):
            raise NotFound("the connection is not an executor of this project")
        return self.set(principal, project_id,
                        executors=[e for e in entries if e["account_id"] != account_id])

    def make_primary(self, principal: str, project_id: str, account_id: str) -> dict:
        """Put one executor first; the others follow in the order they had.
        A connection that is not on the list yet is added."""
        entries = self._entries(principal, project_id)
        chosen = next((e for e in entries if e["account_id"] == account_id),
                      {"account_id": account_id, "enabled": True})
        return self.set(principal, project_id,
                        executors=[chosen] + [e for e in entries
                                              if e["account_id"] != account_id])

    def check(self, principal: str, project_id: str) -> dict:
        """Check every executor of the project now, each with its own key, and
        return the list as it stands afterwards. A connection of another member
        is reported as it was last seen; only its owner can check it."""
        with self.store._tx() as cur:
            self._role(cur, principal, project_id)
            assigned = self._assigned(cur, project_id)
        checked, skipped = [], []
        for item in assigned:
            if item["principal"] != principal or item["state"] == "revoked":
                skipped.append(item["account_id"])
                continue
            self.accounts.verify(principal, item["account_id"])
            checked.append(item["account_id"])
        out = self.get(principal, project_id)
        out["checked"], out["skipped"] = checked, skipped
        return out

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _role(cur: Any, principal: str, project_id: str) -> str:
        cur.execute("SELECT role FROM nexus_project_members WHERE project_id=%s AND principal=%s",
                    (project_id, principal))
        row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        return row["role"]
