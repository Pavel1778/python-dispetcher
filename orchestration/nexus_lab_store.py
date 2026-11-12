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
#: ``retrying`` is active: it is waiting out a backoff, then goes on by itself.
ACTIVE_RUN_STATES = frozenset({"running", "paused", "waiting_for_approval", "blocked",
                               "retrying"})

#: Run states with no way forward. A terminal run is never serviced again.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

#: Run states that a person may act on (pause/cancel buttons, visibility).
LIVE_RUN_STATES = frozenset({"running", "paused", "waiting_for_approval", "blocked",
                             "retrying"})

#: The run state machine: for each state, the states it may be entered from.
#: Asking for the state a run is already in is a no-op, not an error.
#:
#: ``retrying`` is entered from a state that stopped with something left to try
#: (``blocked``/``failed``/``running``) and leaves only for ``running`` (the
#: backoff elapsed and the cycle restarted) or a terminal state (a person
#: cancelled it, or the cycles are spent). A cancelled run has no edge in: it
#: is over for good.
_RUN_ENTERED_FROM: dict[str, frozenset[str]] = {
    "running": frozenset({"paused", "blocked", "retrying"}),
    "paused": frozenset({"running"}),
    "waiting_for_approval": frozenset({"running"}),
    "blocked": frozenset({"running", "retrying"}),
    "retrying": frozenset({"running", "blocked"}),
    "completed": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "cancelled": frozenset({"running", "paused", "waiting_for_approval", "blocked",
                            "retrying"}),
}

#: Task states that still expect work.
UNFINISHED_TASK_STATES = ("pending", "dispatched", "running", "verifying")

#: "Nothing else is at work on this task's branch." One task at a time per
#: branch of a repository, across all runs and within one. A task is accepted
#: only when the head of its branch is the commit its agent reports
#: (``GitVerifier``); two agents pushing to one branch move that head under
#: each other, and whichever is read second is "not the branch head" though it
#: did its work. A task with no repository pushes nowhere and is not held.
_BRANCH_FREE = (
    "(t.repository = '' OR NOT EXISTS ("
    "  SELECT 1 FROM nexus_run_tasks o "
    "  WHERE o.repository = t.repository AND o.branch = t.branch AND o.id <> t.id "
    "    AND o.state IN ('dispatched','running','verifying'))) ")

#: Task states a person's "resume" may send back to the queue.
_RETRYABLE_TASK_STATES = ("completed_unverified", "partial", "failed", "blocked")

#: Task states a retry *cycle* restarts. Same set as ``_RETRYABLE_TASK_STATES``;
#: a task that never verified is given another cycle of attempts. A task that
#: already verified is not touched.
_RESTARTABLE_TASK_STATES = _RETRYABLE_TASK_STATES

#: Retry backoff: the wait before cycle N is ``BASE * 2**(N-1)``, capped. The
#: first retry is a minute, then 2, 4, 8... The cap keeps a long-lived run from
#: waiting days between cycles, while a daily quota is still honoured because a
#: refused start waits on its own ``Retry-After`` (``postpone_task``).
#: ``NEXUS_RETRY_BASE_SECONDS`` / ``NEXUS_RETRY_MAX_SECONDS`` tune them (a test,
#: or an operator who wants faster cycles).
RETRY_BASE_SECONDS = 60.0
RETRY_MAX_SECONDS = 3600.0
#: The default number of extra cycles a run gets after its first one stops.
#: ``NEXUS_MAX_RETRY_CYCLES`` tunes it.
DEFAULT_MAX_RETRY_CYCLES = 5
#: The most cycles a run may ever be given, however it was asked for.
MAX_RETRY_CYCLES = 20


