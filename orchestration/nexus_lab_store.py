"""Durable storage for autonomous runs, run tasks and notifications.

This is the persistence half of the autonomous lab. The scheduler
(``nexus_lab.py``) decides *what* to do; this module is the only place that
writes run/task state, so the invariants live in one spot:

* **One active run per goal.** Enforced by a partial unique index; a second
  start raises instead of silently forking the goal.
* **Atomic claim with a lease.** ``claim_task`` uses ``FOR UPDATE SKIP LOCKED``
  plus a lease, mirroring the legacy dispatcher, so two workers never run the
  same task.
* **A task is only claimable when every dependency is `completed_verified`.**
  A blocked or unverified dependency keeps the child ``pending``.
* **Verification is explicit.** ``record_verification`` sets the terminal task
  state from a *verifier's* verdict, never from the agent's own text.

Membership is checked in the same query that touches a row.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import ProjectService

#: Task states that count as "done well enough to unblock a child".
VERIFIED_STATES = frozenset({"completed_verified"})

#: Task states that are terminal (no further work without a new attempt).
TERMINAL_TASK_STATES = frozenset({
    "completed_verified", "completed_unverified", "partial", "failed", "blocked",
})

#: Run states that are still active (the loop should keep servicing them).
ACTIVE_RUN_STATES = frozenset({"running", "paused", "waiting_for_approval", "blocked"})


def _jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


class LabStore:
    """Runs, run tasks and notifications over the shared PostgreSQL store."""

    def __init__(self, store: PostgresStore):
        self.store = store

    # ------------------------------------------------------------------ runs
    def start_run(self, principal: str, project_id: str, goal_id: str, *,
                  max_iterations: int = 50, max_attempts_per_task: int = 3,
                  deadline_seconds: int | None = None) -> dict:
        """Create the single active run for a goal. Refuses a second one."""
        if max_iterations <= 0 or max_attempts_per_task <= 0:
            raise ValueError("budgets must be positive")
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
                    if deadline_seconds else None)
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT id, state FROM nexus_goals WHERE id=%s AND project_id=%s FOR UPDATE",
                        (goal_id, project_id))
            goal = cur.fetchone()
            if goal is None:
                raise NotFound("goal not found")
            cur.execute("SELECT id FROM nexus_runs WHERE goal_id=%s AND state = ANY(%s)",
                        (goal_id, list(ACTIVE_RUN_STATES)))
            if cur.fetchone() is not None:
                raise ValueError("goal already has an active run")
            cur.execute(
                "INSERT INTO nexus_runs(project_id, goal_id, max_iterations, "
                "max_attempts_per_task, deadline_at, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (project_id, goal_id, max_iterations, max_attempts_per_task,
                 deadline, principal))
            run = dict(cur.fetchone())
            cur.execute("UPDATE nexus_goals SET state='running', active_run_id=%s, updated_at=now() "
                        "WHERE id=%s", (run["id"], goal_id))
            ProjectService._event(cur, project_id, principal, "run.started", run["id"])
            self._notify(cur, project_id, "goal_started", "Цель запущена", run["id"])
            return run

    def get_run(self, principal: str, project_id: str, run_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s AND project_id=%s",
                        (run_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return dict(row)

    def list_runs(self, principal: str, project_id: str, *, limit: int = 50) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_runs WHERE project_id=%s "
                        "ORDER BY created_at DESC, id LIMIT %s", (project_id, limit))
            return [dict(r) for r in cur.fetchall()]

    def set_run_state(self, principal: str, project_id: str, run_id: str, state: str,
                      *, last_error: str | None = None) -> dict:
        if state not in ("running", "paused", "waiting_for_approval", "blocked",
                         "completed", "failed", "cancelled"):
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            return self._apply_run_state(cur, project_id, run_id, state, last_error)

    def set_run_state_internal(self, run_id: str, state: str,
                               *, last_error: str | None = None) -> dict:
        """State transition by the scheduler itself, not a user request.

        The worker is not a project member, so it cannot pass the membership
        check a user request must. It is still not a bypass of *authorization*:
        the run must exist, and only the scheduler calls this.
        """
        if state not in ("running", "paused", "waiting_for_approval", "blocked",
                         "completed", "failed", "cancelled"):
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return self._apply_run_state(cur, str(row["project_id"]), run_id, state, last_error)

    def _apply_run_state(self, cur: Any, project_id: str, run_id: str, state: str,
                         last_error: str | None) -> dict:
        cur.execute("UPDATE nexus_runs SET state=%s, last_error=COALESCE(%s, last_error), "
                    "updated_at=now() WHERE id=%s AND project_id=%s RETURNING *",
                    (state, last_error, run_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise NotFound("run not found")
        run = dict(row)
        # Mirror terminal states onto the goal so the goal is not left
        # "running" after its only run finished.
        if state in ("completed", "failed", "cancelled"):
            cur.execute("UPDATE nexus_goals SET state=%s, updated_at=now() WHERE id=%s",
                        (state, run["goal_id"]))
            self._notify(cur, project_id, f"goal_{state}", f"Цель: {state}", run_id)
        return run

    def bump_iteration(self, run_id: str) -> int:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_runs SET iteration=iteration+1, updated_at=now() "
                        "WHERE id=%s RETURNING iteration", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return row["iteration"]

    # -------------------------------------------------------------- run tasks
    def add_task(self, principal: str, project_id: str, run_id: str, *,
                 task_key: str, title: str, description: str = "",
                 acceptance: list[str] | None = None, dependencies: list[str] | None = None,
                 repository: str = "", branch: str = "", timeout_seconds: int = 1800,
                 max_attempts: int = 3, executor: str = "openhands") -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s AND project_id=%s",
                        (run_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("run not found")
            cur.execute(
                "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, description, "
                "acceptance, dependencies, repository, branch, timeout_seconds, max_attempts, executor) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (run_id, project_id, task_key, title, description, _jsonb(acceptance or []),
                 _jsonb(dependencies or []), repository, branch, timeout_seconds,
                 max_attempts, executor))
            return self._task_row(cur.fetchone())

    def list_tasks(self, principal: str, project_id: str, run_id: str) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s AND project_id=%s "
                        "ORDER BY created_at, id", (run_id, project_id))
            return [self._task_row(r) for r in cur.fetchall()]

    def get_task(self, principal: str, project_id: str, task_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s AND project_id=%s",
                        (task_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    @staticmethod
    def _task_row(row: Any) -> dict:
        out = dict(row)
        out["acceptance"] = _loads(out.get("acceptance")) or []
        out["dependencies"] = _loads(out.get("dependencies")) or []
        out["evidence"] = _loads(out.get("evidence")) or []
        out["verification"] = _loads(out.get("verification"))
        return out

    def dependency_states(self, cur: Any, task: dict) -> dict[str, str]:
        """Map each dependency key to its current state within the same run."""
        deps = task["dependencies"] or []
        if not deps:
            return {}
        cur.execute("SELECT task_key, state FROM nexus_run_tasks WHERE run_id=%s AND task_key = ANY(%s)",
                    (task["run_id"], list(deps)))
        return {r["task_key"]: r["state"] for r in cur.fetchall()}

    def claim_task(self, run_id: str, worker_id: str, *, lease_seconds: int = 900) -> dict | None:
        """Atomically claim the next claimable task of a run, or ``None``.

        A task is claimable when it is ``pending`` with attempts left, no live
        lease, and **every dependency is completed_verified**. Claimability is
        computed in SQL and the row is locked with ``FOR UPDATE SKIP LOCKED``
        one at a time, so N workers claim N distinct tasks rather than one
        worker locking the whole queue (the bug the concurrency test caught).
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT t.* FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state='pending' AND t.attempt < t.max_attempts "
                "AND (t.lease_expires IS NULL OR t.lease_expires < now()) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) "
                "  WHERE NOT EXISTS ("
                "    SELECT 1 FROM nexus_run_tasks dep "
                "    WHERE dep.run_id = t.run_id AND dep.task_key = d.dep "
                "      AND dep.state = 'completed_verified')) "
                "ORDER BY t.created_at, t.id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (run_id,))
            row = cur.fetchone()
            if row is None:
                return None
            task = self._task_row(row)
            cur.execute(
                "UPDATE nexus_run_tasks SET state='dispatched', attempt=attempt+1, "
                "lease_expires=now() + (%s || ' seconds')::interval, updated_at=now() "
                "WHERE id=%s RETURNING *",
                (lease_seconds, task["id"]))
            return self._task_row(cur.fetchone())

    def mark_running(self, task_id: str, *, conversation_id: str | None = None) -> dict:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET state='running', "
                        "conversation_id=COALESCE(%s, conversation_id), updated_at=now() "
                        "WHERE id=%s RETURNING *", (conversation_id, task_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def record_verification(self, task_id: str, *, verdict: str, evidence: dict,
                            detail: dict | None = None) -> dict:
        """Set the terminal task state from a verifier verdict.

        ``verdict`` is one of the five task outcomes. Only
        ``completed_verified`` unblocks children.
        """
        if verdict not in ("completed_verified", "completed_unverified",
                           "partial", "failed", "blocked"):
            raise ValueError("invalid verdict")
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, verification=%s, "
                "evidence = evidence || %s::jsonb, lease_expires=NULL, updated_at=now() "
                "WHERE id=%s RETURNING *",
                (verdict, _jsonb(detail or {}), _jsonb([evidence]), task_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            task = self._task_row(row)
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (task["run_id"],))
            project_id = cur.fetchone()["project_id"]
            self._notify(cur, project_id, f"task_{verdict}", task["title"], task["id"])
            return task

    def release_task(self, task_id: str, *, error: str, requeue: bool) -> dict:
        """Return a claimed task to ``pending`` (requeue) or mark it ``failed``."""
        state = "pending" if requeue else "failed"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, lease_expires=NULL, last_error=%s, "
                "updated_at=now() WHERE id=%s RETURNING *", (state, error, task_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def reclaim_expired(self, run_id: str) -> list[dict]:
        """Requeue tasks whose lease expired (a worker died mid-task).

        A task that has exhausted its attempts is marked ``failed`` instead of
        being requeued forever.
        """
        out = []
        with self.store._tx() as cur:
            cur.execute(
                "SELECT * FROM nexus_run_tasks WHERE run_id=%s AND state IN ('dispatched','running') "
                "AND lease_expires IS NOT NULL AND lease_expires < now() FOR UPDATE",
                (run_id,))
            for row in cur.fetchall():
                task = self._task_row(row)
                if task["attempt"] >= task["max_attempts"]:
                    cur.execute("UPDATE nexus_run_tasks SET state='failed', lease_expires=NULL, "
                                "last_error='lease expired after max attempts', updated_at=now() "
                                "WHERE id=%s RETURNING *", (task["id"],))
                else:
                    cur.execute("UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
                                "last_error='lease expired', updated_at=now() WHERE id=%s RETURNING *",
                                (task["id"],))
                out.append(self._task_row(cur.fetchone()))
            return out

    def run_progress(self, cur: Any, run_id: str) -> dict[str, int]:
        cur.execute("SELECT state, count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                    "GROUP BY state", (run_id,))
        return {r["state"]: r["n"] for r in cur.fetchall()}

    def all_verified(self, run_id: str) -> bool:
        with self.store._tx() as cur:
            cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                        "AND state <> 'completed_verified'", (run_id,))
            return cur.fetchone()["n"] == 0

    def has_pending(self, run_id: str) -> bool:
        with self.store._tx() as cur:
            cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                        "AND state IN ('pending','dispatched','running','verifying')", (run_id,))
            return cur.fetchone()["n"] > 0

    def has_in_flight(self, run_id: str) -> bool:
        """Whether any task is dispatched/running/verifying right now."""
        with self.store._tx() as cur:
            cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                        "AND state IN ('dispatched','running','verifying')", (run_id,))
            return cur.fetchone()["n"] > 0

    def has_claimable(self, run_id: str) -> bool:
        """Whether any pending task could be claimed right now.

        Same predicate as ``claim_task`` (attempts left, no live lease, every
        dependency completed_verified), without taking a lock. Used to tell a
        run that is *waiting* from one that is *deadlocked*: a pending task
        whose dependency ended `completed_unverified`/`failed`/`blocked` can
        never become claimable.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state='pending' AND t.attempt < t.max_attempts "
                "AND (t.lease_expires IS NULL OR t.lease_expires < now()) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) "
                "  WHERE NOT EXISTS ("
                "    SELECT 1 FROM nexus_run_tasks dep "
                "    WHERE dep.run_id = t.run_id AND dep.task_key = d.dep "
                "      AND dep.state = 'completed_verified'))", (run_id,))
            return cur.fetchone()["n"] > 0

    # ----------------------------------------------------------- notifications
    @staticmethod
    def _notify(cur: Any, project_id: Any, kind: str, title: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_notifications(project_id, kind, title, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, kind, title[:200], resource_id))

    def notifications(self, principal: str, project_id: str, *, limit: int = 50) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT id, kind, title, body, resource_id, created_at, read_at "
                        "FROM nexus_notifications WHERE project_id=%s "
                        "ORDER BY created_at DESC, id LIMIT %s", (project_id, limit))
            return [dict(r) for r in cur.fetchall()]
