"""Per-project OpenHands executor assignment: primary, fallbacks and order.

A project may name which of the principal's OpenHands accounts may run its
work, in order. This is the project-scoped allowlist the requirement asks for:
with assignments present, the pool for that project is *restricted* to them, so
one project never runs on an account another project uses unless it was assigned
the same account on purpose.

This module owns the assignment rows only. Which account is *chosen* among the
assigned ones is the pool's job (:mod:`orchestration.openhands_pool`), and the
hand-off between accounts is the router's
(:mod:`orchestration.openhands_router`). Keeping the three apart means the
"which are allowed" question has one answer, and the "which is used now" one has
another.

Rules kept here:

* at most one **primary** executor per project (a partial unique index);
* a **fallback** is an ordered list (``position``), so failover is deterministic;
* an assignment may be **disabled** without being removed (kept for later);
* every write checks the principal owns the project and owns the account, so a
  project cannot be pointed at another principal's account.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore

#: The roles an assignment may have. ``primary`` is used first; ``fallback``
#: accounts are tried in ``position`` order.
ROLES = ("primary", "fallback")


class ProjectExecutorService:
    """Assign OpenHands accounts to a project (primary + ordered fallbacks)."""

    def __init__(self, store: PostgresStore):
        self.store = store

    # --------------------------------------------------------------- reads
    def list(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._member(cur, principal, project_id)
            cur.execute(
                "SELECT po.project_id, po.account_id, po.role, po.position, po.enabled, "
                "       po.created_at, a.label, a.state, a.health, a.transport, "
                "       a.default_model, a.base_url "
                "FROM nexus_project_openhands po "
                "JOIN nexus_openhands_accounts a ON a.id = po.account_id "
                "WHERE po.project_id=%s "
                "ORDER BY (po.role='primary') DESC, po.position, po.created_at",
                (project_id,))
            return [self._view(r) for r in cur.fetchall()]

    def assigned_account_ids(self, project_id: str, *, enabled_only: bool = True
                             ) -> list[str]:
        """The accounts assigned to *project_id*, in selection order.

        No membership check: this is read by the worker, which is not a project
        member, and it returns ids only (never a secret). An empty list means
        "no assignment" - the caller keeps the pre-existing behaviour.
        """
        with self.store._tx() as cur:
            sql = "SELECT account_id FROM nexus_project_openhands WHERE project_id=%s"
            if enabled_only:
                sql += " AND enabled"
            sql += " ORDER BY (role='primary') DESC, position, created_at"
            cur.execute(sql, (project_id,))
            return [str(r["account_id"]) for r in cur.fetchall()]

    # -------------------------------------------------------------- writes
    def set_executor(self, principal: str, project_id: str, account_id: str, *,
                     role: str = "fallback", position: int | None = None) -> dict:
        """Assign (or re-assign) one account to a project.

        Assigning an account that is already the primary moves it to the new
        role, so the "at most one primary" rule is kept by the database, not by
        a client. *position* defaults to the end of the fallback list.
        """
        if role not in ROLES:
            raise ValueError("role must be 'primary' or 'fallback'")
        with self.store._tx() as cur:
            self._owner(cur, principal, project_id)
            self._account(cur, principal, account_id)
            if role == "primary":
                # Demote any current primary first: the unique index forbids two.
                cur.execute("UPDATE nexus_project_openhands SET role='fallback', "
                            "updated_at=now() WHERE project_id=%s AND role='primary' "
                            "AND account_id<>%s", (project_id, account_id))
            if position is None:
                cur.execute("SELECT COALESCE(max(position), -1) + 1 AS next "
                            "FROM nexus_project_openhands WHERE project_id=%s",
                            (project_id,))
                position = int(cur.fetchone()["next"])
            cur.execute(
                "INSERT INTO nexus_project_openhands(project_id, account_id, role, "
                "  position, enabled, created_by) VALUES (%s,%s,%s,%s,true,%s) "
                "ON CONFLICT (project_id, account_id) DO UPDATE SET "
                "  role=EXCLUDED.role, position=EXCLUDED.position, enabled=true, "
                "  updated_at=now() RETURNING project_id, account_id, role, position, enabled",
                (project_id, account_id, role, position, principal))
            row = cur.fetchone()
            self._event(cur, project_id, principal, "project.executor_assigned", account_id)
            return self._view(row)

    def set_enabled(self, principal: str, project_id: str, account_id: str,
                    enabled: bool) -> dict:
        with self.store._tx() as cur:
            self._owner(cur, principal, project_id)
            cur.execute("UPDATE nexus_project_openhands SET enabled=%s, updated_at=now() "
                        "WHERE project_id=%s AND account_id=%s "
                        "RETURNING project_id, account_id, role, position, enabled",
                        (bool(enabled), project_id, account_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("executor not assigned to this project")
            return self._view(row)

    def remove(self, principal: str, project_id: str, account_id: str) -> None:
        with self.store._tx() as cur:
            self._owner(cur, principal, project_id)
            cur.execute("DELETE FROM nexus_project_openhands "
                        "WHERE project_id=%s AND account_id=%s",
                        (project_id, account_id))
            if cur.rowcount == 0:
                raise NotFound("executor not assigned to this project")
            self._event(cur, project_id, principal, "project.executor_removed", account_id)

    def replace_all(self, principal: str, project_id: str, *,
                    primary: str | None, fallbacks: list[str]) -> list[dict]:
        """Set the whole assignment in one action: one primary, ordered fallbacks.

        Used by the project settings screen so the operator's list is applied as
        a whole (and a primary is never left unset by an intermediate state).
        Duplicates are refused, and the primary must not also be a fallback.
        """
        fallbacks = [str(x) for x in fallbacks]
        if len(set(fallbacks)) != len(fallbacks):
            raise ValueError("a fallback appears twice")
        if primary is not None and primary in fallbacks:
            raise ValueError("the primary executor must not also be a fallback")
        with self.store._tx() as cur:
            self._owner(cur, principal, project_id)
            ids = ([primary] if primary else []) + fallbacks
            for account_id in ids:
                self._account(cur, principal, account_id)
            cur.execute("DELETE FROM nexus_project_openhands WHERE project_id=%s",
                        (project_id,))
            if primary:
                cur.execute(
                    "INSERT INTO nexus_project_openhands(project_id, account_id, role, "
                    "  position, created_by) VALUES (%s,%s,'primary',0,%s)",
                    (project_id, primary, principal))
            for position, account_id in enumerate(fallbacks):
                cur.execute(
                    "INSERT INTO nexus_project_openhands(project_id, account_id, role, "
                    "  position, created_by) VALUES (%s,%s,'fallback',%s,%s)",
                    (project_id, account_id, position + 1, principal))
            # ``resource_id`` is NOT NULL in the events table; the project is the
            # resource a whole-assignment change belongs to.
            self._event(cur, project_id, principal, "project.executors_replaced",
                        project_id)
        return self.list(principal, project_id)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _view(row: Any) -> dict:
        out = dict(row)
        for key in ("project_id", "account_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

    @staticmethod
    def _member(cur: Any, principal: str, project_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_project_members WHERE project_id=%s AND principal=%s",
                    (project_id, principal))
        if cur.fetchone() is None:
            raise NotFound("project not found")

    @staticmethod
    def _owner(cur: Any, principal: str, project_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_project_members "
                    "WHERE project_id=%s AND principal=%s AND role='owner'",
                    (project_id, principal))
        if cur.fetchone() is None:
            raise NotFound("project not found")

    @staticmethod
    def _account(cur: Any, principal: str, account_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_openhands_accounts "
                    "WHERE id=%s AND principal=%s", (account_id, principal))
        if cur.fetchone() is None:
            raise NotFound("OpenHands account not found")

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str,
               resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))