def _float_env(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except ValueError:
        return default


def retry_backoff_seconds(cycle: int) -> float:
    """How long to wait before retry *cycle* (0-based: 0 is the first retry)."""
    base = max(0.0, _float_env("NEXUS_RETRY_BASE_SECONDS", RETRY_BASE_SECONDS))
    cap = max(base, _float_env("NEXUS_RETRY_MAX_SECONDS", RETRY_MAX_SECONDS))
    exponent = max(0, min(int(cycle), 20))
    return min(cap, base * (2 ** exponent))


def default_max_retry_cycles() -> int:
    """The retry cycles a run started without an explicit number is given."""
    try:
        raw = os.environ.get("NEXUS_MAX_RETRY_CYCLES", "").strip()
        value = int(raw) if raw else DEFAULT_MAX_RETRY_CYCLES
    except ValueError:
        value = DEFAULT_MAX_RETRY_CYCLES
    return max(0, min(MAX_RETRY_CYCLES, value))


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
                  max_retry_cycles: int | None = None,
                  source_conversation_id: str | None = None,
                  also: Callable[[Any, dict], None] | None = None) -> dict:
        """Create the single active run for a goal. Refuses a second one.

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
        if max_retry_cycles is None:
            max_retry_cycles = default_max_retry_cycles()
        if not 0 <= max_retry_cycles <= MAX_RETRY_CYCLES:
            raise ValueError(f"max_retry_cycles must be between 0 and {MAX_RETRY_CYCLES}")
        if deadline_seconds is None:
            deadline_seconds = DEFAULT_DEADLINE_SECONDS
        if not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS:
            raise ValueError(f"deadline_seconds must be between 1 and {MAX_DEADLINE_SECONDS}")
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
                "max_attempts_per_task, deadline_at, created_by, max_retry_cycles, "
                "source_conversation_id) "
                "VALUES (%s,%s,%s,%s, now() + %s * interval '1 second', %s, %s, %s) "
                "RETURNING *",
                (project_id, goal_id, max_iterations, max_attempts_per_task,
                 deadline_seconds, principal, max_retry_cycles, source_conversation_id))
            run = self._run_view(cur.fetchone())
            cur.execute("UPDATE nexus_goals SET state='running', active_run_id=%s, updated_at=now() "
                        "WHERE id=%s", (run["id"], goal_id))
            for position, task in enumerate(plan or []):
                self._insert_task(
                    cur, run["id"], project_id, task_key=task["id"], title=task["title"],
                    description=task.get("description") or "",
                    acceptance=task.get("acceptance") or [],
                    dependencies=task.get("dependencies") or [],
                    repository=repository, branch=branch,
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
        return run

    def budget_spent(self, run_id: str) -> str | None:
        """Which of the run's limits stops new work, if any.

        Read on the database's clock - the one ``claim_task`` uses - so the
        scheduler's reason and the claim's refusal can never disagree.
        """
        with self.store._tx() as cur:
            cur.execute(f"SELECT iteration, max_iterations, retry_attempt, "
                        f"max_retry_cycles, {_DEADLINE_SQL} <= now() AS late "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        if row["iteration"] >= row["max_iterations"]:
            # The per-cycle budget is used up. That is a *cycle* end, not the
            # run's: the scheduler puts the run in ``retrying`` with a fresh
            # budget, unless the cycles are spent, in which case it is blocked.
            return (f"iteration budget used: {row['iteration']} of "
                    f"{row['max_iterations']} task starts")
        if row["late"]:
            return "deadline passed"
        return None

    def deadline_passed(self, run_id: str) -> bool:
        """Whether the run's deadline has passed. A hard limit: retrying cannot
        help, so the run stops for good (the operator can start a new one)."""
        with self.store._tx() as cur:
            cur.execute(f"SELECT {_DEADLINE_SQL} <= now() AS late "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
        return bool(row and row["late"])

    def cycles_spent(self, run_id: str) -> bool:
        """Whether the run has used every retry cycle it was given."""
        with self.store._tx() as cur:
            cur.execute("SELECT retry_attempt >= max_retry_cycles AS spent "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
        return bool(row and row["spent"])

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
        """The project's runs, newest first.

        A run the operator cancelled (or hid) is left out unless *include_hidden*
        is asked for: the interface shows the work in progress, not the history
        of what was stopped. The rows are untouched - the run is still readable
        by id - so nothing is lost, only not listed.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            sql = "SELECT * FROM nexus_runs WHERE project_id=%s"
            if not include_hidden:
                sql += " AND hidden_at IS NULL"
            sql += " ORDER BY created_at DESC, id LIMIT %s"
            cur.execute(sql, (project_id, limit))
            return [self._run_view(r) for r in cur.fetchall()]

    def set_run_state(self, principal: str, project_id: str, run_id: str, state: str,
                      *, last_error: str | None = None) -> dict:
        """Move a run on a member's behalf. Raises ``InvalidTransition`` when the
        run's current state does not lead there (see ``_RUN_ENTERED_FROM``)."""
        if state not in _RUN_ENTERED_FROM:
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            run = self._apply_run_state(cur, project_id, run_id, state, last_error, strict=True)
            assert run is not None
            return run

    def set_run_state_internal(self, run_id: str, state: str,
                               *, last_error: str | None = None) -> dict | None:
        """State transition by the scheduler itself, not a user request.

        The worker is not a project member, so it cannot pass the membership
        check a user request must. It is still not a bypass of *authorization*:
        the run must exist, and only the scheduler calls this.

        Returns ``None`` when the run is no longer in a state the transition can
        start from. The scheduler decides from what it read a moment ago; if a
        person paused or cancelled the run since, their request stands.
        """
        if state not in _RUN_ENTERED_FROM:
            raise ValueError("invalid run state")
        with self.store._tx() as cur:
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return self._apply_run_state(cur, str(row["project_id"]), run_id, state,
                                         last_error, strict=False)

    def _apply_run_state(self, cur: Any, project_id: str, run_id: str, state: str,
                         last_error: str | None, *, strict: bool) -> dict | None:
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
            # try: tasks that did not verify and still have attempts left.
            if not self._requeue_retryable(cur, run_id, "resumed"):
                raise InvalidTransition(
                    "nothing to retry: every task that did not verify has used all its attempts")
        if state == "running" and current == "retrying":
            # "Resume" on a run waiting out its backoff means "go now": restart
            # the cycle's tasks at once instead of waiting for ``retry_at``.
            self._restart_cycle_tasks(cur, run_id)
        if state == "running" and current in ("blocked", "retrying"):
            # A person's explicit resume is consent to try again: the run gets
            # its cycles and its per-cycle start budget back, or a run that
            # stopped with either exhausted would block again on the next tick.
            cur.execute("UPDATE nexus_runs SET retry_attempt=0, iteration=0, retry_at=NULL, "
                        "retry_reason=NULL WHERE id=%s", (run_id,))
        if state in ("cancelled", "failed"):
            # Nothing of a cancelled or failed run is started or polled again,
            # so its unfinished tasks must not go on saying "pending" or
            # "running".
            why = "run cancelled" if state == "cancelled" else f"run failed: {last_error or 'no reason given'}"
            cur.execute(
                "UPDATE nexus_run_tasks SET state='blocked', lease_expires=NULL, "
                "last_error=%s, updated_at=now() "
                "WHERE run_id=%s AND state = ANY(%s)",
                (why[:500], run_id, list(UNFINISHED_TASK_STATES)))
        if state == "running":
            # A run that is going again no longer has the error that stopped it.
            cur.execute("UPDATE nexus_runs SET state=%s, last_error=NULL, updated_at=now() "
                        "WHERE id=%s RETURNING *", (state, run_id))
        else:
            cur.execute("UPDATE nexus_runs SET state=%s, last_error=COALESCE(%s, last_error), "
                        "updated_at=now() WHERE id=%s RETURNING *", (state, last_error, run_id))
        run = self._run_view(cur.fetchone())
        # Mirror terminal states onto the goal so the goal is not left
        # "running" after its only run finished.
        if state in TERMINAL_RUN_STATES:
            cur.execute("UPDATE nexus_goals SET state=%s, updated_at=now() WHERE id=%s",
                        (state, run["goal_id"]))
            self._notify(cur, project_id, f"goal_{state}", f"Цель: {state}", run_id)
        return run

    @staticmethod
    def _requeue_retryable(cur: Any, run_id: str, why: str) -> int:
        """Send back to the queue every task that ended without verifying and
        still has attempts left. Returns how many. The conversation of the
        attempt that is over is kept in the task's evidence, not on the task: a
        task's ``conversation_id`` always belongs to its current attempt."""
        cur.execute(
            "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
            "evidence = evidence || jsonb_build_array(jsonb_build_object("
            "  'requeued', %s::text, 'attempt', attempt, 'was', state, "
            "  'conversation_id', conversation_id)), "
            "conversation_id=NULL, verification=NULL, updated_at=now() "
            "WHERE run_id=%s AND state = ANY(%s) AND attempt < max_attempts",
            (why, run_id, list(_RETRYABLE_TASK_STATES)))
        return cur.rowcount

    # ------------------------------------------------------- durable retrying
    def schedule_retry(self, run_id: str, *, reason: str,
                       cycle: int | None = None) -> dict | None:
        """Put a run that stopped with work left into ``retrying`` with a backoff.

        A run no longer stops for good when its tasks have used their attempts.
        It waits (``retry_at``) and then its tasks get a fresh cycle of attempts
        (``start_retry_cycle``). *cycle* is the retry that is being scheduled
        (0-based); the wait is ``retry_backoff_seconds(cycle)``.

        Refuses (returns ``None``) when the run is not in a state a retry may
        start from, or when it has spent its ``max_retry_cycles``. A cancelled
        run never retries. The caller decides what to do when the cycles are
        spent (block it for a person).
        """
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            if row["state"] not in _RUN_ENTERED_FROM["retrying"]:
                return None
            done = int(row["retry_attempt"] or 0)
            if done >= int(row["max_retry_cycles"] or 0):
                return None
            which = done if cycle is None else int(cycle)
            wait = retry_backoff_seconds(which)
            cur.execute(
                "UPDATE nexus_runs SET state='retrying', retry_reason=%s, "
                "retry_at = now() + (%s || ' seconds')::interval, "
                "last_error=COALESCE(%s, last_error), updated_at=now() "
                "WHERE id=%s RETURNING *",
                (reason[:2000], wait, reason[:2000], run_id))
            return self._run_view(cur.fetchone())

    def start_retry_cycle(self, run_id: str) -> dict | None:
        """Begin the next cycle of a ``retrying`` run whose wait has elapsed.

        Every task that did not verify is put back in the queue with a fresh
        per-cycle attempt budget, the run's per-cycle start budget is reset, and
        the run returns to ``running``. Returns ``None`` when the wait has not
        elapsed or the run is no longer ``retrying`` (a cancel committed first).

        The claim predicate already refuses a run that is not ``running``, so
        nothing is started between the scheduler reading the run and this call.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            if row["state"] != "retrying":
                return None
            cur.execute("SELECT (retry_at IS NULL OR retry_at <= now()) AS due "
                        "FROM nexus_runs WHERE id=%s", (run_id,))
            if not cur.fetchone()["due"]:
                return None
            self._restart_cycle_tasks(cur, run_id)
            cur.execute(
                "UPDATE nexus_runs SET state='running', iteration=0, "
                "retry_attempt=retry_attempt+1, retry_at=NULL, retry_reason=NULL, "
                "last_error=NULL, updated_at=now() WHERE id=%s RETURNING *",
                (run_id,))
            return self._run_view(cur.fetchone())

    @staticmethod
    def _restart_cycle_tasks(cur: Any, run_id: str) -> int:
        """Give every task that did not verify a fresh attempt budget, and put
        it back in the queue. The attempt that is over stays on record in the
        task's evidence with its conversation; the task's ``attempt`` is
        per-cycle, and ``cycle`` rises so a person can see the retries."""
        cur.execute(
            "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
            "attempt=0, cycle=cycle+1, conversation_id=NULL, verification=NULL, "
            "evidence = evidence || jsonb_build_array(jsonb_build_object("
            "  'requeued', 'retry-cycle', 'cycle', cycle, 'was', state, "
            "  'conversation_id', conversation_id)), "
            "updated_at=now() "
            "WHERE run_id=%s AND state = ANY(%s)",
            (run_id, list(_RESTARTABLE_TASK_STATES)))
        return cur.rowcount

    def due_retries(self, *, limit: int = 100) -> list[str]:
        """Run ids in ``retrying`` whose backoff has elapsed (for the loop)."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id FROM nexus_runs WHERE state='retrying' "
                "AND (retry_at IS NULL OR retry_at <= now()) "
                "ORDER BY retry_at NULLS FIRST, created_at LIMIT %s", (limit,))
            return [str(r["id"]) for r in cur.fetchall()]

    def cancel_run(self, principal: str, project_id: str, run_id: str) -> dict:
        """Stop a run for good, as a person asks. Terminal and idempotent.

        The run is hidden from the active lists (``hidden_at``) so a cancelled
        run disappears from the interface, while every row - tasks, evidence,
        the report - is kept. Cancelling an already-cancelled run is a no-op,
        not an error.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            run = self._apply_run_state(cur, project_id, run_id, "cancelled",
                                        "cancelled by the operator", strict=True)
            assert run is not None
            # Idempotent: a second cancel keeps the first hidden/cancel times.
            cur.execute("UPDATE nexus_runs SET hidden_at=COALESCE(hidden_at, now()), "
                        "cancel_requested_at=COALESCE(cancel_requested_at, now()), "
                        "retry_at=NULL, updated_at=now() WHERE id=%s RETURNING *", (run_id,))
            return self._run_view(cur.fetchone())

    def hide_run(self, principal: str, project_id: str, run_id: str) -> dict:
        """Hide a terminal run from the active lists without deleting it."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("UPDATE nexus_runs SET hidden_at=now(), updated_at=now() "
                        "WHERE id=%s AND project_id=%s RETURNING *",
                        (run_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("run not found")
            return self._run_view(row)

    def in_flight_conversations(self, run_id: str) -> list[dict]:
        """Tasks of a run with a conversation that may still be running.

        Used by a cancel to signal the executor: a task whose conversation was
        started and never settled is named here, so the loop can ask the service
        to stop it. A task that never got a conversation has nothing to stop.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, task_key, conversation_id, openhands_account_id "
                "FROM nexus_run_tasks WHERE run_id=%s AND conversation_id IS NOT NULL "
                "AND state IN ('dispatched','running','verifying') "
                "ORDER BY created_at, id", (run_id,))
            return [dict(r) for r in cur.fetchall()]

    def mark_conversation_stopped(self, task_id: str, *, conversation_id: str,
                                  note: str = "") -> None:
        """Record that the executor was told to stop a task's conversation.

        The value of ``conversation_stopped`` is the conversation id itself, so
        ``conversations_needing_stop`` can match on it: a task whose *current*
        conversation was stopped is not signalled again, while a task whose
        conversation was replaced (a retry) is.
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET evidence = evidence || "
                "jsonb_build_array(jsonb_build_object('conversation_stopped', %s::text, "
                "  'note', %s::text, 'at', now())), updated_at=now() WHERE id=%s",
                (str(conversation_id)[:200], (note or "")[:500], task_id))

    def cancelled_runs_needing_signal(self, *, limit: int = 50) -> list[str]:
        """Cancelled runs whose conversations have not all been signalled yet.

        A cancel records ``cancel_requested_at`` and settles the run at once.
        Signalling the executor is done by the loop, not the request, so a
        cancel cannot fail because the Cloud is slow, and a signal that could
        not be sent is retried on the next pass. A run is returned here while
        any of its tasks still has a conversation and no ``conversation_stopped``
        record for it.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT r.id FROM nexus_runs r "
                "WHERE r.state='cancelled' AND r.cancel_requested_at IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM nexus_run_tasks t WHERE t.run_id=r.id "
                "  AND t.conversation_id IS NOT NULL "
                "  AND NOT (t.evidence @> jsonb_build_array("
                "    jsonb_build_object('conversation_stopped', t.conversation_id)))) "
                "ORDER BY r.cancel_requested_at LIMIT %s", (limit,))
            return [str(r["id"]) for r in cur.fetchall()]

    def conversations_needing_stop(self, run_id: str) -> list[dict]:
        """In-flight conversations of a cancelled run that were not stopped yet."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, task_key, conversation_id, openhands_account_id, project_id "
                "FROM nexus_run_tasks WHERE run_id=%s AND conversation_id IS NOT NULL "
                "AND NOT (evidence @> jsonb_build_array("
                "  jsonb_build_object('conversation_stopped', conversation_id))) "
                "ORDER BY created_at, id", (run_id,))
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- run reports
    def save_report(self, run_id: str, *, content: str, meta: dict) -> dict | None:
        """Store the run's final report. One per run; a second call is a no-op.

        ``run_id`` is UNIQUE, so a report written twice (a retried finish, two
        workers finishing at once) is absorbed by ``ON CONFLICT DO NOTHING``:
        the run keeps exactly one report. Returns the row that exists.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT project_id, source_conversation_id FROM nexus_runs "
                        "WHERE id=%s", (run_id,))
            run = cur.fetchone()
            if run is None:
                raise NotFound("run not found")
            cur.execute(
                "INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, "
                "  content, meta) VALUES (%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (run_id) DO NOTHING "
                "RETURNING id, run_id, project_id, conversation_id, status, content, "
                "  meta, attempts, next_attempt_at, last_error, created_at, sent_at",
                (run_id, run["project_id"], run["source_conversation_id"],
                 content[:60000], _jsonb(meta)))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT id, run_id, project_id, conversation_id, status, content, "
                    "  meta, attempts, next_attempt_at, last_error, created_at, sent_at "
                    "FROM nexus_run_reports WHERE run_id=%s", (run_id,))
                row = cur.fetchone()
            return self._report_view(row)

    def report_for_run(self, run_id: str) -> dict | None:
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, run_id, project_id, conversation_id, status, content, "
                "  meta, attempts, next_attempt_at, last_error, created_at, sent_at "
                "FROM nexus_run_reports WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            return self._report_view(row) if row else None

    def pending_reports(self, *, limit: int = 50) -> list[dict]:
        """Reports that still have to reach their conversation, oldest first."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, run_id, project_id, conversation_id, status, content, "
                "  meta, attempts, next_attempt_at, last_error, created_at, sent_at "
                "FROM nexus_run_reports WHERE status='pending' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                "ORDER BY created_at, id LIMIT %s", (limit,))
            return [self._report_view(r) for r in cur.fetchall()]

    def claim_pending_reports(self, *, limit: int = 50) -> list[dict]:
        """Atomically take pending reports for delivery, so no two workers do.

        ``pending_reports`` reads what is due; two workers (the web server's
        inline loop and a standalone ``lab_worker``, which the docs say may run
        side by side) would then both read the same row and both post it - the
        report would reach the chat twice. This claims each row under
        ``FOR UPDATE SKIP LOCKED`` and moves it to ``sending`` in the same
        transaction, so a row is handed to exactly one caller. A row left
        ``sending`` by a worker that died is returned to ``pending`` by
        :meth:`reclaim_stale_report_sends` on the next pass.

        The claim is the idempotency the module promises: the chat sees one
        report per run even with several workers.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, run_id, project_id, conversation_id, status, content, "
                "  meta, attempts, next_attempt_at, last_error, created_at, sent_at "
                "FROM nexus_run_reports WHERE status='pending' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                "ORDER BY created_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,))
            rows = [self._report_view(r) for r in cur.fetchall()]
            if rows:
                cur.execute("UPDATE nexus_run_reports SET status='sending', updated_at=now() "
                            "WHERE id = ANY(%s)", ([r["id"] for r in rows],))
            return rows

    def reclaim_stale_report_sends(self, *, older_than_seconds: int = 300) -> int:
        """Return reports stuck in ``sending`` to ``pending``.

        A worker that claimed a report and died before marking it sent would
        otherwise leave it ``sending`` for ever, and the report would never
        reach the chat. The claim is short-lived, so a row still ``sending``
        after a few minutes is taken back.
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_reports SET status='pending', updated_at=now() "
                "WHERE status='sending' AND updated_at <= now() - %s * interval '1 second'",
                (older_than_seconds,))
            return cur.rowcount

    def mark_report_sent(self, report_id: str) -> None:
        """Record delivery. Only moves forward from a claimed report."""
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_run_reports SET status='sent', sent_at=now(), "
                        "last_error=NULL, updated_at=now() "
                        "WHERE id=%s AND status IN ('pending','sending')", (report_id,))

    def mark_report_failed(self, report_id: str, *, error: str, retry_in: float,
                           give_up: bool = False) -> None:
        """A delivery failed; it will be retried after *retry_in* seconds.

        With *give_up*, the report is moved to ``failed`` and never retried - it
        is kept (its content is not lost) but its delivery has stopped. It is
        never marked ``sent``, so it can never be posted twice.
        """
        status = "failed" if give_up else "pending"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_reports SET attempts=attempts+1, last_error=%s, "
                "next_attempt_at = now() + (%s || ' seconds')::interval, "
                "status=%s, updated_at=now() WHERE id=%s AND status IN ('pending','sending')",
                (error[:1000], max(0.0, float(retry_in)), status, report_id))

    @staticmethod
    def _report_view(row: Any) -> dict:
        out = dict(row)
        for key in ("id", "run_id", "project_id", "conversation_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

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
        out["spec_ref"] = _loads(out.get("spec_ref"))
        out["tried_accounts"] = _loads(out.get("tried_accounts")) or []
        if out.get("openhands_account_id") is not None:
            out["openhands_account_id"] = str(out["openhands_account_id"])
        if out.get("preferred_account_id") is not None:
            out["preferred_account_id"] = str(out["preferred_account_id"])
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
        lease, **every dependency is completed_verified**, and no other task is
        at work on its branch (``_BRANCH_FREE``). Claimability is computed in
        SQL and the row is locked with ``FOR UPDATE SKIP LOCKED`` one at a
        time, so N workers claim N distinct tasks rather than one worker
        locking the whole queue (the bug the concurrency test caught). Two
        workers that reach for one branch at the same moment are put in line by
        an advisory lock on the branch; the second looks again and waits.

        The run itself is part of the claim: nothing is claimed unless the run
        is ``running``, has iterations left and is inside its deadline. That is
        checked on the locked run row in this transaction, so a pause or a
        cancel that committed first always wins - there is no moment between
        "the run looked fine" and "the task was taken". Each claim uses one
        iteration: an iteration is one start of one task.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id FROM nexus_runs WHERE id=%s AND state='running' "
                "AND iteration < max_iterations "
                f"AND {_DEADLINE_SQL} > now() FOR UPDATE", (run_id,))
            if cur.fetchone() is None:
                return None
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
                f"AND {_BRANCH_FREE}"
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
                        "total_iterations=total_iterations+1, updated_at=now() "
                        "WHERE id=%s", (run_id,))
            cur.execute(
                "UPDATE nexus_run_tasks SET state='dispatched', attempt=attempt+1, "
                "conversation_id=NULL, "
                "lease_expires=now() + (%s || ' seconds')::interval, updated_at=now() "
                "WHERE id=%s RETURNING *",
                (lease_seconds, task["id"]))
            return self._task_row(cur.fetchone())

    def mark_running(self, task_id: str, *, conversation_id: str | None = None,
                     delivery: dict | None = None,
                     account_id: str | None = None) -> dict:
        """Bind the conversation that was started and mark the task running.

        The conversation id is always kept. The state changes only if the task
        is still ``dispatched``: a task settled between the claim and this call
        (its run was cancelled) stays settled.

        *delivery* is the executor's record of how the task's original text was
        handed to the agent (inline, or a file with the path, size and SHA-256
        that were read back). It is kept under ``spec_ref.delivery``.
        """
        proof = _jsonb(delivery) if delivery else None
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET "
                        "state = CASE WHEN state='dispatched' THEN 'running' ELSE state END, "
                        "conversation_id=COALESCE(%s, conversation_id), "
                        "openhands_account_id=COALESCE(%s, openhands_account_id), "
                        "preferred_account_id = CASE WHEN %s::uuid IS NULL THEN "
                        "  preferred_account_id ELSE NULL END, "
                        "spec_ref = CASE WHEN %s::jsonb IS NULL THEN spec_ref ELSE "
                        "  COALESCE(spec_ref, '{}'::jsonb) || jsonb_build_object("
                        "    'delivery', %s::jsonb || jsonb_build_object('at', now())) END, "
                        "updated_at=now() "
                        "WHERE id=%s RETURNING *",
                        (conversation_id, account_id, account_id,
                         proof, proof, task_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def record_verification(self, task_id: str, *, verdict: str, evidence: dict,
                            detail: dict | None = None) -> dict:
        """Set the terminal task state from a verifier verdict.

        ``verdict`` is one of the five task outcomes. Only
        ``completed_verified`` unblocks children.

        The verdict is recorded only while the task is still in flight. A task
        that was settled in the meantime (its run was cancelled while the
        conversation was being polled) keeps the state it was given, and is
        returned as it is.
        """
        if verdict not in ("completed_verified", "completed_unverified",
                           "partial", "failed", "blocked"):
            raise ValueError("invalid verdict")
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, verification=%s, "
                "evidence = evidence || %s::jsonb, lease_expires=NULL, updated_at=now() "
                "WHERE id=%s AND state IN ('dispatched','running','verifying') RETURNING *",
                (verdict, _jsonb(detail or {}), _jsonb([evidence]), task_id))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound("task not found")
                return self._task_row(row)
            task = self._task_row(row)
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (task["run_id"],))
            project_id = cur.fetchone()["project_id"]
            self._notify(cur, project_id, f"task_{verdict}", task["title"], task["id"])
            return task

    def release_task(self, task_id: str, *, error: str, requeue: bool,
                     failed_account_id: str | None = None,
                     permanent: bool = False) -> dict:
        """Return a claimed task to ``pending`` (requeue) or mark it ``failed``.

        Only a task that is still ``dispatched`` is released. One that was
        settled while its start was failing (the run was cancelled) keeps the
        state it was given.

        *failed_account_id*, when given, is appended to ``tried_accounts`` so a
        later failover never picks that account for this task again.

        *permanent* records that another attempt would fail the same way (a
        delivery the transport cannot make, a rejected request). A run whose
        every unfinished task failed permanently is *blocked* rather than
        retried: no cycle can change the outcome, and the run is left for a
        person with the reason on it.
        """
        state = "pending" if requeue else "failed"
        marker = (_jsonb([{"permanent_failure": True, "error": error[:1000]}])
                  if permanent else "[]")
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, lease_expires=NULL, last_error=%s, "
                "evidence = CASE WHEN %s::jsonb = '[]'::jsonb THEN evidence "
                "  ELSE evidence || %s::jsonb END, "
                "tried_accounts = CASE WHEN %s::uuid IS NULL THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() WHERE id=%s AND state='dispatched' RETURNING *",
                (state, error, marker, marker, failed_account_id, failed_account_id, task_id))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

    def has_retryable_failure(self, run_id: str) -> bool:
        """Whether any unfinished task of the run *could* change on a retry.

        A task is not retryable when it ended ``failed`` with a recorded
        permanent failure and there is nothing else pending: no cycle will
        change it. A task that was merely unverified, timed out, or blocked
        (waiting on a dependency) is retryable - the cause may be gone next
        cycle. Used to block a run instead of retrying it for ever when the
        blocker is real and external.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state = ANY(%s) "
                "AND NOT (t.state='failed' AND t.conversation_id IS NULL "
                "  AND t.evidence @> '[{\"permanent_failure\": true}]'::jsonb)",
                (run_id, list(_RETRYABLE_TASK_STATES)))
            return cur.fetchone()["n"] > 0

    def postpone_task(self, task_id: str, *, error: str, seconds: float,
                      failed_account_id: str | None = None,
                      forget_tried: bool = False) -> dict:
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
        """
        with self.store._tx() as cur:
            cur.execute("SELECT run_id FROM nexus_run_tasks WHERE id=%s", (task_id,))
            found = cur.fetchone()
            if found is None:
                raise NotFound("task not found")
            # The run row first, as everywhere else that locks both.
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s FOR UPDATE", (found["run_id"],))
            cur.execute(
                "UPDATE nexus_run_tasks SET state='pending', attempt=GREATEST(attempt - 1, 0), "
                "lease_expires=now() + (%s || ' seconds')::interval, last_error=%s, "
                "tried_accounts = CASE WHEN %s THEN '[]'::jsonb "
                "  WHEN %s::text IS NULL OR tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() WHERE id=%s AND state='dispatched' RETURNING *",
                (max(0.0, float(seconds)), error, bool(forget_tried), failed_account_id,
                 failed_account_id, failed_account_id, task_id))
            row = cur.fetchone()
            if row is not None:
                cur.execute("UPDATE nexus_runs SET iteration=GREATEST(iteration - 1, 0), "
                            "updated_at=now() WHERE id=%s", (found["run_id"],))
            else:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            return self._task_row(row)

    def requeue_for_failover(self, task_id: str, *, failed_account_id: str,
                             error: str, requeue: bool = True) -> dict:
        """Requeue a task whose executor failed, excluding that account.

        Works from ``dispatched`` or ``running`` (a conversation that was
        started and then vanished): the task goes back to ``pending`` so another
        account can pick it up, and the failed account is added to
        ``tried_accounts`` so the pool never selects it for this task again. A
        task that is already terminal keeps its state.
        """
        state = "pending" if requeue else "failed"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_tasks SET state=%s, lease_expires=NULL, "
                "conversation_id=NULL, last_error=%s, "
                "tried_accounts = CASE WHEN tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() "
                "WHERE id=%s AND state IN ('dispatched','running','verifying') RETURNING *",
                (state, error[:2000], failed_account_id, failed_account_id, task_id))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            return self._task_row(row)

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
        terminal task is done. The old executor is not stopped here — a
        conversation already running is left to finish; it is simply never
        polled again, and the workspace lock keeps two executors off one
        checkout.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT state, openhands_account_id FROM nexus_run_tasks "
                        "WHERE id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("task not found")
            if row["state"] not in ("dispatched", "running", "verifying"):
                raise ValueError(f"cannot switch a task that is {row['state']}")
            current = row["openhands_account_id"]
            # The current account is recorded as tried (the pool never picks it
            # for this task again) and the target is stored as the preference
            # for the next dispatch. The target is only a preference: if it is
            # not usable the pool selects another, rather than failing the task.
            cur.execute(
                "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, "
                "conversation_id=NULL, openhands_account_id=NULL, "
                "preferred_account_id=%s, "
                "tried_accounts = CASE WHEN %s::text IS NULL THEN tried_accounts "
                "  WHEN tried_accounts ? %s::text THEN tried_accounts "
                "  ELSE tried_accounts || to_jsonb(%s::text) END, "
                "updated_at=now() WHERE id=%s RETURNING *",
                (target_account_id,
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

    def reclaim_expired(self, run_id: str) -> list[dict]:
        """Requeue tasks a worker claimed and never started (it died in between).

        Only a task with **no conversation bound** is taken back. Polling is
        stateless - any worker can follow a conversation by its id - so a task
        whose conversation exists was not lost with the worker that started it,
        and requeueing it would start a second conversation for the same work
        while the first is still running. How long such a task may run is the
        task's own ``timeout_seconds`` (see ``in_flight_tasks``), not the lease.

        A task that has exhausted its attempts is marked ``failed`` instead of
        being requeued forever.
        """
        out = []
        with self.store._tx() as cur:
            cur.execute(
                "SELECT * FROM nexus_run_tasks WHERE run_id=%s AND state IN ('dispatched','running') "
                "AND conversation_id IS NULL "
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

    def in_flight_tasks(self, run_id: str) -> list[dict]:
        """Tasks with a conversation under way, each with ``running_seconds``:
        how long ago it was started (``mark_running`` is the last write to a
        running task, so its ``updated_at`` is the start)."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT t.*, EXTRACT(EPOCH FROM (now() - t.updated_at)) AS running_seconds "
                "FROM nexus_run_tasks t WHERE t.run_id=%s "
                "AND t.state IN ('dispatched','running') ORDER BY t.created_at, t.id",
                (run_id,))
            out = []
            for row in cur.fetchall():
                task = self._task_row(row)
                task["running_seconds"] = float(task["running_seconds"] or 0)
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
                        "AND state IN ('pending','dispatched','running','verifying')", (run_id,))
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
        completed_verified) without the lease and without taking a lock. Used
        to tell a run that is *waiting* from one that is *deadlocked*: a
        pending task whose dependency ended `completed_unverified`/`failed`/
        `blocked` can never become claimable. A task that waits out a refused
        start (``postpone_task``) will be - that is a wait, not a deadlock.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM nexus_run_tasks t "
                "WHERE t.run_id=%s AND t.state='pending' AND t.attempt < t.max_attempts "
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
