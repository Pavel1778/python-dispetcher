"""Cloud dispatcher: drives Sr/Jr tasks through OpenHands Cloud conversations.

One tick, in order:

1. **Recover** — expired leases become RECOVERING (or DEAD_LETTER when out of
   attempts). This is what makes a crash safe.
2. **Advance in-flight** — for each task with a recorded conversation, poll the
   real Cloud status and move the task only on confirmed evidence. A missing
   answer is never completion.
3. **Dispatch queued** — atomically claim ready tasks and start a Cloud
   conversation for each, up to ``max_dispatch``.

Design points that matter:

* **Two-phase creation.** ``POST /api/v1/app-conversations`` returns a *start
  task*; the conversation id arrives later as ``app_conversation_id`` when the
  start task reaches ``READY``. The dispatcher records the start-task id first
  (so a crash mid-start is recoverable) and polls until READY or ERROR.
* **No duplicate conversations.** ``conversations.task_id`` is UNIQUE, and the
  dispatcher always checks for an existing row before creating.
* **Bounded retries with backoff.** Failures increment ``attempt`` through
  ``transition(FAILED)``; a task is dead-lettered when ``attempt >=
  max_attempts``. Retryable transport errors keep the lease and do not burn an
  attempt.
* **Repository allowlist.** A task may only run in a repository and branch that
  the allowlist permits (``repositories`` table). Nothing else is dispatched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .cloud_client import (
    CloudAuthError,
    CloudError,
    CloudNotFound,
    CloudUnavailable,
    CloudValidationError,
    CloudClient,
    ExecutionStatus,
)
from .cloud_store import (
    InvalidTransition,
    LeaseConflict,
    NotFound,
    PostgresStore,
    Task,
    TaskState,
)
from .nexus_binding import AuthorizationError, authorize_dispatch

log = logging.getLogger(__name__)


@dataclass
class TickReport:
    """What one dispatcher tick did. Returned so tests and callers can assert."""

    reclaimed: list[dict] = field(default_factory=list)
    started: list[dict] = field(default_factory=list)
    advanced: list[dict] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    requeued: list[str] = field(default_factory=list)
    dead_letter: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reclaimed": self.reclaimed,
            "started": self.started,
            "advanced": self.advanced,
            "completed": self.completed,
            "failed": self.failed,
            "blocked": self.blocked,
            "requeued": self.requeued,
            "dead_letter": self.dead_letter,
            "unavailable": self.unavailable,
        }


class RepoNotAllowed(RuntimeError):
    """The task asks for a repository or branch outside the allowlist."""


class CloudDispatcher:
    def __init__(
        self,
        store: PostgresStore,
        client: CloudClient,
        *,
        worker_id: str,
        lease_seconds: float = 300.0,
        start_poll_attempts: int = 30,
        start_poll_interval: float = 2.0,
    ):
        self.store = store
        self.client = client
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.start_poll_attempts = start_poll_attempts
        self.start_poll_interval = start_poll_interval
        # One action per task per tick. Without this a task that just failed
        # would be re-claimed and re-dispatched inside the same tick.
        self._touched: set[str] = set()

    # --------------------------------------------------------------- helpers
    def _allowed(self, task: Task) -> bool:
        """A task with a repository must match the allowlist; else it may run
        repository-less (analysis-only work).

        For a project-scoped task the allowlist alone is not enough: the
        repository must be linked to the task's project. That stronger check is
        :func:`authorize_dispatch`, called in ``_start_task`` right before the
        conversation is created.
        """
        if not task.selected_repository:
            return True
        repo = self.store.allowed_repository(task.selected_repository)
        if repo is None:
            return False
        if task.selected_branch:
            allowed = repo.get("allowed_branches") or []
            # An empty list means "default branch only".
            if allowed and task.selected_branch not in allowed:
                return False
            if not allowed and task.selected_branch != repo["default_branch"]:
                return False
        return True

    def _outcome(self, execution_status: str | None) -> str:
        if execution_status == ExecutionStatus.FINISHED.value:
            return "finished"
        if execution_status in (ExecutionStatus.ERROR.value, ExecutionStatus.STUCK.value):
            return "failed"
        if execution_status == ExecutionStatus.WAITING_FOR_CONFIRMATION.value:
            return "blocked"
        if execution_status == ExecutionStatus.RUNNING.value:
            return "running"
        return "idle"

    # ------------------------------------------------------------------ tick
    def tick(self, *, max_dispatch: int = 5) -> TickReport:
        report = TickReport()
        self._touched = set()
        report.reclaimed = self.store.reclaim_expired()
        self._advance_active(report)
        self._dispatch_queued(report, max_dispatch=max_dispatch)
        return report

    # ------------------------------------------------------------ dispatch
    def _dispatch_queued(self, report: TickReport, *, max_dispatch: int) -> None:
        for _ in range(max_dispatch):
            try:
                task = self.store.claim_task(self.worker_id, lease_seconds=self.lease_seconds)
            except LeaseConflict:
                continue
            except InvalidTransition:
                continue
            if task is None:
                return
            if task.id in self._touched:
                # Already handled earlier this tick; release and stop.
                try:
                    self.store.force_transition(task.id, TaskState.QUEUED)
                except (InvalidTransition, NotFound):
                    pass
                continue
            self._touched.add(task.id)
            try:
                self._start_task(task, report)
            except RepoNotAllowed as e:
                self.store.force_transition(
                    task.id, TaskState.DEAD_LETTER,
                    evidence=[{"reason": "repository not allowed", "detail": str(e)}],
                )
                report.dead_letter.append(task.id)
            except CloudAuthError as e:
                # Retrying cannot fix a bad key: dead-letter at once, keep evidence.
                self.store.force_transition(
                    task.id, TaskState.DEAD_LETTER,
                    evidence=[{"reason": "cloud auth failure", "detail": str(e)}],
                )
                report.dead_letter.append(task.id)
            except CloudValidationError as e:
                self.store.force_transition(
                    task.id, TaskState.DEAD_LETTER,
                    evidence=[{"reason": "cloud validation failure", "detail": str(e)}],
                )
                report.dead_letter.append(task.id)
            except CloudUnavailable as e:
                # Transient: release the task back to the queue without burning
                # an attempt so the next tick retries it.
                self._requeue(task, report, reason=f"cloud unavailable: {e}")
            except CloudError as e:
                self._fail(task, report, reason=f"cloud error: {e}")

    def _task_tag(self, task: Task) -> tuple[str, str]:
        """The Cloud tag that lets us find a conversation we started but lost.

        A crash between ``start_conversation`` and ``bind_conversation`` leaves a
        real conversation with no local row. Without a tag, the retry would start
        a second one. The tag is stable per task, so the orphan can be found and
        reused.
        """
        return "orchestratortaskid", task.id.replace("-", "")

    def _find_orphan(self, task: Task) -> str | None:
        """Return a Cloud conversation already tagged for this task, if any."""
        key, value = self._task_tag(task)
        items, _ = self.client.search_conversations(limit=50, tag=(key, value))
        for conv in items:
            if conv.tags.get(key) == value:
                return conv.id
        return None

    def _start_task(self, task: Task, report: TickReport) -> None:
        if not self._allowed(task):
            raise RepoNotAllowed(
                f"{task.selected_repository}@{task.selected_branch or 'default'}"
            )
        # Re-check the full project/repository authorization immediately before
        # the conversation is created. The allowlist above is necessary but not
        # sufficient for a project-scoped task: the repository must be linked to
        # the task's project, the workflow must share the project, and the task
        # must still belong to the project it was queued under.
        try:
            authorize_dispatch(self.store, task=task)
        except AuthorizationError as e:
            raise RepoNotAllowed(str(e)) from e
        existing = self.store.conversation_for(task.id)
        if existing and existing.get("external_conversation_id"):
            # Already started on a previous attempt: just move to running.
            self.store.transition(task.id, TaskState.RUNNING, owner=self.worker_id)
            report.started.append({"task": task.id,
                                   "conversation": existing["external_conversation_id"],
                                   "reused": True})
            return

        # A previous attempt recorded a start task but not the conversation id
        # (crash mid-start). Resume that start task instead of starting a second.
        if existing and existing.get("external_start_task_id"):
            start = self.client.get_start_task(existing["external_start_task_id"])
            if start is not None and not start.failed:
                conversation_id = self._await_start(start)
                self.store.bind_conversation(
                    task.id, conversation_id,
                    start_task_id=start.id, status="running",
                )
                self.store.transition(task.id, TaskState.RUNNING, owner=self.worker_id)
                report.started.append({"task": task.id, "conversation": conversation_id,
                                       "resumed": True})
                return
            # Start task is gone or errored: fall through to the orphan search.

        # A previous attempt may have created a conversation we never recorded.
        orphan = self._find_orphan(task)
        if orphan is not None:
            self.store.bind_conversation(task.id, orphan, status="running")
            self.store.transition(task.id, TaskState.RUNNING, owner=self.worker_id)
            report.started.append({"task": task.id, "conversation": orphan,
                                   "recovered": True})
            return

        key, value = self._task_tag(task)
        start = self.client.start_conversation(
            initial_message=task.goal,
            selected_repository=task.selected_repository,
            selected_branch=task.selected_branch,
            title=(task.goal[:60] or None),
            tags={key: value},
        )
        # Record the start task before polling so a crash here is recoverable.
        self.store.bind_conversation(
            task.id, None, start_task_id=start.id, status="starting"
        )
        report.started.append({"task": task.id, "start_task": start.id})

        conversation_id = self._await_start(start)
        self.store.bind_conversation(
            task.id, conversation_id, start_task_id=start.id, status="running"
        )
        self.store.transition(task.id, TaskState.RUNNING, owner=self.worker_id)

    def _await_start(self, start) -> str:
        """Poll the start task until it is READY, returning the conversation id."""
        current = start
        for _ in range(self.start_poll_attempts):
            if current.app_conversation_id:
                return current.app_conversation_id
            if current.failed:
                raise CloudError(f"start task failed: {current.detail or current.status}")
            time.sleep(self.start_poll_interval)
            nxt = self.client.get_start_task(current.id)
            if nxt is None:
                raise CloudUnavailable("start task disappeared")
            current = nxt
        if current.app_conversation_id:
            return current.app_conversation_id
        raise CloudUnavailable(f"start task not READY after {self.start_poll_attempts} polls")

    # -------------------------------------------------------------- advance
    def _advance_active(self, report: TickReport) -> None:
        for task in self.store.list_tasks():
            if task.state not in (TaskState.ASSIGNED, TaskState.RUNNING,
                                  TaskState.VERIFYING, TaskState.RECOVERING):
                continue
            conv = self.store.conversation_for(task.id)
            if not conv or not conv.get("external_conversation_id"):
                # Start never completed; recovery will requeue it.
                continue
            self._touched.add(task.id)
            cid = conv["external_conversation_id"]
            try:
                view = self.client.get_conversation(cid)
            except CloudNotFound:
                self._requeue(task, report, reason="conversation gone")
                continue
            except CloudUnavailable:
                report.unavailable.append(task.id)
                continue

            outcome = self._outcome(view.execution_status)
            report.advanced.append({"task": task.id, "status": view.execution_status})

            if outcome == "finished":
                self._verify_finished(task, report)
            elif outcome == "failed":
                self._fail(task, report, reason=f"runtime {view.execution_status}")
            elif outcome == "blocked":
                self.store.transition(
                    task.id, TaskState.BLOCKED,
                    evidence=[{"reason": "waiting for confirmation"}],
                )
                report.blocked.append(task.id)
            # running/idle: nothing to do, the next tick re-checks.

    def _verify_finished(self, task: Task, report: TickReport) -> None:
        """A finished conversation is only complete with a non-empty answer."""
        conv = self.store.conversation_for(task.id)
        try:
            answer = self.client.final_response(conv["external_conversation_id"])
        except CloudError:
            report.unavailable.append(task.id)
            return
        if not answer:
            # Missing answer is not success.
            self.store.transition(
                task.id, TaskState.BLOCKED,
                evidence=[{"reason": "finished with no final response"}],
            )
            report.blocked.append(task.id)
            return
        if task.state is not TaskState.VERIFYING:
            try:
                self.store.transition(task.id, TaskState.VERIFYING, owner=self.worker_id)
            except InvalidTransition:
                pass
        self.store.set_result(task.id, answer)
        self.store.transition(task.id, TaskState.COMPLETED, owner=self.worker_id)
        report.completed.append(task.id)

    # ------------------------------------------------------------ recovery
    def _fail(self, task: Task, report: TickReport, *, reason: str) -> None:
        current = self.store.get_task(task.id)
        if current.attempt + 1 >= current.max_attempts:
            self.store.force_transition(
                task.id, TaskState.DEAD_LETTER,
                evidence=[{"reason": reason, "attempt": current.attempt + 1}],
            )
            report.dead_letter.append(task.id)
        else:
            self.store.transition(
                task.id, TaskState.FAILED,
                evidence=[{"reason": reason}],
            )
            report.failed.append(task.id)

    def _requeue(self, task: Task, report: TickReport, *, reason: str) -> None:
        """Return a task to the queue without burning an attempt.

        ``force_transition`` walks a legal route (assigned -> failed -> queued)
        because the state machine has no direct assigned -> queued edge.
        """
        try:
            self.store.force_transition(
                task.id, TaskState.QUEUED, evidence=[{"reason": reason}]
            )
        except (InvalidTransition, NotFound):
            pass
        report.requeued.append(task.id)
