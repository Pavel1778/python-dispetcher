"""The autonomous lab: dispatch, independent verification, replanning.

This module ties the durable store (``nexus_lab_store``) to real execution and
to *independent* verification. Three pieces, each replaceable so the loop can be
tested without a live OpenHands:

* :class:`OpenHandsExecutor` — starts a conversation for a task and polls it.
  It never decides the task is done; it only reports what the conversation said.
* :class:`GitVerifier` — checks the *actual* repository state (branch, commit,
  changed files) through the read-only GitHub client. An agent's summary is an
  input to verification, never a substitute for it.
* :class:`LabScheduler` — one tick = reclaim → poll → verify → record →
  claim → authorize → dispatch → decide. This is the only place that moves a
  run forward, and it does so only while the run is ``running``, inside its
  budgets, and only against a repository the project is allowed to use.

The core rule the whole module enforces: a task is `completed_verified` only
when the verifier observed the required state. Otherwise it is
`completed_unverified`, `partial`, `failed` or `blocked`, and a dependent task
stays gated.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .cloud_client import CloudError, CloudNotFound
from .github_client import GitHubClient, GitHubError
from .nexus_binding import AuthorizationError, authorize_run_task
from .nexus_lab_store import TERMINAL_RUN_STATES, UNFINISHED_TASK_STATES, LabStore

log = logging.getLogger("nexus_lab")

#: Conversation execution states that mean the agent is still working.
_RUNNING = {"idle", "running", "waiting_for_confirmation"}
#: Terminal-bad states.
_BAD = {"error", "stuck"}


@dataclass
class TaskReport:
    """What the executor observed about a task's conversation."""

    state: str                      # running | finished | error | stuck | missing
    final_text: str = ""
    conversation_id: str | None = None
    detail: str = ""

    @property
    def finished(self) -> bool:
        return self.state == "finished"


