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
* **Only a running run starts work.** ``claim_task`` checks the run's state, its
  iteration budget and its deadline in the same transaction that claims the
  task, so a pause or a cancel cannot be overtaken by a claim already on its way.
* **No run is open-ended.** A run that names no deadline gets
  ``DEFAULT_DEADLINE_SECONDS``. A run nobody served - no executor was running -
  fails when that time is up; it does not spring to life days later.
* **Run states follow a table** (``_RUN_ENTERED_FROM``). A finished or cancelled
  run cannot be brought back, and a decision the scheduler made a moment too
  late never overwrites what a person just asked for.
* **A started conversation is never abandoned for a second one.** Only a task
  that has no conversation bound is requeued when its lease runs out.
* **A conversation that is left behind is stopped, not forgotten**, and its
  branch is given to nobody else until it is. There are two records of what is
  owed, for two situations, and one way of stopping (``OpenHandsExecutor.stop``):

  - a switch or a failover of a task in a live run records the conversation it
    leaves behind in the task's ``abandoned`` list; the run's own pass stops
    each (``pending_stops`` / ``mark_abandoned_stopped``);
  - a conversation that has to stop for any other reason - its run was
    cancelled, it ran out of time or stalled, it was started by a worker that
    had lost the task - is owed a stop signal (``nexus_executor_stops``). Such
    a stop may have no live run behind it, so it is delivered by the worker
    loop, and it is over only when the executor is seen to have stopped.

  ``_BRANCH_FREE`` holds the repository and branch for both.
* **A durable run does not give a task up.** Its attempts are counted inside a
  work cycle; when they are used up the task is ``waiting`` until ``retry_at``
  and then starts the next cycle (``retry_task``, ``wait_task``). A ``bounded``
  run - one whose caller named explicit budgets - keeps the older rule: when
  the attempts or the budgets are spent, it stops.
* **A cancelled run leaves the lists, not the database** (``hidden_at``).

Membership is checked in the same query that touches a row.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .cloud_store import InvalidTransition, NotFound, PostgresStore
from .nexus_projects import ProjectService

#: Task states that count as "done well enough to unblock a child".
VERIFIED_STATES = frozenset({"completed_verified"})

#: Task states that are terminal (no further work without a new attempt).
TERMINAL_TASK_STATES = frozenset({
    "completed_verified", "completed_unverified", "partial", "failed", "blocked",
})

#: Run states that are still active (the loop should keep servicing them).
ACTIVE_RUN_STATES = frozenset({"running", "paused", "waiting_for_approval", "blocked"})

#: Run states with no way forward. A terminal run is never serviced again.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

#: The run state machine: for each state, the states it may be entered from.
#: Asking for the state a run is already in is a no-op, not an error.
_RUN_ENTERED_FROM: dict[str, frozenset[str]] = {
    "running": frozenset({"paused", "blocked"}),
    "paused": frozenset({"running"}),
    "waiting_for_approval": frozenset({"running"}),
    "blocked": frozenset({"running"}),
    "completed": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "cancelled": frozenset({"running", "paused", "waiting_for_approval", "blocked"}),
}

#: Task states that still expect work. ``waiting`` is a task that will be tried
#: again at its ``retry_at``.
UNFINISHED_TASK_STATES = ("pending", "waiting", "dispatched", "running", "verifying")

#: Task states with a conversation that may be at work.
IN_FLIGHT_TASK_STATES = ("dispatched", "running", "verifying")

#: Run policies. ``durable``: keeps going until every task verified or a person
#: cancels; budgets are per cycle and renew. ``bounded``: stops when its
#: attempts or budgets are spent (what every run did before migration 0026).
RUN_POLICIES = ("durable", "bounded")

#: "Nothing else is at work on this task's branch." One task at a time per
#: branch of a repository, across all runs and within one. A task is accepted
#: only when the head of its branch is the commit its agent reports
#: (``GitVerifier``); two agents pushing to one branch move that head under
#: each other, and whichever is read second is "not the branch head" though it
#: did its work. A task with no repository pushes nowhere and is not held.
#
#: A branch is also held while any task has an *abandoned* conversation on it:
#: an executor switch starts a new conversation while the old one may still be
#: running in its sandbox, editing the same checkout. Until the worker has
#: really stopped the old conversation (``abandoned`` drained), nobody starts a
#: second writer there. A repository-less task is never held.
#:
#: And while a conversation that was told to stop for another reason (its run
#: was cancelled, it ran out of time) has not been seen to stop: until then it
#: may push, so its branch is not handed to another task either.
_BRANCH_FREE = (
    "(t.repository = '' OR ("
    "  NOT EXISTS ("
    "    SELECT 1 FROM nexus_run_tasks o "
    "    WHERE o.repository = t.repository AND o.branch = t.branch AND o.id <> t.id "
    "      AND o.state IN ('dispatched','running','verifying')) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM nexus_run_tasks a "
    "    WHERE a.repository = t.repository AND a.branch = t.branch "
    "      AND jsonb_array_length(a.abandoned) > 0) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM nexus_executor_stops s "
    "    WHERE s.repository = t.repository AND s.branch = t.branch "
    "      AND s.state = 'requested'))) ")

#: "This task may be taken now": it is pending with no live lease, or it has
#: waited until its ``retry_at``.
_TASK_DUE = (
    "((t.state = 'pending' AND (t.lease_expires IS NULL OR t.lease_expires < now())) "
    " OR (t.state = 'waiting' AND (t.retry_at IS NULL OR t.retry_at <= now()))) ")

#: "Every task this one depends on has verified."
_DEPS_VERIFIED = (
    "NOT EXISTS ("
    "  SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) "
    "  WHERE NOT EXISTS ("
    "    SELECT 1 FROM nexus_run_tasks dep "
    "    WHERE dep.run_id = t.run_id AND dep.task_key = d.dep "
    "      AND dep.state = 'completed_verified')) ")

#: Task states a person's "resume" may send back to the queue.
_RETRYABLE_TASK_STATES = ("completed_unverified", "partial", "failed", "blocked")

#: How long one task's conversation may run before the task is given up, in
#: seconds, unless ``NEXUS_TASK_TIMEOUT_SECONDS`` says otherwise. It was thirty
#: minutes; real tasks ran longer and were recorded as failed while their
#: agents were still at work (see migration 0025).
DEFAULT_TASK_TIMEOUT_SECONDS = 7200


def task_timeout_seconds() -> int:
    """The time limit a new task is given: between a minute and a day."""
    raw = os.environ.get("NEXUS_TASK_TIMEOUT_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TASK_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TASK_TIMEOUT_SECONDS
    return min(24 * 3600, max(60, value))


#: How many tasks may be at work at once, over all runs this database holds,
#: unless ``NEXUS_LAB_MAX_ACTIVE_TASKS`` says otherwise (0 = no limit). A task
#: over the limit is not refused: it stays pending and is taken when another
#: one ends.
DEFAULT_MAX_ACTIVE_TASKS = 8


def max_active_tasks() -> int:
    raw = os.environ.get("NEXUS_LAB_MAX_ACTIVE_TASKS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_ACTIVE_TASKS
    except ValueError:
        value = DEFAULT_MAX_ACTIVE_TASKS
    return max(0, value)


#: Upper bounds for what a caller may ask of one run.
MAX_RUN_ITERATIONS = 500
MAX_TASK_ATTEMPTS = 10
MAX_DEADLINE_SECONDS = 30 * 24 * 3600

#: The deadline of a run that names none.
DEFAULT_DEADLINE_SECONDS = 24 * 3600

#: A run's deadline, in SQL. Rows written before deadlines were enforced have
#: none; they are read as if they had been given the default when they were
#: created, so an old run is bounded by the same rule as a new one.
_DEADLINE_SQL = (f"COALESCE(deadline_at, created_at + "
                 f"interval '{DEFAULT_DEADLINE_SECONDS} seconds')")


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
                  deadline_seconds: int | None = None,
                  plan: list[dict[str, Any]] | None = None,
                  repository: str = "", branch: str = "",
                  also: Callable[[Any, dict], None] | None = None,
                  policy: str = "bounded",
                  conversation_id: str | None = None,
                  repositories: dict[str, str] | None = None) -> dict:
        """Create the single active run for a goal. Refuses a second one.

        *policy* is ``durable`` or ``bounded`` (see ``RUN_POLICIES``); a caller
        that names no explicit budget asks for ``durable``. *conversation_id*
        is the chat the run was asked for in: its report is delivered there.

        *repositories* maps a repository to the branch its tasks work on, for
        a plan that spans several: a task that names a ``repository`` in that
        mapping is stored with it, any other with *repository* / *branch*.

        With *plan* (validated tasks from ``nexus_planner``) the tasks are stored
        in the same transaction as the run, so a run never exists with half of
        its plan - or with none, which would look like a run with nothing left
        to do.

        *also* is called with the cursor and the new run before the transaction
        commits, for what must become true together with the run (the
        specification it was planned from is marked planned; the chat is told).
        If it raises, the run is not created.
        """
        if not 0 < max_iterations <= MAX_RUN_ITERATIONS:
            raise ValueError(f"max_iterations must be between 1 and {MAX_RUN_ITERATIONS}")
        if not 0 < max_attempts_per_task <= MAX_TASK_ATTEMPTS:
            raise ValueError(f"max_attempts_per_task must be between 1 and {MAX_TASK_ATTEMPTS}")
        if deadline_seconds is None:
            deadline_seconds = DEFAULT_DEADLINE_SECONDS
        if not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS:
            raise ValueError(f"deadline_seconds must be between 1 and {MAX_DEADLINE_SECONDS}")
        if policy not in RUN_POLICIES:
            raise ValueError("policy must be durable or bounded")
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
                "max_attempts_per_task, deadline_at, created_by, policy, conversation_id) "
                "VALUES (%s,%s,%s,%s, now() + %s * interval '1 second', %s, %s, "
                # A conversation of another project, or one that is gone, is
                # not a place to report to.
                "  (SELECT id FROM nexus_conversations WHERE id=%s AND project_id=%s)) "
                "RETURNING *",
                (project_id, goal_id, max_iterations, max_attempts_per_task,
                 deadline_seconds, principal, policy, conversation_id, project_id))
            run = self._run_view(cur.fetchone())
            cur.execute("UPDATE nexus_goals SET state='running', active_run_id=%s, updated_at=now() "
                        "WHERE id=%s", (run["id"], goal_id))
            for position, task in enumerate(plan or []):
                named = task.get("repository") or ""
                elsewhere = bool(named) and named in (repositories or {})
                self._insert_task(
                    cur, run["id"], project_id, task_key=task["id"], title=task["title"],
                    description=task.get("description") or "",
                    acceptance=task.get("acceptance") or [],
                    dependencies=task.get("dependencies") or [],
                    repository=named if elsewhere else repository,
                    branch=(repositories or {})[named] if elsewhere else branch,
                    max_attempts=max_attempts_per_task, position=position,
                    spec_ref=task.get("spec_ref"))
            ProjectService._event(cur, project_id, principal, "run.started", run["id"])
            self._notify(cur, project_id, "goal_started", "Цель запущена", run["id"])
            if also is not None:
                also(cur, run)
            return run

    def active_run_for_goal(self, principal: str, project_id: str, goal_id: str) -> dict | None:
        """The goal's live run, if it has one. Lets a caller refuse a second
        start before it spends a planning call; ``start_run`` still decides."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_runs WHERE goal_id=%s AND project_id=%s "
                        "AND state = ANY(%s)", (goal_id, project_id, list(ACTIVE_RUN_STATES)))
            row = cur.fetchone()
            return self._run_view(row) if row else None

    def active_run_internal(self, goal_id: str) -> dict | None:
        """The goal's live run, for the planning loop (not a project member)."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_runs WHERE goal_id=%s AND state = ANY(%s)",
                        (goal_id, list(ACTIVE_RUN_STATES)))
            row = cur.fetchone()
            return self._run_view(row) if row else None

    @staticmethod
    def _run_view(row: Any) -> dict:
        """A run as callers see it: always with the deadline it is held to."""
        run = dict(row)
        if run.get("deadline_at") is None and run.get("created_at") is not None:
            run["deadline_at"] = run["created_at"] + timedelta(seconds=DEFAULT_DEADLINE_SECONDS)
        if "hidden_at" in run:
            run["hidden"] = run["hidden_at"] is not None
        if run.get("policy") == "durable":
            # A durable run is not held to a deadline; showing one would say
            # it stops then.
            run["deadline_at"] = None
        return run

    def budget_spent(self, run_id: str) -> str | None:
        """Which of the run's limits stops new work, if any.

        Read on the database's clock - the one ``claim_task`` uses - so the
        scheduler's reason and the claim's refusal can never disagree.
        """
        with self.store._tx() as cur:
            cur.execute(f"SELECT iteration, max_iterations, policy, "
                        f"{_DEADLINE_SQL} <= now() AS late "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        if row["iteration"] >= row["max_iterations"]:
            return (f"iteration budget used: {row['iteration']} of "
                    f"{row['max_iterations']} task starts")
        if row["late"] and row["policy"] != "durable":
            return "deadline passed"
        return None

    def run_row(self, run_id: str) -> dict | None:
        """The run as the scheduler sees it. No membership check: the worker is
        not a project member, and this never leaves the process."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            return self._run_view(row) if row else None

    def get_run(self, principal: str, project_id: str, run_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s AND project_id=%s",
                        (run_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return self._run_view(row)

    def list_runs(self, principal: str, project_id: str, *, limit: int = 50,
                  include_hidden: bool = False) -> list[dict]:
        """The project's runs, newest first. A run that was cancelled is hidden
        (``hidden_at``) and is listed only when *include_hidden* asks for the
        history; ``get_run`` reads it either way."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_runs WHERE project_id=%s "
                        "AND (%s OR hidden_at IS NULL) "
                        "ORDER BY created_at DESC, id LIMIT %s",
                        (project_id, bool(include_hidden), limit))
            return [self._run_view(r) for r in cur.fetchall()]

    def set_run_state(self, principal: str, project_id: str, run_id: str, state: str,
                      *, last_error: str | None = None) -> dict:
        """Move a run on a member's behalf. Raises ``InvalidTransition`` when the
        run's current state does not lead there (see ``_RUN_ENTERED_FROM``)."""
        if state not in _RUN_ENTERED_FROM:
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            run = self._apply_run_state(cur, project_id, run_id, state, last_error,
                                        strict=True, actor=principal)
            assert run is not None
            return run

    def set_hidden(self, principal: str, project_id: str, run_id: str,
                   hidden: bool) -> dict:
        """Take a finished run out of the lists, or put it back. Only a run
        that is over can be hidden: one that is at work has to stay in sight."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT state FROM nexus_runs WHERE id=%s AND project_id=%s FOR UPDATE",
                        (run_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            if hidden and row["state"] not in TERMINAL_RUN_STATES:
                raise InvalidTransition(
                    f"a {row['state']} run cannot be hidden; cancel it first")
            cur.execute("UPDATE nexus_runs SET hidden_at = CASE WHEN %s THEN "
                        "COALESCE(hidden_at, now()) ELSE NULL END WHERE id=%s RETURNING *",
                        (bool(hidden), run_id))
            run = self._run_view(cur.fetchone())
            ProjectService._event(cur, project_id, principal,
                                  "run.hidden" if hidden else "run.shown", run_id)
            return run

    def set_run_state_internal(self, run_id: str, state: str,
                               *, last_error: str | None = None,
                               also: Callable[[Any, dict], None] | None = None) -> dict | None:
        """State transition by the scheduler itself, not a user request.

        The worker is not a project member, so it cannot pass the membership
        check a user request must. It is still not a bypass of *authorization*:
        the run must exist, and only the scheduler calls this.

        Returns ``None`` when the run is no longer in a state the transition can
        start from. The scheduler decides from what it read a moment ago; if a
        person paused or cancelled the run since, their request stands.

        *also* is called with the cursor and the run once the transition is
        made, inside the same transaction: what must become true together with
        it (a completed run and its report). If it raises, the run is not moved.
        """
        if state not in _RUN_ENTERED_FROM:
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            run = self._apply_run_state(cur, str(row["project_id"]), run_id, state,
                                        last_error, strict=False)
            if run is not None and also is not None and run["state"] == state:
                also(cur, run)
            return run

    def _apply_run_state(self, cur: Any, project_id: str, run_id: str, state: str,
                         last_error: str | None, *, strict: bool,
                         actor: str | None = None) -> dict | None:
        # The run row is locked first, here and in ``claim_task``: one order for
        # everyone, so a cancel and a claim can wait for each other but never
        # deadlock.
        cur.execute("SELECT * FROM nexus_runs WHERE id=%s AND project_id=%s FOR UPDATE",
                    (run_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise NotFound("run not found")
        current = row["state"]
        if current == state:
            return self._run_view(row)
        if current not in _RUN_ENTERED_FROM[state]:
            if not strict:
                return None
            raise InvalidTransition(f"a {current} run cannot become {state}")
        if state == "running" and current == "blocked":
            # "Resume" on a blocked run means "try again". It needs something to
            # try: tasks that did not verify and still have attempts left. In a
            # durable run a person's "resume" is also a new work cycle for a
            # task that has used its attempts.
            if not self._requeue_retryable(cur, run_id, "resumed",
                                           new_cycle=row["policy"] == "durable"):
                raise InvalidTransition(
                    "nothing to retry: every task that did not verify has used all its attempts")
        if state in ("cancelled", "failed"):
            # Nothing of a cancelled or failed run is started or polled again,
            # so its unfinished tasks must not go on saying "pending" or
            # "running" - and a conversation that is at work for it is owed a
            # stop signal: it must not go on changing the repository.
            why = "run cancelled" if state == "cancelled" else f"run failed: {last_error or 'no reason given'}"
            cur.execute(
                "INSERT INTO nexus_executor_stops(run_id, task_id, project_id, conversation_id, "
                "  openhands_account_id, repository, branch, reason) "
                "SELECT run_id, id, project_id, conversation_id, openhands_account_id, "
                "  repository, branch, %s FROM nexus_run_tasks "
                "WHERE run_id=%s AND state = ANY(%s) AND conversation_id IS NOT NULL "
                "ON CONFLICT (conversation_id) DO NOTHING",
                (why[:200], run_id, list(IN_FLIGHT_TASK_STATES)))
            cur.execute(
                "UPDATE nexus_task_executions SET ended_at=now(), outcome=%s, reason=%s "
                "WHERE run_id=%s AND ended_at IS NULL",
                ("cancelled" if state == "cancelled" else "stopped", why[:500], run_id))
            cur.execute(
                "UPDATE nexus_run_tasks SET state='blocked', lease_expires=NULL, retry_at=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "wait_reason=NULL, resume_conversation_id=NULL, last_error=%s, updated_at=now() "
                "WHERE run_id=%s AND state = ANY(%s)",
                (why[:500], run_id, list(UNFINISHED_TASK_STATES)))
        if state == "running":
            # A run that is going again no longer has the error that stopped it,
            # and does not wait out a pause that belonged to it.
            cur.execute("UPDATE nexus_runs SET state=%s, last_error=NULL, resume_at=NULL, "
                        "wait_reason=NULL, updated_at=now() "
                        "WHERE id=%s RETURNING *", (state, run_id))
        elif state == "cancelled":
            # Cancelled is also "out of sight": the run leaves the lists the
            # moment it is cancelled, and everything it recorded stays.
            cur.execute("UPDATE nexus_runs SET state=%s, last_error=COALESCE(%s, last_error), "
                        "resume_at=NULL, wait_reason=NULL, cancelled_at=now(), cancelled_by=%s, "
                        "hidden_at=COALESCE(hidden_at, now()), updated_at=now() "
                        "WHERE id=%s RETURNING *", (state, last_error, actor, run_id))
        else:
            cur.execute("UPDATE nexus_runs SET state=%s, last_error=COALESCE(%s, last_error), "
                        "resume_at=NULL, wait_reason=NULL, updated_at=now() "
                        "WHERE id=%s RETURNING *", (state, last_error, run_id))
        run = self._run_view(cur.fetchone())
        if state == "cancelled":
            ProjectService._event(cur, project_id, actor or "system", "run.cancelled", run_id)
        # Mirror terminal states onto the goal so the goal is not left
        # "running" after its only run finished.
        if state in TERMINAL_RUN_STATES:
            cur.execute("UPDATE nexus_goals SET state=%s, updated_at=now() WHERE id=%s",
                        (state, run["goal_id"]))
            self._notify(cur, project_id, f"goal_{state}", f"Цель: {state}", run_id)
        return run

    @staticmethod
    def _requeue_retryable(cur: Any, run_id: str, why: str, *, new_cycle: bool = False) -> int:
        """Send back to the queue every task that ended without verifying and
        still has attempts left. Returns how many. The conversation of the
        attempt that is over is kept in the task's evidence, not on the task: a
        task's ``conversation_id`` always belongs to its current attempt.

        With *new_cycle* a task that has used all its attempts is taken too: it
        starts its next work cycle, with the attempts restored."""
        cur.execute(
            "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, retry_at=NULL, "
            "lease_owner=NULL, lease_token=lease_token+1, wait_reason=NULL, "
            "evidence = evidence || jsonb_build_array(jsonb_build_object("
            "  'requeued', %s::text, 'attempt', attempt, 'was', state, "
            "  'conversation_id', conversation_id)), "
            "cycle = cycle + CASE WHEN attempt >= max_attempts THEN 1 ELSE 0 END, "
            "tried_accounts = CASE WHEN attempt >= max_attempts THEN '[]'::jsonb "
            "  ELSE tried_accounts END, "
            "attempt = CASE WHEN attempt >= max_attempts THEN 0 ELSE attempt END, "
            "conversation_id=NULL, verification=NULL, updated_at=now() "
            "WHERE run_id=%s AND state = ANY(%s) AND (attempt < max_attempts OR %s)",
            (why, run_id, list(_RETRYABLE_TASK_STATES), bool(new_cycle)))
        return cur.rowcount

    # -------------------------------------------------------------- run tasks
    def add_task(self, principal: str, project_id: str, run_id: str, *,
                 task_key: str, title: str, description: str = "",
                 acceptance: list[str] | None = None, dependencies: list[str] | None = None,
                 repository: str = "", branch: str = "",
                 timeout_seconds: int | None = None,
                 max_attempts: int = 3, executor: str = "openhands") -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s AND project_id=%s",
                        (run_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("run not found")
            return self._insert_task(
                cur, run_id, project_id, task_key=task_key, title=title,
                description=description, acceptance=acceptance or [],
                dependencies=dependencies or [], repository=repository, branch=branch,
                timeout_seconds=timeout_seconds, max_attempts=max_attempts, executor=executor)

    def _insert_task(self, cur: Any, run_id: Any, project_id: str, *, task_key: str,
                     title: str, description: str = "", acceptance: list[str],
                     dependencies: list[str], repository: str = "", branch: str = "",
                     timeout_seconds: int | None = None, max_attempts: int = 3,
                     executor: str = "openhands", position: int = 0,
                     spec_ref: dict | None = None) -> dict:
        if timeout_seconds is None:
            timeout_seconds = task_timeout_seconds()
        # Tasks are listed and claimed in ``created_at`` order, and ``now()`` is
        # the same instant for every row of one transaction. *position* keeps a
        # plan stored in one go in the order it was written.
        #
        # *spec_ref* says which part of the goal's specification the task
        # covers (see migration 0021); the executor uses it to hand the agent
        # the original text.
        cur.execute(
            "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, description, "
            "acceptance, dependencies, repository, branch, timeout_seconds, max_attempts, "
            "executor, spec_ref, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
            "        now() + %s * interval '1 microsecond') RETURNING *",
            (run_id, project_id, task_key, title, description, _jsonb(acceptance),
             _jsonb(dependencies), repository, branch, timeout_seconds,
             max_attempts, executor, _jsonb(spec_ref) if spec_ref else None, position))
        return self._task_row(cur.fetchone())

    def tasks_internal(self, run_id: str) -> list[dict]:
        """Every task of a run, in plan order, for the scheduler (which is not
        a project member)."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s ORDER BY created_at, id",
                        (run_id,))
            return [self._task_row(r) for r in cur.fetchall()]

    def run_bases(self, run_id: str) -> dict[tuple[str, str], str]:
        """Per ``(repository, branch)``, the head the run's first attempt
        there started from: what "changed by this run" is measured against."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT DISTINCT ON (repository, branch) repository, branch, base_sha "
                "FROM nexus_task_executions WHERE run_id=%s AND base_sha IS NOT NULL "
                "AND repository <> '' ORDER BY repository, branch, started_at, id", (run_id,))
            return {(r["repository"], r["branch"]): r["base_sha"] for r in cur.fetchall()}

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

    def get_task_internal(self, task_id: str) -> dict | None:
        """One task by id, for the scheduler (not a project member). ``None``
        when it is gone. Used to attest a verdict just recorded; never returned
        to a caller outside the process."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
            return self._task_row(row) if row else None

    def list_tasks_internal(self, run_id: str) -> list[dict]:
        """Every task of a run, for the scheduler (not a project member)."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s "
                        "ORDER BY created_at, id", (run_id,))
            return [self._task_row(r) for r in cur.fetchall()]

    @staticmethod
    def _task_row(row: Any) -> dict:
        out = dict(row)
        out["acceptance"] = _loads(out.get("acceptance")) or []
        out["dependencies"] = _loads(out.get("dependencies")) or []
        out["evidence"] = _loads(out.get("evidence")) or []
        out["verification"] = _loads(out.get("verification"))
        out["spec_ref"] = _loads(out.get("spec_ref"))
        out["tried_accounts"] = _loads(out.get("tried_accounts")) or []
        out["pending_start"] = _loads(out.get("pending_start"))
        if out.get("openhands_account_id") is not None:
            out["openhands_account_id"] = str(out["openhands_account_id"])
        if out.get("preferred_account_id") is not None:
            out["preferred_account_id"] = str(out["preferred_account_id"])
        # The fencing token every mutation of this task is checked against. A
        # caller that read the task carries it back; an out-of-date token means
        # the task was taken away in the meantime and the write must not land.
        out["lease_token"] = int(out.get("lease_token") or 0)
        # Conversations this task left behind (a switch or a failover) that the
        # worker still has to stop for real; while the list is non-empty the
        # task's branch is held so no second writer starts there.
        out["abandoned"] = _loads(out.get("abandoned")) or []
        return out

    @staticmethod
    def _close_execution(cur: Any, task_id: Any, *, outcome: str, reason: str = "",
                         head_sha: str | None = None) -> None:
        """Write how the task's current conversation ended into its history."""
        cur.execute(
            "UPDATE nexus_task_executions SET ended_at=now(), outcome=%s, reason=%s, "
            "head_sha=COALESCE(%s, head_sha) WHERE task_id=%s AND ended_at IS NULL",
            (outcome[:60], (reason or "")[:1000], head_sha, task_id))

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

        A task is claimable when it is due (``pending`` with no live lease, or
        ``waiting`` and its ``retry_at`` has come), has attempts left, **every
        dependency is completed_verified**, and no other task is at work on its
        branch (``_BRANCH_FREE``). Claimability is computed in SQL and the row
        is locked with ``FOR UPDATE SKIP LOCKED`` one at a time, so N workers
        claim N distinct tasks rather than one worker locking the whole queue
        (the bug the concurrency test caught). Two workers that reach for one
        branch at the same moment are put in line by an advisory lock on the
        branch; the second looks again and waits.

        The run itself is part of the claim: nothing is claimed unless the run
        is ``running``, is not pausing (``resume_at``), has iterations left in
        its cycle and - for a ``bounded`` run - is inside its deadline. That is
        checked on the locked run row in this transaction, so a pause or a
        cancel that committed first always wins - there is no moment between
        "the run looked fine" and "the task was taken". Each claim uses one
        iteration: an iteration is one start of one task.

        Nothing is claimed while ``max_active_tasks()`` tasks are already at
        work in this database; the task stays where it is and is taken later.

        The claim records *worker_id* as the task's ``lease_owner`` and hands
        back a fresh ``lease_token``. Every mutation the worker makes afterwards
        is fenced on that token, so once the task is taken away (the lease
        expires, an operator switches it, the run is resumed) the old worker's
        writes match no row and are no-ops.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id FROM nexus_runs WHERE id=%s AND state='running' "
                "AND iteration < max_iterations "
                "AND (resume_at IS NULL OR resume_at <= now()) "
                f"AND (policy = 'durable' OR {_DEADLINE_SQL} > now()) FOR UPDATE", (run_id,))
            if cur.fetchone() is None:
                return None
            limit = max_active_tasks()
            if limit:
                # One claim at a time decides against the limit, so that two
                # workers cannot both take "the last place".
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('nexus-active-tasks', 0))")
                cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE state = ANY(%s)",
                            (list(IN_FLIGHT_TASK_STATES),))
                if cur.fetchone()["n"] >= limit:
                    return None
            cur.execute(
                "SELECT t.* FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.attempt < t.max_attempts "
                f"AND {_TASK_DUE} AND {_DEPS_VERIFIED} AND {_BRANCH_FREE}"
                "ORDER BY t.created_at, t.id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (run_id,))
            row = cur.fetchone()
            if row is None:
                return None
            task = self._task_row(row)
            if task.get("repository"):
                # The branch looked free to this transaction; another may be
                # taking it right now. One at a time, then look again.
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"nexus-branch:{task['repository']}#{task.get('branch') or ''}",))
                cur.execute(f"SELECT 1 FROM nexus_run_tasks t WHERE t.id=%s AND {_BRANCH_FREE}",
                            (task["id"],))
                if cur.fetchone() is None:
                    return None
            cur.execute("UPDATE nexus_runs SET iteration=iteration+1, "
                        "total_iterations=total_iterations+1, wait_reason=NULL, updated_at=now() "
                        "WHERE id=%s", (run_id,))
            cur.execute(
                "UPDATE nexus_run_tasks SET state='dispatched', attempt=attempt+1, "
                "conversation_id=NULL, retry_at=NULL, wait_reason=NULL, pending_start=NULL, "
                "lease_owner=%s, lease_token=lease_token+1, "
                "lease_expires=now() + (%s || ' seconds')::interval, updated_at=now() "
                "WHERE id=%s RETURNING *",
                (worker_id, lease_seconds, task["id"]))
            return self._task_row(cur.fetchone())

    def conversation_of(self, task_id: str) -> str | None:
        """The conversation bound to the task now, if any."""
        with self.store._tx() as cur:
            cur.execute("SELECT conversation_id FROM nexus_run_tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
            return str(row["conversation_id"]) if row and row["conversation_id"] else None

    def record_start(self, task_id: str, *, start: dict, lease_token: int) -> bool:
        """Keep the start the executor's service has accepted for this claim.

        Starting a conversation has two phases: the service takes the start and
        names a start task, and the conversation exists when the sandbox is
        ready - about a minute later. Until then the claim has nothing to show
        but the lease, and a worker replaced inside that minute used to take
        the start with it (see migration 0034). With the start task on the row,
        whoever runs next finishes the start instead of waiting out the lease.

        *start* names the ``start_task_id`` and what binding the conversation
        will need: the ``account_id`` that accepted it, the ``kind`` of the
        attempt, the ``base_sha`` it began from and the ``delivery`` record.
        The claim's token is stored with it; the record counts only while the
        task still carries that token. Returns whether it was written - it is
        not when the claim was taken away meanwhile.
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET pending_start=%s::jsonb, updated_at=now() "
                "WHERE id=%s AND lease_token=%s AND state='dispatched' "
                "AND conversation_id IS NULL",
                (_jsonb({**start, "lease_token": int(lease_token)}), task_id,
                 int(lease_token)))
            return cur.rowcount == 1

    def take_start(self, task_id: str, *, lease_token: int) -> bool:
        """Take the recorded start off the task, to deal with its failure.

        A start that failed may be seen by two workers - the one that asked,
        still waiting for it, and one that followed it. The failure is dealt
        with once: by the worker this returns True to. (Binding needs no such
        gate: binding the same conversation twice changes nothing.)
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET pending_start=NULL, updated_at=now() "
                "WHERE id=%s AND lease_token=%s AND state='dispatched' "
                "AND conversation_id IS NULL AND pending_start IS NOT NULL",
                (task_id, int(lease_token)))
            return cur.rowcount == 1

    def pending_starts(self, run_id: str) -> list[dict]:
        """Claimed tasks whose start was accepted and whose conversation is not
        bound yet - by this worker or by any other.

        Following a start is reading: the start task is asked how it stands,
        and the conversation it produced is bound once, by the first to see it
        (``mark_running`` makes the second a no-op). So a start is not lost
        with the process that asked for it, the same way a conversation is not
        (``reclaim_expired``). The lease still bounds it: a start that has
        produced nothing when the claim runs out is taken back as before.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT * FROM nexus_run_tasks WHERE run_id=%s AND state='dispatched' "
                "AND conversation_id IS NULL AND pending_start IS NOT NULL "
                "AND pending_start->>'start_task_id' <> '' "
                "AND (pending_start->>'lease_token')::bigint = lease_token "
                "AND lease_expires IS NOT NULL AND lease_expires > now() "
                "ORDER BY updated_at, id", (run_id,))
            return [self._task_row(row) for row in cur.fetchall()]

    def mark_running(self, task_id: str, *, conversation_id: str | None = None,
                     delivery: dict | None = None,
                     account_id: str | None = None,
                     base_sha: str | None = None, kind: str = "start",
                     lease_token: int | None = None) -> dict:
        """Bind the conversation that was started and mark the task running.

        The conversation id is always kept. The state changes only if the task
        is still ``dispatched``: a task settled between the claim and this call
        (its run was cancelled) stays settled - and the conversation that was
        started for it all the same is owed a stop signal, which is recorded
        here, in the same transaction.

        *lease_token* is the fencing token the claim handed out. When it is
        given, the write lands only while the task still carries that token: a
        worker whose lease expired - or whose task was switched, reclaimed or
        requeued - cannot bind a conversation onto a task another worker has
        taken. The whole update is one statement, so the token is compared and
        the write is applied in the same row lock.

        *delivery* is the executor's record of how the task's original text was
        handed to the agent (inline, or a file with the path, size and SHA-256
        that were read back). It is kept under ``spec_ref.delivery``.

        *base_sha* is the head of the task's branch when the attempt began;
        *kind* says what this conversation is to the task (``start``, a
        ``continue`` in the conversation it already had, a ``handoff`` to
        another executor, an ``adopted`` orphan). Both go into the task's
        execution history together with the executor's name as it is now.

        Binding the conversation the task already has changes nothing. A start
        that two processes followed (``pending_starts``) is bound by whichever
        saw it first; the other's call is this no-op - not a second entry in
        the history, and not a stop signal for the conversation at work.
        """
        proof = _jsonb(delivery) if delivery else None
        fence = "AND lease_token=%s" if lease_token is not None else ""
        params: list[Any] = [conversation_id, account_id, account_id, proof, proof,
                             conversation_id, conversation_id, conversation_id,
                             conversation_id, conversation_id, conversation_id,
                             conversation_id, conversation_id, base_sha, task_id]
        if lease_token is not None:
            params.append(lease_token)
        with self.store._tx() as cur:
            if conversation_id:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s FOR UPDATE", (task_id,))
                held = cur.fetchone()
                if held is not None and str(held["conversation_id"] or "") == str(conversation_id):
                    return self._task_row(held)
            cur.execute("UPDATE nexus_run_tasks SET "
                        "state = CASE WHEN state='dispatched' THEN 'running' ELSE state END, "
                        "conversation_id=COALESCE(%s, conversation_id), "
                        "openhands_account_id=COALESCE(%s, openhands_account_id), "
                        "preferred_account_id = CASE WHEN %s::uuid IS NULL THEN "
                        "  preferred_account_id ELSE NULL END, "
                        "spec_ref = CASE WHEN %s::jsonb IS NULL THEN spec_ref ELSE "
                        "  COALESCE(spec_ref, '{}'::jsonb) || jsonb_build_object("
                        "    'delivery', %s::jsonb || jsonb_build_object('at', now())) END, "
                        "started_at = CASE WHEN %s::text IS NULL THEN started_at ELSE now() END, "
                        "progress_at = CASE WHEN %s::text IS NULL THEN progress_at ELSE now() END, "
                        "progress_marker = CASE WHEN %s::text IS NULL THEN progress_marker "
                        "  ELSE NULL END, "
                        "progress_checked_at = CASE WHEN %s::text IS NULL THEN "
                        "  progress_checked_at ELSE NULL END, "
                        "refusals = CASE WHEN %s::text IS NULL THEN refusals ELSE 0 END, "
                        "resume_conversation_id = CASE WHEN %s::text IS NULL THEN "
                        "  resume_conversation_id ELSE NULL END, "
                        "resume_note = CASE WHEN %s::text IS NULL THEN resume_note ELSE NULL END, "
                        "pending_start = CASE WHEN %s::text IS NULL THEN pending_start "
                        "  ELSE NULL END, "
                        "base_sha = COALESCE(%s, base_sha), "
                        "updated_at=now() "
                        f"WHERE id=%s {fence} RETURNING *",
                        tuple(params))
            row = cur.fetchone()
            if row is None:
                # Either the task is gone, or a stale worker tried to bind a
                # conversation to a task it no longer holds. The write did not
                # land, and the task is returned as it stands. The conversation
                # was started all the same and nobody follows it: it is told to
                # stop - recorded here, delivered by the loop until confirmed.
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if row is not None and conversation_id:
                    cur.execute(
                        "INSERT INTO nexus_executor_stops(run_id, task_id, project_id, "
                        "  conversation_id, openhands_account_id, repository, branch, reason) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (conversation_id) DO NOTHING",
                        (row["run_id"], task_id, row["project_id"], str(conversation_id),
                         account_id, row["repository"] or "", row["branch"] or "",
                         "started by a worker that had lost the task meanwhile"))
                    return self._task_row(row)
            if row is None:
                raise NotFound("task not found")
            task = self._task_row(row)
            if conversation_id:
                self._close_execution(cur, task_id, outcome="superseded",
                                      reason="another conversation was started for the task")
                cur.execute(
                    "INSERT INTO nexus_task_executions(run_id, task_id, project_id, task_key, "
                    "  cycle, attempt, kind, openhands_account_id, executor_label, "
                    "  conversation_id, repository, branch, base_sha) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, COALESCE("
                    "  (SELECT label FROM nexus_openhands_accounts WHERE id=%s), ''), "
                    "  %s,%s,%s,%s)",
                    (task["run_id"], task_id, task["project_id"], task["task_key"],
                     task.get("cycle") or 0, task["attempt"],
                     kind if kind in ("start", "continue", "handoff", "adopted") else "start",
                     task.get("openhands_account_id"), task.get("openhands_account_id"),
                     str(conversation_id), task.get("repository") or "",
                     task.get("branch") or "", task.get("base_sha")))
                if task["state"] != "running":
                    # The run was cancelled while the start was on its way.
                    # Nobody will follow this conversation; it is told to stop.
                    cur.execute(
                        "INSERT INTO nexus_executor_stops(run_id, task_id, project_id, "
                        "  conversation_id, openhands_account_id, repository, branch, reason) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (conversation_id) DO NOTHING",
                        (task["run_id"], task_id, task["project_id"], str(conversation_id),
                         task.get("openhands_account_id"), task.get("repository") or "",
                         task.get("branch") or "",
                         "started for a task that was settled meanwhile (its run was stopped)"))
                    self._close_execution(cur, task_id, outcome="cancelled",
                                          reason="the run was stopped while the start was under way")
            return task

    def record_verification(self, task_id: str, *, verdict: str, evidence: dict,
                            detail: dict | None = None,
                            lease_token: int | None = None) -> dict:
        """Set the terminal task state from a verifier verdict.

        ``verdict`` is one of the five task outcomes. Only
        ``completed_verified`` unblocks children.

        The verdict is recorded only while the task is still in flight. A task
        that was settled in the meantime (its run was cancelled while the
        conversation was being polled) keeps the state it was given, and is
        returned as it is.

        *lease_token* fences the verdict: when it is given, the verdict lands
        only while the task still carries that token, so a worker whose lease
        expired cannot settle a task that has since been taken up by another
        worker (or requeued for a new attempt).
        """
        if verdict not in ("completed_verified", "completed_unverified",
                           "partial", "failed", "blocked"):
            raise ValueError("invalid verdict")
        fence = "AND lease_token=%s" if lease_token is not None else ""
        params: list[Any] = [verdict, _jsonb(detail or {}), _jsonb([evidence])]
        params.append(task_id)
        if lease_token is not None:
            params.append(lease_token)
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, verification=%s, "
                "evidence = evidence || %s::jsonb, lease_expires=NULL, "
                "lease_owner=NULL, updated_at=now() "
                f"WHERE id=%s AND state IN ('dispatched','running','verifying') {fence} "
                "RETURNING *",
                tuple(params))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound("task not found")
                return self._task_row(row)
            task = self._task_row(row)
            self._close_execution(cur, task_id, outcome=verdict,
                                  reason=str(evidence.get("reason") or ""),
                                  head_sha=evidence.get("remote_sha"))
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (task["run_id"],))
            project_id = cur.fetchone()["project_id"]
            self._notify(cur, project_id, f"task_{verdict}", task["title"], task["id"])
            return task

    # ------------------------------------------------------ trying again
    def retry_task(self, task_id: str, *, verdict: str, evidence: dict,
                   detail: dict | None = None, note: str, continue_conversation: bool,
                   wait_seconds: float | None = None, outcome: str | None = None,
                   lease_token: int | None = None) -> dict:
        """The attempt is over and did not verify; the task will be tried again.

        What the attempt was is kept: *evidence* joins the task's evidence
        (with the conversation it belongs to) and *detail* becomes its
        ``verification`` - the reason a person reads. Then, in the same
        transaction:

        * ``wait_seconds is None`` - the cycle has attempts left: the task is
          ``pending`` and is taken on the next pass;
        * otherwise the cycle's attempts are used up: the task is ``waiting``
          for that long and then starts its next cycle, with the attempts
          restored and no executor ruled out.

        With *continue_conversation* the next attempt is made in the
        conversation that just ended (the agent is told what is missing, where
        its work still is); without it, a new conversation is started. *note*
        is what the next attempt is told and what the task shows meanwhile.

        Only a task that is still in flight is touched; one that was settled
        meanwhile (its run was cancelled) keeps the state it was given. With
        *lease_token* the same holds for a task that was taken away meanwhile
        (reclaimed, switched, requeued): a worker that no longer holds it
        cannot send it back to work on an old verdict. The token moves on and
        the owner is cleared, as for every transition that ends a claim.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            if row["state"] not in IN_FLIGHT_TASK_STATES:
                return self._task_row(row)
            if lease_token is not None and int(row["lease_token"] or 0) != int(lease_token):
                return self._task_row(row)
            waiting = wait_seconds is not None
            record = {**evidence, "verdict": verdict, "attempt": row["attempt"],
                      "cycle": row["cycle"], "conversation_id": row["conversation_id"]}
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, verification=%s, "
                "evidence = evidence || %s::jsonb, lease_expires=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "resume_conversation_id=%s, resume_note=%s, conversation_id=NULL, "
                "retry_at = CASE WHEN %s THEN now() + %s * interval '1 second' ELSE NULL END, "
                "wait_reason = CASE WHEN %s THEN %s ELSE NULL END, "
                "cycle = cycle + CASE WHEN %s THEN 1 ELSE 0 END, "
                "attempt = CASE WHEN %s THEN 0 ELSE attempt END, "
                "tried_accounts = CASE WHEN %s THEN '[]'::jsonb ELSE tried_accounts END, "
                "last_error=NULL, updated_at=now() WHERE id=%s RETURNING *",
                ("waiting" if waiting else "pending", _jsonb(detail or {}), _jsonb([record]),
                 row["conversation_id"] if continue_conversation else None, note[:2000],
                 waiting, max(0.0, float(wait_seconds or 0.0)), waiting, note[:1000],
                 waiting, waiting, waiting, task_id))
            task = self._task_row(cur.fetchone())
            self._close_execution(cur, task_id, outcome=outcome or verdict,
                                  reason=str(evidence.get("reason") or note),
                                  head_sha=evidence.get("remote_sha"))
            self._notify(cur, task["project_id"], "task_retry", task["title"], task["id"])
            return task

    def wait_task(self, task_id: str, *, seconds: float, reason: str, new_cycle: bool,
                  give_back: bool = False, failed_account_id: str | None = None,
                  forget_tried: bool = False, lease_token: int | None = None) -> dict:
        """Put a claimed task aside until *seconds* have passed: ``waiting``.

        For a start that did not happen - no executor could take the task, the
        project may not use the repository right now, the start failed for the
        last time this cycle. *new_cycle* begins the task's next work cycle
        (attempts restored); *give_back* returns the attempt and the run's
        iteration, for a start that was never made at all. Only a task that is
        still ``dispatched`` is touched - and, with *lease_token*, only while
        it is still claimed under that token.
        """
        fence = "AND lease_token=%s" if lease_token is not None else ""
        with self.store._tx() as cur:
            cur.execute("SELECT run_id FROM nexus_run_tasks WHERE id=%s", (task_id,))
            found = cur.fetchone()
            if found is None:
                raise NotFound("task not found")
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s FOR UPDATE", (found["run_id"],))
            cur.execute(
                "UPDATE nexus_run_tasks SET state='waiting', lease_expires=NULL, "
                "retry_at = now() + %s * interval '1 second', wait_reason=%s, last_error=%s, "
                "attempt = CASE WHEN %s THEN 0 WHEN %s THEN GREATEST(attempt - 1, 0) "
                "  ELSE attempt END, "
                "cycle = cycle + CASE WHEN %s THEN 1 ELSE 0 END, "
                "tried_accounts = CASE WHEN %s OR %s THEN '[]'::jsonb "
                "  WHEN %s::text IS NULL OR tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                f"updated_at=now() WHERE id=%s AND state='dispatched' {fence} RETURNING *",
                (max(0.0, float(seconds)), reason[:1000], reason[:2000],
                 bool(new_cycle), bool(give_back), bool(new_cycle),
                 bool(new_cycle), bool(forget_tried),
                 failed_account_id, failed_account_id, failed_account_id, task_id)
                + ((lease_token,) if lease_token is not None else ()))
            row = cur.fetchone()
            if row is not None and give_back:
                cur.execute("UPDATE nexus_runs SET iteration=GREATEST(iteration - 1, 0), "
                            "total_iterations=GREATEST(total_iterations - 1, 0), "
                            "updated_at=now() WHERE id=%s", (found["run_id"],))
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            return self._task_row(row)

    def hold_task(self, task_id: str, *, seconds: float, reason: str) -> None:
        """Leave a running task as it is and do not look at it again for
        *seconds*: its conversation ended, and the result cannot be checked
        right now (GitHub does not answer). Nothing is spent and nothing is
        said to the agent; the check is simply made later."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET retry_at = now() + %s * interval '1 second', "
                "wait_reason=%s, started_at = COALESCE(started_at, updated_at), "
                "updated_at=now() WHERE id=%s AND state = ANY(%s)",
                (max(0.0, float(seconds)), reason[:1000], task_id, list(IN_FLIGHT_TASK_STATES)))

    def note_progress(self, task_id: str, marker: str | None) -> bool:
        """Record that the task's conversation was looked at, and whether it
        has moved since the last look. Returns whether it has."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET progress_checked_at=now(), "
                "progress_at = CASE WHEN %s::text IS NOT NULL "
                "  AND progress_marker IS DISTINCT FROM %s::text THEN now() ELSE progress_at END, "
                "progress_marker = COALESCE(%s::text, progress_marker) "
                "WHERE id=%s AND state = ANY(%s) "
                "RETURNING (progress_at = now()) AS moved",
                (marker, marker, marker, task_id, list(IN_FLIGHT_TASK_STATES)))
            row = cur.fetchone()
            return bool(row and row["moved"])

    def reopen_task(self, task_id: str, *, reason: str, seconds: float = 0.0) -> dict:
        """A verified task is not verified any more: what it was accepted for
        is no longer in its branch. It goes back to work, as a new cycle."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state = CASE WHEN %s > 0 THEN 'waiting' "
                "  ELSE 'pending' END, "
                "retry_at = CASE WHEN %s > 0 THEN now() + %s * interval '1 second' ELSE NULL END, "
                "wait_reason=%s, resume_note=%s, resume_conversation_id=NULL, "
                "evidence = evidence || jsonb_build_array(jsonb_build_object("
                "  'reopened', %s::text, 'attempt', attempt, 'cycle', cycle, "
                "  'conversation_id', conversation_id)), "
                "cycle = cycle + 1, attempt = 0, tried_accounts='[]'::jsonb, "
                "conversation_id=NULL, lease_expires=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "verification = jsonb_build_object('verdict', 'reopened', 'reason', %s::text), "
                "updated_at=now() WHERE id=%s AND state='completed_verified' RETURNING *",
                (float(seconds), float(seconds), float(seconds), reason[:1000], reason[:2000],
                 reason[:1000], reason[:1000], task_id))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound("task not found")
            return self._task_row(row)

    def release_task(self, task_id: str, *, error: str, requeue: bool,
                     failed_account_id: str | None = None,
                     lease_token: int | None = None) -> dict:
        """Return a claimed task to ``pending`` (requeue) or mark it ``failed``.

        Only a task that is still ``dispatched`` is released. One that was
        settled while its start was failing (the run was cancelled) keeps the
        state it was given.

        *failed_account_id*, when given, is appended to ``tried_accounts`` so a
        later failover never picks that account for this task again.

        *lease_token* fences the release: when it is given, the task is released
        only while it still carries that token, so a worker whose lease expired
        cannot undo a claim another worker has taken. The token is bumped, so
        the released task's next holder gets a fresh one.
        """
        state = "pending" if requeue else "failed"
        fence = "AND lease_token=%s" if lease_token is not None else ""
        params: list[Any] = [state, error, failed_account_id, failed_account_id]
        params.append(task_id)
        if lease_token is not None:
            params.append(lease_token)
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, lease_expires=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, last_error=%s, "
                "tried_accounts = CASE WHEN %s::uuid IS NULL THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                f"updated_at=now() WHERE id=%s AND state='dispatched' {fence} RETURNING *",
                tuple(params))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def postpone_task(self, task_id: str, *, error: str, seconds: float,
                      failed_account_id: str | None = None,
                      forget_tried: bool = False,
                      lease_token: int | None = None) -> dict:
        """Put a claimed task back because the start was refused, not failed.

        The service said "not now" (a rate limit, a daily quota). No
        conversation exists, so the claim is undone rather than spent: the
        task's attempt and the run's iteration are given back, and the task
        waits *seconds* before it can be claimed again (a pending task with a
        live lease is not claimable). Without this a quota that lasts until
        tomorrow used up all three attempts within seconds, and a run stopped
        that way could not be resumed: "every task has used all its attempts".

        With an account pool: *failed_account_id* is the account that refused,
        given when another account can take the task - it is added to
        ``tried_accounts`` so the next claim goes there (and *seconds* is then
        0, nothing to wait for). *forget_tried* is for the opposite case, no
        account left to ask: ``tried_accounts`` is emptied, so that after the
        wait the task is offered to the whole pool again and starts on
        whichever account takes work first.

        Like ``release_task``, only a task that is still ``dispatched`` is
        touched; one settled meanwhile keeps the state it was given.

        *lease_token*, when given, fences the postpone: the task goes back only
        while it still carries that token. The token is bumped so the next
        holder of the task gets a fresh one.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT run_id FROM nexus_run_tasks WHERE id=%s", (task_id,))
            found = cur.fetchone()
            if found is None:
                raise NotFound("task not found")
            # The run row first, as everywhere else that locks both.
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s FOR UPDATE", (found["run_id"],))
            fence = "AND lease_token=%s" if lease_token is not None else ""
            wait = max(0.0, float(seconds))
            params: list[Any] = [wait, error, wait, wait, bool(forget_tried),
                                 failed_account_id, failed_account_id, failed_account_id,
                                 task_id]
            if lease_token is not None:
                params.append(lease_token)
            cur.execute(
                "UPDATE nexus_run_tasks SET state='pending', attempt=GREATEST(attempt - 1, 0), "
                "lease_expires=now() + (%s || ' seconds')::interval, last_error=%s, "
                # When it will be asked again, for whoever shows the task; the
                # lease is what holds it back. ``refusals`` counts the starts
                # refused in a row - the next wait grows with it.
                "retry_at = CASE WHEN %s > 0 THEN now() + (%s || ' seconds')::interval "
                "  ELSE NULL END, "
                "refusals = refusals + 1, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "tried_accounts = CASE WHEN %s THEN '[]'::jsonb "
                "  WHEN %s::text IS NULL OR tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                f"updated_at=now() WHERE id=%s AND state='dispatched' {fence} RETURNING *",
                tuple(params))
            row = cur.fetchone()
            if row is not None:
                cur.execute("UPDATE nexus_runs SET iteration=GREATEST(iteration - 1, 0), "
                            "total_iterations=GREATEST(total_iterations - 1, 0), "
                            "updated_at=now() WHERE id=%s", (found["run_id"],))
            else:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            return self._task_row(row)

    def requeue_for_failover(self, task_id: str, *, failed_account_id: str,
                             error: str, requeue: bool = True,
                             lease_token: int | None = None) -> dict:
        """Requeue a task whose executor failed, excluding that account.

        Works from ``dispatched`` or ``running`` (a conversation that was
        started and then vanished): the task goes back to ``pending`` so another
        account can pick it up, and the failed account is added to
        ``tried_accounts`` so the pool never selects it for this task again. A
        task that is already terminal keeps its state.

        *lease_token*, when given, fences the requeue: a worker whose lease
        expired cannot requeue a task another worker has since taken. The token
        is bumped and the owner cleared, so the failed worker cannot go on
        writing to the task it has lost.
        """
        state = "pending" if requeue else "failed"
        fence = "AND lease_token=%s" if lease_token is not None else ""
        params: list[Any] = [state, error[:2000], failed_account_id, failed_account_id]
        params.append(task_id)
        if lease_token is not None:
            params.append(lease_token)
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, lease_expires=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "conversation_id=NULL, resume_conversation_id=NULL, last_error=%s, "
                "abandoned = CASE WHEN conversation_id IS NULL THEN abandoned "
                "  ELSE abandoned || jsonb_build_array(jsonb_build_object("
                "    'conversation_id', conversation_id, "
                "    'account_id', openhands_account_id, 'at', now())) END, "
                "tried_accounts = CASE WHEN tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() "
                f"WHERE id=%s AND state IN ('dispatched','running','verifying') {fence} "
                "RETURNING *",
                tuple(params))
            row = cur.fetchone()
            if row is not None:
                self._close_execution(cur, task_id, outcome="vanished", reason=error)
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def holds_lease(self, task_id: str, lease_token: int) -> bool:
        """Whether the task is still claimed under *lease_token*.

        The scheduler asks this immediately before it starts a conversation, so
        a worker whose claim was taken away in the meantime (its lease expired
        and another worker reclaimed the task, or an operator switched it) does
        not start a second conversation for the same task. This narrows the
        window; it cannot close it (the check and the start are not one
        transaction), which is why the conversation is also bound with the token
        (``mark_running``): the *record* of the start is what must not race.

        The token is the fence, not the clock: a lease that expired but that
        nobody has reclaimed still belongs to this worker, so the start goes
        ahead. It is the reclaim (or switch, or resume) that bumps the token and
        takes the task away.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_run_tasks WHERE id=%s AND lease_token=%s "
                        "AND state IN ('dispatched','running','verifying')",
                        (task_id, lease_token))
            return cur.fetchone() is not None

    def pending_stops(self, run_id: str) -> list[dict]:
        """The conversations a run still has to stop, task by task.

        A switch records the conversation it leaves behind in the task's
        ``abandoned`` list; the worker stops each for real (``stop``) and drops
        it with ``mark_abandoned_stopped``. Until the list is empty the task's
        branch is held (``_BRANCH_FREE``), so an interrupted stop is retried
        rather than lost.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, task_key, project_id, abandoned FROM nexus_run_tasks "
                "WHERE run_id=%s AND jsonb_array_length(abandoned) > 0 "
                "ORDER BY created_at, id", (run_id,))
            out = []
            for row in cur.fetchall():
                for entry in (row["abandoned"] or []):
                    if isinstance(entry, dict) and entry.get("conversation_id"):
                        out.append({"task_id": str(row["id"]), "task_key": row["task_key"],
                                    "project_id": str(row["project_id"]), **entry})
            return out

    def ended_runs_with_abandoned(self, *, limit: int = 20) -> list[str]:
        """Runs that are over and still have a conversation to stop.

        ``pending_stops`` is drained by the run's own pass, and a run that has
        ended - it was cancelled, it failed, it completed - has no pass any
        more. What a switch or a failover left behind just before that would
        never be stopped, and ``_BRANCH_FREE`` would hold its branch against
        every later run. The worker loop asks for these and drains them.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT DISTINCT t.run_id FROM nexus_run_tasks t "
                "JOIN nexus_runs r ON r.id = t.run_id "
                "WHERE t.abandoned <> '[]'::jsonb AND r.state = ANY(%s) LIMIT %s",
                (list(TERMINAL_RUN_STATES), limit))
            return [str(row["run_id"]) for row in cur.fetchall()]

    def mark_abandoned_stopped(self, task_id: str, conversation_id: str) -> None:
        """Drop one stopped conversation from a task's ``abandoned`` list.

        Idempotent: a conversation already gone is a no-op, so a retried stop is
        safe. When the list empties, the task's branch is free again.
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET abandoned = COALESCE(("
                "  SELECT jsonb_agg(e) FROM jsonb_array_elements(abandoned) e "
                "  WHERE e->>'conversation_id' <> %s), '[]'::jsonb), updated_at=now() "
                "WHERE id=%s",
                (str(conversation_id), task_id))

    def tried_accounts(self, task_id: str) -> list[str]:
        with self.store._tx() as cur:
            cur.execute("SELECT tried_accounts FROM nexus_run_tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return [str(x) for x in (row["tried_accounts"] or [])]

    def switch_task_executor(self, task_id: str, *, target_account_id: str) -> dict:
        """Hand an in-flight task to a different account on the next tick.

        A switch is a *new* conversation on the target account (a Cloud
        conversation cannot move between accounts), so this only prepares the
        move: the current account is added to ``tried_accounts`` so the pool will
        not pick it again for this task, and the task is requeued to ``pending``
        with its conversation unbound. The scheduler then dispatches it through
        the target account with a context hand-off (the hand-off names the plan
        and what is already done).

        Refused (``ValueError``) unless the task is ``dispatched``, ``running``
        or ``verifying``: a pending task has no executor to switch from, and a
        terminal task is done.

        The abandoned conversation is **recorded in the task's evidence** before
        the binding is cleared, so the conversation id is never lost: the
        operator can still stop that conversation (``stop_abandoned``) and the
        transcript remains the record of why the work moved. Clearing the
        binding alone is not enough to stop an agent already running - the
        conversation goes on in the Cloud - so the switch is a *hand-off plus a
        stop*, and the two must not be separated.

        The same conversation is also added to the task's ``abandoned`` list: the
        worker stops it for real, and until then the task's branch is held
        (``_BRANCH_FREE``) so no second writer starts on the same checkout. The
        evidence copy is the human record; the ``abandoned`` list is what the
        loop acts on, so the two are not redundant.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT state, openhands_account_id, conversation_id, attempt "
                        "FROM nexus_run_tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            if row["state"] not in ("dispatched", "running", "verifying"):
                raise ValueError(f"cannot switch a task that is {row['state']}")
            current = row["openhands_account_id"]
            abandoned = row["conversation_id"]
            # The current account is recorded as tried (the pool never picks it
            # for this task again) and the target is stored as the preference
            # for the next dispatch. The target is only a preference: if it is
            # not usable the pool selects another, rather than failing the task.
            self._close_execution(cur, task_id, outcome="superseded",
                                  reason="the operator switched the task to another executor")
            cur.execute(
                "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
                "lease_owner=NULL, lease_token=lease_token+1, "
                "conversation_id=NULL, resume_conversation_id=NULL, openhands_account_id=NULL, "
                "abandoned = CASE WHEN %s::text IS NULL THEN abandoned "
                "  ELSE abandoned || jsonb_build_array(jsonb_build_object("
                "    'conversation_id', %s::text, 'account_id', %s::text, "
                "    'at', now())) END, "
                "evidence = CASE WHEN %s::text IS NULL THEN evidence "
                "  ELSE evidence || jsonb_build_array(jsonb_build_object("
                "    'abandoned', 'switch', 'attempt', %s, "
                "    'conversation_id', %s::text)) END, "
                "preferred_account_id=%s, "
                "tried_accounts = CASE WHEN %s::text IS NULL THEN tried_accounts "
                "  WHEN tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() WHERE id=%s RETURNING *",
                (abandoned, abandoned, str(current) if current else None,
                 abandoned, row["attempt"], abandoned,
                 target_account_id,
                 str(current) if current else None,
                 str(current) if current else None,
                 str(current) if current else None, task_id))
            return self._task_row(cur.fetchone())

    def record_handoff(self, *, run_id: str, task_id: str,
                       from_account_id: str | None, to_account_id: str | None,
                       reason: str, context: dict) -> dict:
        """Record that a task's executor was switched, and the context passed."""
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_task_handoffs(run_id, task_id, from_account_id, "
                "  to_account_id, reason, context) VALUES (%s,%s,%s,%s,%s,%s::jsonb) "
                "RETURNING id, created_at",
                (run_id, task_id, from_account_id, to_account_id, reason[:2000],
                 _jsonb(context)))
            row = cur.fetchone()
            return {"id": str(row["id"]), "created_at": row["created_at"]}

    def handoffs(self, task_id: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute("SELECT id, from_account_id, to_account_id, reason, context, "
                        "created_at FROM nexus_task_handoffs WHERE task_id=%s "
                        "ORDER BY created_at, id", (task_id,))
            return [dict(r) for r in cur.fetchall()]

    def run_handoffs(self, run_id: str) -> list[dict]:
        """Every executor switch of a run, oldest first, with the account labels.

        The account labels are joined here so the interface can name the
        executor a task moved *from* and *to* without holding the account list;
        a switch whose account was later removed keeps a ``NULL`` label and the
        id, never a fabricated name.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT h.id, h.task_id, t.task_key, h.from_account_id, h.to_account_id, "
                "  fa.label AS from_label, ta.label AS to_label, h.reason, h.created_at "
                "FROM nexus_task_handoffs h "
                "LEFT JOIN nexus_run_tasks t ON t.id = h.task_id "
                "LEFT JOIN nexus_openhands_accounts fa ON fa.id = h.from_account_id "
                "LEFT JOIN nexus_openhands_accounts ta ON ta.id = h.to_account_id "
                "WHERE h.run_id=%s ORDER BY h.created_at, h.id", (run_id,))
            out = []
            for row in cur.fetchall():
                item = dict(row)
                for key in ("id", "task_id", "from_account_id", "to_account_id"):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                out.append(item)
            return out

    def run_events(self, run_id: str, *, limit: int = 100) -> list[dict]:
        """The run's recent events, newest first, for the progress view.

        Two real sources are merged, both with a real timestamp: the project
        events the run itself wrote (started, cancelled, hidden/shown) and one
        entry per executor attempt - its start, and its settled outcome when it
        has one, carrying the account, the conversation id and the reason.
        Nothing is invented: a run with no attempts yet has only its own events.
        """
        limit = max(1, min(int(limit), 500))
        with self.store._tx() as cur:
            cur.execute(
                "SELECT kind, created_at AS at, actor FROM nexus_project_events "
                "WHERE resource_id=%s ORDER BY seq DESC LIMIT %s", (run_id, limit))
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT e.task_key, e.kind, e.started_at, e.ended_at, "
                "  e.executor_label AS account, e.conversation_id, e.outcome, e.reason "
                "FROM nexus_task_executions e WHERE e.run_id=%s "
                "ORDER BY e.started_at DESC, e.id DESC LIMIT %s", (run_id, limit))
            executions = [dict(r) for r in cur.fetchall()]
        events = [{"at": r["at"], "kind": r["kind"],
                   "detail": {"actor": r["actor"]} if r.get("actor") else {}}
                  for r in rows]
        for row in executions:
            base = {"task": row["task_key"]}
            if row.get("account"):
                base["account"] = row["account"]
            if row.get("conversation_id"):
                base["conversation_id"] = str(row["conversation_id"])
            events.append({"at": row["started_at"], "kind": f"attempt.{row['kind']}",
                           "detail": dict(base)})
            if row.get("ended_at"):
                settled = dict(base)
                if row.get("outcome"):
                    settled["outcome"] = row["outcome"]
                if row.get("reason"):
                    settled["reason"] = row["reason"]
                events.append({"at": row["ended_at"],
                               "kind": f"attempt.{row['kind']}.ended", "detail": settled})
        events.sort(key=lambda e: e["at"], reverse=True)
        return events[:limit]

    def reclaim_expired(self, run_id: str) -> list[dict]:
        """Requeue tasks a worker claimed and never started (it died in between).

        Only a task with **no conversation bound** is taken back. Polling is
        stateless - any worker can follow a conversation by its id - so a task
        whose conversation exists was not lost with the worker that started it,
        and requeueing it would start a second conversation for the same work
        while the first is still running. How long such a task may run is the
        task's own ``timeout_seconds`` (see ``in_flight_tasks``), not the lease.

        A task that has exhausted its attempts is marked ``failed`` instead of
        being requeued forever - in a ``bounded`` run. In a ``durable`` one it
        starts its next work cycle at once: the claim that was lost with a
        worker is not a reason to wait.

        The task is marked (``evidence``: ``reclaimed``) so that the next
        dispatch looks for a conversation the lost worker may have started
        before it starts another.
        """
        out = []
        with self.store._tx() as cur:
            cur.execute("SELECT policy FROM nexus_runs WHERE id=%s", (run_id,))
            run = cur.fetchone()
            durable = bool(run) and run["policy"] == "durable"
            cur.execute(
                "SELECT * FROM nexus_run_tasks WHERE run_id=%s AND state IN ('dispatched','running') "
                "AND conversation_id IS NULL "
                "AND lease_expires IS NOT NULL AND lease_expires < now() FOR UPDATE",
                (run_id,))
            for row in cur.fetchall():
                task = self._task_row(row)
                if task["attempt"] >= task["max_attempts"] and durable:
                    cur.execute(
                        "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
                        "attempt=0, cycle=cycle+1, tried_accounts='[]'::jsonb, "
                        "evidence = evidence || jsonb_build_array(jsonb_build_object("
                        "  'reclaimed', true, 'attempt', attempt, 'cycle', cycle)), "
                        "last_error='lease expired', updated_at=now() WHERE id=%s RETURNING *",
                        (task["id"],))
                elif task["attempt"] >= task["max_attempts"]:
                    cur.execute("UPDATE nexus_run_tasks SET state='failed', lease_expires=NULL, "
                                "lease_owner=NULL, lease_token=lease_token+1, "
                                "last_error='lease expired after max attempts', updated_at=now() "
                                "WHERE id=%s RETURNING *", (task["id"],))
                else:
                    cur.execute(
                        "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
                        "lease_owner=NULL, lease_token=lease_token+1, "
                        "evidence = evidence || jsonb_build_array(jsonb_build_object("
                        "  'reclaimed', true, 'attempt', attempt, 'cycle', cycle)), "
                        "last_error='lease expired', updated_at=now() WHERE id=%s RETURNING *",
                        (task["id"],))
                out.append(self._task_row(cur.fetchone()))
            return out

    def in_flight_tasks(self, run_id: str) -> list[dict]:
        """Tasks with a conversation under way, each with

        * ``running_seconds`` - how long ago the conversation was started
          (``started_at``; for a row written before that column existed,
          ``updated_at``, which ``mark_running`` was then the last to write);
        * ``quiet_seconds`` - how long since the conversation was last seen to
          move (``note_progress``);
        * ``checked_seconds`` - how long since it was last looked at for that,
          or ``None`` if it never was;
        * ``held`` - its result is not to be read before ``retry_at``
          (``hold_task``).
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT t.*, "
                "EXTRACT(EPOCH FROM (now() - COALESCE(t.started_at, t.updated_at))) "
                "  AS running_seconds, "
                "EXTRACT(EPOCH FROM (now() - COALESCE(t.progress_at, t.started_at, t.updated_at))) "
                "  AS quiet_seconds, "
                "EXTRACT(EPOCH FROM (now() - t.progress_checked_at)) AS checked_seconds, "
                "(t.retry_at IS NOT NULL AND t.retry_at > now()) AS held, "
                # What the current conversation is to the task (a start, or a
                # continuation of the one before).
                "(SELECT e.kind FROM nexus_task_executions e WHERE e.task_id = t.id "
                "  AND e.ended_at IS NULL ORDER BY e.started_at DESC LIMIT 1) AS execution_kind "
                "FROM nexus_run_tasks t WHERE t.run_id=%s "
                "AND t.state IN ('dispatched','running') ORDER BY t.created_at, t.id",
                (run_id,))
            out = []
            for row in cur.fetchall():
                task = self._task_row(row)
                task["running_seconds"] = float(task["running_seconds"] or 0)
                task["quiet_seconds"] = float(task["quiet_seconds"] or 0)
                if task["checked_seconds"] is not None:
                    task["checked_seconds"] = float(task["checked_seconds"])
                out.append(task)
            return out

    def first_start_failure(self, run_id: str) -> dict | None:
        """The earliest task of the run that failed without ever being started.

        Such a task has no conversation: nothing was run and nothing was
        verified, and ``last_error`` is why the start itself failed.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT task_key, last_error FROM nexus_run_tasks WHERE run_id=%s "
                "AND state='failed' AND conversation_id IS NULL "
                "AND COALESCE(last_error, '') <> '' ORDER BY created_at, id LIMIT 1", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def first_unverified(self, run_id: str) -> dict | None:
        """The earliest task of the run that was started and did not verify,
        with the verifier's reason: what the rest of the run is waiting on."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT task_key, state, verification->>'reason' AS reason "
                "FROM nexus_run_tasks WHERE run_id=%s AND state = ANY(%s) "
                "AND COALESCE(verification->>'reason', '') <> '' "
                "ORDER BY created_at, id LIMIT 1", (run_id, list(_RETRYABLE_TASK_STATES)))
            row = cur.fetchone()
            return dict(row) if row else None

    def count_tasks(self, run_id: str) -> dict[str, int]:
        """How many of the run's tasks are in each state (absent = none)."""
        with self.store._tx() as cur:
            return self.run_progress(cur, run_id)

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
                        "AND state = ANY(%s)", (run_id, list(UNFINISHED_TASK_STATES)))
            return cur.fetchone()["n"] > 0

    def has_in_flight(self, run_id: str) -> bool:
        """Whether any task is dispatched/running/verifying right now."""
        with self.store._tx() as cur:
            cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                        "AND state IN ('dispatched','running','verifying')", (run_id,))
            return cur.fetchone()["n"] > 0

    def has_claimable(self, run_id: str) -> bool:
        """Whether any pending task can be claimed, now or once its wait is over.

        The predicate of ``claim_task`` (attempts left, every dependency
        completed_verified) without the lease, the wait and without taking a
        lock. Used to tell a run that is *waiting* from one that is
        *deadlocked*: a pending task whose dependency ended
        `completed_unverified`/`failed`/`blocked` can never become claimable.
        A task that waits out a refused start (``postpone_task``) or the pause
        between two work cycles (``waiting``) will be - that is a wait, not a
        deadlock.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state IN ('pending','waiting') "
                f"AND t.attempt < t.max_attempts AND {_DEPS_VERIFIED}", (run_id,))
            return cur.fetchone()["n"] > 0

    def blocked_ancestry(self, run_id: str) -> bool:
        """Whether some task that still expects work can never get it: it
        waits, directly or through others, for a task that ended without
        verifying and will not be tried again."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state IN ('pending','waiting') AND EXISTS ("
                "  SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) "
                "  JOIN nexus_run_tasks dep ON dep.run_id = t.run_id AND dep.task_key = d.dep "
                "  WHERE dep.state = ANY(%s))", (run_id, list(_RETRYABLE_TASK_STATES)))
            return cur.fetchone()["n"] > 0

    def next_wake(self, run_id: str) -> dict | None:
        """What a run that has nothing in flight is waiting for: the earliest
        moment one of its tasks may be taken again, and that task's reason."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT task_key, COALESCE(retry_at, lease_expires) AS at, "
                "  COALESCE(wait_reason, last_error) AS reason "
                "FROM nexus_run_tasks WHERE run_id=%s AND ("
                "  (state='waiting' AND retry_at IS NOT NULL) OR "
                "  (state='pending' AND lease_expires IS NOT NULL AND lease_expires > now())) "
                "ORDER BY COALESCE(retry_at, lease_expires), created_at, id LIMIT 1", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------- the run's pauses
    def pause_for_budget(self, run_id: str, *, seconds: float, reason: str) -> dict | None:
        """A durable run has used the task starts of its cycle: it pauses for
        *seconds* and goes on with the budget restored. Returns the run, or
        ``None`` when it is not a durable run that is running."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_runs SET resume_at = now() + %s * interval '1 second', "
                "wait_reason=%s, cycle = cycle + 1, iteration = 0, updated_at=now() "
                "WHERE id=%s AND state='running' AND policy='durable' RETURNING *",
                (max(0.0, float(seconds)), reason[:1000], run_id))
            row = cur.fetchone()
            return self._run_view(row) if row else None

    def defer_run(self, run_id: str, *, seconds: float, reason: str,
                  confirming: bool = False) -> None:
        """The run starts nothing and is not decided before *seconds* have
        passed, for *reason* (the final confirmation could not be made)."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_runs SET resume_at = now() + %s * interval '1 second', "
                "wait_reason=%s, confirm_attempts = confirm_attempts + CASE WHEN %s THEN 1 "
                "  ELSE 0 END, updated_at=now() WHERE id=%s AND state='running'",
                (max(0.0, float(seconds)), reason[:1000], bool(confirming), run_id))

    def run_pausing(self, run_id: str) -> bool:
        """Whether the run is inside a pause of its own (``resume_at``)."""
        with self.store._tx() as cur:
            cur.execute("SELECT (resume_at IS NOT NULL AND resume_at > now()) AS pausing "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            return bool(row and row["pausing"])

    # ---------------------------------------------------------- stop signals
    def request_stop(self, task: dict, conversation_id: str, *, reason: str) -> None:
        """Record that *conversation_id* has to stop. Idempotent: one stop per
        conversation, whoever asks and however often."""
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_executor_stops(run_id, task_id, project_id, conversation_id, "
                "  openhands_account_id, repository, branch, reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (conversation_id) DO NOTHING",
                (task["run_id"], task["id"], task["project_id"], str(conversation_id),
                 task.get("openhands_account_id"), task.get("repository") or "",
                 task.get("branch") or "", reason[:200]))

    def claim_stops(self, worker_id: str, *, limit: int = 10,
                    lease_seconds: int = 120) -> list[dict]:
        """Take the stop signals that are due, each for *lease_seconds*: a
        signal is sent by one worker at a time, and a worker that dies with one
        in hand gives it up when the lease runs out."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id FROM nexus_executor_stops WHERE state='requested' "
                "AND next_attempt_at <= now() "
                "AND (lease_expires IS NULL OR lease_expires < now()) "
                "ORDER BY next_attempt_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,))
            ids = [row["id"] for row in cur.fetchall()]
            if not ids:
                return []
            cur.execute(
                "UPDATE nexus_executor_stops SET attempts = attempts + 1, "
                "lease_expires = now() + (%s || ' seconds')::interval "
                "WHERE id = ANY(%s) RETURNING *", (lease_seconds, ids))
            return [self._stop_row(row) for row in cur.fetchall()]

    def stop_result(self, stop_id: str, *, signalled: bool | None = None,
                    confirmed: bool = False, observed: str | None = None,
                    error: str | None = None, retry_seconds: float = 0.0,
                    abandon: bool = False) -> dict:
        """Write what came of one try at stopping a conversation."""
        state = "confirmed" if confirmed else "abandoned" if abandon else "requested"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_executor_stops SET state=%s, "
                "signalled = signalled OR COALESCE(%s, false), "
                "observed = COALESCE(%s, observed), last_error=%s, lease_expires=NULL, "
                "next_attempt_at = now() + %s * interval '1 second', "
                "confirmed_at = CASE WHEN %s THEN now() ELSE confirmed_at END "
                "WHERE id=%s AND state='requested' RETURNING *",
                (state, signalled, observed, (error or None) and error[:1000],
                 max(0.0, float(retry_seconds)), confirmed, stop_id))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_executor_stops WHERE id=%s", (stop_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound("stop not found")
            return self._stop_row(row)

    def stops(self, run_id: str) -> list[dict]:
        """Every stop signal of a run, oldest first."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_executor_stops WHERE run_id=%s "
                        "ORDER BY created_at, id", (run_id,))
            return [self._stop_row(row) for row in cur.fetchall()]

    def stop_for(self, conversation_id: str) -> dict | None:
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_executor_stops WHERE conversation_id=%s",
                        (str(conversation_id),))
            row = cur.fetchone()
            return self._stop_row(row) if row else None

    @staticmethod
    def _stop_row(row: Any) -> dict:
        out = dict(row)
        for key in ("id", "run_id", "task_id", "project_id", "openhands_account_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

    # ------------------------------------------------------ execution history
    def executions(self, run_id: str, *, task_id: str | None = None) -> list[dict]:
        """Which executor ran which attempt, oldest first."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, run_id, task_id, task_key, cycle, attempt, kind, "
                "  openhands_account_id, executor_label, conversation_id, repository, branch, "
                "  base_sha, head_sha, started_at, ended_at, outcome, reason "
                "FROM nexus_task_executions WHERE run_id=%s AND (%s::uuid IS NULL OR task_id=%s) "
                "ORDER BY started_at, id", (run_id, task_id, task_id))
            out = []
            for row in cur.fetchall():
                item = dict(row)
                for key in ("id", "run_id", "task_id", "openhands_account_id"):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                out.append(item)
            return out

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

    # -------------------------------------------------------- worker heartbeat
    def register_worker(self, worker_id: str, *, kind: str = "worker") -> None:
        """Record that a worker is running (upsert; also restarts the clock)."""
        if kind not in ("worker", "inline"):
            raise ValueError("kind must be 'worker' or 'inline'")
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_lab_workers(worker_id, kind, state, started_at, "
                "  heartbeat_at) VALUES (%s,%s,'running',now(),now()) "
                "ON CONFLICT (worker_id) DO UPDATE SET kind=EXCLUDED.kind, state='running', "
                "  heartbeat_at=now()", (worker_id, kind))

    def heartbeat(self, worker_id: str, *, error: str | None = None) -> None:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_lab_workers SET heartbeat_at=now(), "
                        "last_error=%s, state='running' WHERE worker_id=%s",
                        ((error or None) and error[:1000], worker_id))

    def stop_worker(self, worker_id: str) -> None:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_lab_workers SET state='stopped', heartbeat_at=now() "
                        "WHERE worker_id=%s", (worker_id,))

    def live_workers(self, *, max_age_seconds: int = 60) -> list[dict]:
        """Workers whose heartbeat is fresh enough to be considered running."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT worker_id, kind, state, started_at, heartbeat_at, last_error, "
                "  EXTRACT(EPOCH FROM (now() - heartbeat_at))::int AS age_seconds "
                "FROM nexus_lab_workers "
                "WHERE state='running' AND heartbeat_at > now() - (%s || ' seconds')::interval "
                "ORDER BY heartbeat_at DESC", (max_age_seconds,))
            return [dict(r) for r in cur.fetchall()]