@dataclass
class Verification:
    """The verifier's verdict, with the evidence that produced it."""

    verdict: str                    # completed_verified | completed_unverified | partial | failed | blocked
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_detail(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


class Executor(Protocol):
    """Dispatch a task and observe it. No completion decision lives here."""

    def dispatch(self, task: dict) -> str: ...
    def poll(self, task: dict, conversation_id: str) -> TaskReport: ...


class Verifier(Protocol):
    def verify(self, task: dict, report: TaskReport) -> Verification: ...


# --------------------------------------------------------------------- executor


class OpenHandsExecutor:
    """Start an OpenHands conversation for a task and read its result.

    The instructions tell the agent to inspect before changing, follow repo
    instructions, run the relevant tests, and report exact commits/files. The
    executor does **not** treat the reply as proof; that is the verifier's job.
    """

    def __init__(self, client: Any, *, poll_interval: float = 2.0,
                 start_poll_attempts: int = 40):
        self.client = client
        self.poll_interval = poll_interval
        self.start_poll_attempts = start_poll_attempts

    @staticmethod
    def instructions(task: dict) -> str:
        acceptance = task.get("acceptance") or []
        criteria = "\n".join(f"- {c}" for c in acceptance) or "- (no explicit criteria)"
        repo = task.get("repository") or "(no repository)"
        branch = task.get("branch") or "(default branch)"
        return (
            "You are a coding agent working inside a repository checkout.\n"
            f"Repository: {repo}\nBranch: {branch}\n\n"
            f"Task: {task['title']}\n{task.get('description') or ''}\n\n"
            "Acceptance criteria:\n"
            f"{criteria}\n\n"
            "Rules:\n"
            "1. Inspect the repository before changing anything.\n"
            "2. Preserve unrelated user changes; do not revert others' work.\n"
            "3. Follow the repository's own instructions (README, AGENTS.md).\n"
            "4. Implement the acceptance criteria.\n"
            "5. Run the relevant tests and report their output.\n"
            "6. If configured to push, commit and push to the authorized branch.\n"
            "7. Finish with a short report: exact commits (SHA), files changed, "
            "tests run and their result, and any blocker. Do not claim success "
            "you did not verify."
        )

    def dispatch(self, task: dict) -> str:
        start = self.client.start_conversation(
            initial_message=self.instructions(task),
            selected_repository=task.get("repository") or None,
            selected_branch=task.get("branch") or None,
            title=f"lab:{task['task_key']}",
            tags={"nexusrun": str(task["run_id"])[:40], "nexustask": task["task_key"][:40]},
        )
        # Two-phase start: the conversation id appears only at READY. Wait for
        # it (bounded) so the returned id is a real conversation, not a start
        # task that a later poll would report as "missing". If the client has no
        # start-task API (e.g. a local agent-server), fall back to its id.
        conv_id = getattr(start, "app_conversation_id", None)
        if conv_id:
            return conv_id
        if hasattr(self.client, "get_start_task") and getattr(start, "id", None):
            current = start
            for _ in range(self.start_poll_attempts):
                conv_id = getattr(current, "app_conversation_id", None)
                if conv_id:
                    return conv_id
                if getattr(current, "failed", False):
                    raise CloudError(f"start task failed: {getattr(current, 'detail', '')}")
                time.sleep(self.poll_interval)
                nxt = self.client.get_start_task(current.id)
                if nxt is None:
                    raise CloudError("start task disappeared")
                current = nxt
            raise CloudError("start task not READY within the poll budget")
        return getattr(start, "id", "")

    def poll(self, task: dict, conversation_id: str) -> TaskReport:
        try:
            conv = self.client.get_conversation(conversation_id)
        except CloudNotFound:
            return TaskReport(state="missing", conversation_id=conversation_id,
                              detail="conversation not found")
        status = conv.execution_status or "unknown"
        if status in _RUNNING:
            return TaskReport(state="running", conversation_id=conversation_id)
        if status in _BAD:
            return TaskReport(state=status, conversation_id=conversation_id,
                              detail=status)
        if status == "finished":
            try:
                text = self.client.final_response(conversation_id) or ""
            except CloudError:
                text = ""
            return TaskReport(state="finished", final_text=text,
                              conversation_id=conversation_id)
        return TaskReport(state=status, conversation_id=conversation_id)


# --------------------------------------------------------------------- verifier


class GitVerifier:
    """Check a task's result against the real repository.

    The default policy: a task is `completed_verified` only when the agent
    finished *and* the verifier observed a new commit on the task's branch that
    the agent named. A finished conversation with no observable commit is
    `completed_unverified` — never verified.
    """

    def __init__(self, github: GitHubClient, *, require_commit: bool = True):
        self.github = github
        self.require_commit = require_commit

    def verify(self, task: dict, report: TaskReport) -> Verification:
        if report.state == "missing":
            return Verification("failed", "conversation vanished",
                                {"state": report.state})
        if report.state in _BAD:
            return Verification("failed", f"conversation {report.state}",
                                {"state": report.state})
        if not report.finished:
            return Verification("blocked", "conversation did not finish",
                                {"state": report.state})
        repository = task.get("repository") or ""
        branch = task.get("branch") or ""
        evidence: dict[str, Any] = {"state": "finished",
                                    "final_text": report.final_text[:2000]}
        if not repository:
            # No repository to inspect: the work cannot be independently
            # verified, so it is unverified rather than verified.
            return Verification("completed_unverified",
                                "no repository bound; cannot verify remote state",
                                evidence)
        try:
            sha = self.github.default_branch_sha(repository, branch or None)
        except (GitHubError, ValueError) as e:
            return Verification("blocked", f"github unavailable: {type(e).__name__}",
                                {**evidence, "error": str(e)})
        evidence["remote_sha"] = sha
        evidence["branch"] = branch or "(default)"
        if not sha:
            return Verification("completed_unverified",
                                "branch has no readable commit", evidence)
        # The agent should have named the commit it produced; check it is real.
        named = _extract_sha(report.final_text)
        if named:
            evidence["named_sha"] = named
            if named == sha:
                return Verification("completed_verified",
                                    "remote branch is at the reported commit", evidence)
            return Verification("completed_unverified",
                                "reported commit is not the branch head", evidence)
        if not self.require_commit:
            return Verification("completed_unverified",
                                "no commit reported", evidence)
        return Verification("completed_unverified",
                            "no verifiable commit reported by the agent", evidence)


def _extract_sha(text: str) -> str | None:
    """Find a 7-40 char hex commit SHA in the agent's report, if present."""
    import re

    for match in re.finditer(r"\b[0-9a-f]{7,40}\b", text or ""):
        token = match.group(0)
        # Skip pure-digit runs (sizes, ids) that are not hex letters.
        if any(c in "abcdef" for c in token):
            return token
    return None


# -------------------------------------------------------------------- scheduler


@dataclass
class TickResult:
    """What one scheduler tick did. Small and honest for the UI/logs."""

    reclaimed: int = 0
    claimed: str | None = None
    dispatched: bool = False
    verified: str | None = None
    verdict: str | None = None
    finished: bool = False
    #: The run is alive but ticking cannot move it: it is paused, blocked or
    #: waiting, and nothing of it is in flight. A person has to act first.
    idle: bool = False
    note: str = ""


class LabScheduler:
    """Drive one run forward by one step per tick.

    One tick = reclaim expired leases → poll what is in flight → verify and
    record → claim one claimable task → authorize → dispatch → decide (complete
    the run, or keep going). All state lives in PostgreSQL, so the loop can run
    in a separate worker process and survive a restart.

    What a tick may do depends on the run:

    * a **terminal** run (completed, failed, cancelled) is not touched at all;
    * a run that is alive but **not running** (paused, blocked, waiting) has the
      results of work already in flight collected - that work was started and
      paid for, and leaving it unread would lose the answer - and nothing new is
      started;
    * only a **running** run starts tasks, and only while it has iterations left
      and its deadline has not passed. The store checks that again inside the
      claim itself, so a pause or cancel is never overtaken.
    """

    def __init__(self, store: LabStore, executor: Executor, verifier: Verifier, *,
                 principal: str = "machine:default", worker_id: str = "lab",
                 lease_seconds: int = 900):
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.principal = principal
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def tick(self, run_id: str) -> TickResult:
        result = TickResult()
        run = self.store.run_row(run_id)
        if run is None:
            result.finished = True
            result.note = "run not found"
            return result
        if run["state"] in TERMINAL_RUN_STATES:
            result.finished = True
            result.note = f"run is {run['state']}"
            return result
        result.reclaimed = len(self.store.reclaim_expired(run_id))

        # 1. Poll every task already dispatched, so a slow conversation advances.
        in_flight = 0
        for task in self.store.in_flight_tasks(run_id):
            conv = task.get("conversation_id")
            if not conv:
                in_flight += 1
                continue
            report = self.executor.poll(task, conv)
            if report.state == "running":
                if task["running_seconds"] <= task["timeout_seconds"]:
                    in_flight += 1
                    continue
                # The task's own time limit. The conversation is left as it is
                # (its transcript is the explanation); the task is over, and it
                # is not started a second time behind the first one's back.
                verdict = Verification(
                    "failed",
                    f"timed out after {task['timeout_seconds']} s; the conversation was left running",
                    {"state": "running", "conversation_id": conv})
            else:
                verdict = self.verifier.verify(task, report)
            self.store.record_verification(
                task["id"], verdict=verdict.verdict,
                evidence={"reason": verdict.reason, **verdict.evidence},
                detail=verdict.as_detail())
            result.verified = task["task_key"]
            result.verdict = verdict.verdict

        if run["state"] != "running":
            result.idle = in_flight == 0
            result.note = f"run is {run['state']}: nothing new is started"
            return result

        # 2. Claim and dispatch the next claimable task. The claim refuses by
        #    itself when the run stopped running or is out of budget.
        task = self.store.claim_task(run_id, self.worker_id, lease_seconds=self.lease_seconds)
        if task is not None:
            result.claimed = task["task_key"]
            try:
                # The gate every other path to a conversation goes through. It
                # ran when the run was started; the link or the allowlist may
                # have changed since.
                authorize_run_task(self.store.store, project_id=str(task["project_id"]),
                                   repository=task.get("repository") or "",
                                   branch=task.get("branch") or "")
            except AuthorizationError as e:
                reason = f"not authorized: {e}"
                self.store.record_verification(
                    task["id"], verdict="blocked", evidence={"reason": reason},
                    detail={"verdict": "blocked", "reason": reason})
                applied = self.store.set_run_state_internal(
                    run_id, "blocked",
                    last_error=f"task {task['task_key']} was not started: {e}")
                result.finished = applied is not None
                result.note = "blocked: dispatch not authorized"
                return result
            try:
                conversation_id = self.executor.dispatch(task)
            except (CloudError, GitHubError) as e:
                # A dispatch failure is a provider/transport problem: release the
                # task so another attempt (or worker) can try, and keep the lease
                # short so recovery is fast.
                self.store.release_task(task["id"], error=f"{type(e).__name__}: {e}",
                                        requeue=task["attempt"] < task["max_attempts"])
                result.note = f"dispatch failed: {type(e).__name__}"
                return result
            self.store.mark_running(task["id"], conversation_id=conversation_id or None)
            result.dispatched = True

        # 3. Decide the run's fate.
        outcome = self._decide(run_id)
        if outcome is not None:
            state, last_error, note = outcome
            applied = self.store.set_run_state_internal(run_id, state, last_error=last_error)
            result.finished = applied is not None
            if applied is not None and note:
                result.note = result.note or note
        return result

    def _decide(self, run_id: str) -> tuple[str, str | None, str] | None:
        """The state the run should move to now, or ``None`` to keep going."""
        counts = self.store.count_tasks(run_id)
        total = sum(counts.values())
        if total == 0:
            # A run with nothing in it has not achieved its goal.
            return ("failed", "the run has no tasks", "failed: no tasks")
        if counts.get("completed_verified", 0) == total:
            return ("completed", None, "")
        unfinished = sum(counts.get(state, 0) for state in UNFINISHED_TASK_STATES)
        if unfinished == 0:
            # Nothing left to do and not all verified: the run is blocked, not
            # complete. A person resumes it (retry) or cancels it.
            return ("blocked", "no claimable tasks; some tasks not verified",
                    "blocked: tasks remain unverified")
        if unfinished - counts.get("pending", 0) > 0:
            return None                     # work is in flight; wait for it
        spent = self._budget_spent(run_id)
        if spent:
            # Tasks are waiting and the run may not start them any more.
            return ("failed", spent, f"failed: {spent}")
        if not self.store.has_claimable(run_id):
            # A pending task exists but can never be claimed: a dependency ended
            # unverified/failed/blocked. That is a deadlock, not progress, so the
            # run is blocked rather than left spinning.
            return ("blocked", "dependency graph deadlocked: a dependency is not verified",
                    "blocked: dependency not verified")
        return None

    def _budget_spent(self, run_id: str) -> str | None:
        """Which of the run's limits stops new work, if any."""
        run = self.store.run_row(run_id)
        if run is None:
            return None
        if run["iteration"] >= run["max_iterations"]:
            return (f"iteration budget used: {run['iteration']} of "
                    f"{run['max_iterations']} task starts")
        deadline = run.get("deadline_at")
        if deadline is not None and deadline <= datetime.now(timezone.utc):
            return "deadline passed"
        return None

    def _project_of(self, run_id: str) -> str:
        with self.store.store._tx() as cur:
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            return str(row["project_id"]) if row else ""

    def run_until_done(self, run_id: str, *, max_ticks: int = 500,
                       sleep: float = 0.0) -> TickResult:
        """Loop ticks until the run finishes or the tick budget is spent."""
        last = TickResult()
        for _ in range(max_ticks):
            last = self.tick(run_id)
            if last.finished or last.idle:
                return last
            if sleep:
                time.sleep(sleep)
        last.note = last.note or "tick budget exhausted"
        return last
