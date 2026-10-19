"""Dispatcher: connects the durable control plane to the OpenHands runtime.

One tick does four things, in this order:

1. **Reconcile** — before dispatching anything, compare every in-flight
   assignment against the runtime. This is what makes a restart safe: the
   dispatcher adopts the conversation it created last time instead of creating
   a second one.
2. **Recover** — expired leases become RECOVERING, or DEAD_LETTER when a task
   has used up its attempts.
3. **Advance** — for each active task with a conversation, read its real
   status and move the task only on confirmed evidence.
4. **Dispatch** — claim queued work and give it to a conversation.

The dispatcher never treats a missing answer as completion, and never starts a
task whose result it cannot verify.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .control_plane import (
    ControlPlane,
    InvalidTransition,
    LeaseConflict,
    Task,
    TaskState,
)
from .openhands_client import (
    ApiUnavailable,
    ConversationGone,
    OpenHandsClient,
    Outcome,
    task_conversation_id,
)
from .runtime_state import (
    DEFAULT_DESIRED_STATE_PATH,
    DesiredStateError,
    RuntimeReadiness,
)

log = logging.getLogger(__name__)


@dataclass
class TickReport:
    """What one dispatcher tick did. Returned so tests can assert on it."""

    reconciled: list[str] = field(default_factory=list)
    reclaimed: list[dict] = field(default_factory=list)
    dispatched: list[dict] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    requeued: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "reclaimed": self.reclaimed,
            "dispatched": self.dispatched,
            "completed": self.completed,
            "failed": self.failed,
            "blocked": self.blocked,
            "requeued": self.requeued,
            "unavailable": self.unavailable,
        }


class Dispatcher:
    def __init__(
        self,
        cp: ControlPlane,
        client: OpenHandsClient,
        *,
        worker_id: str,
        workspace_root: str,
        lease_seconds: float = 300.0,
        session_ttl: float = 120.0,
        desired_state_path: str | None = str(DEFAULT_DESIRED_STATE_PATH),
    ):
        self.cp = cp
        self.client = client
        self.worker_id = worker_id
        self.workspace_root = workspace_root.rstrip("/")
        self.lease_seconds = lease_seconds
        self.session_ttl = session_ttl
        # Fetched once and reused: the runtime requires agent_settings on every
        # create, and re-reading it per task would be wasteful.
        self._agent_settings: dict[str, Any] | None = None
        # The readiness barrier closes the init race: it applies and verifies
        # the durable desired state before any conversation is created, instead
        # of trusting the async SessionStart hook that runs too late.
        self.readiness = (
            RuntimeReadiness(client, desired_state_path)
            if desired_state_path
            else None
        )
        self._ready = self.readiness is None

    def _settings(self) -> dict[str, Any]:
        if self._agent_settings is None:
            self._agent_settings = self.client.agent_settings()
        return self._agent_settings

    def _ensure_ready(self) -> tuple[bool, str | None]:
        """Make the runtime match the durable desired state before dispatch.

        Returns ``(ready, reason)``. The settings snapshot is re-read whenever
        the barrier actually writes, so a conversation created afterwards
        carries the registration the barrier just verified — that is what makes
        the race impossible rather than merely unlikely.
        """
        if self._ready:
            return True, None
        assert self.readiness is not None
        report = self.readiness.ensure()
        if report.applied:
            # The write changed the store; drop the cached snapshot so the next
            # create forwards the verified state, not a pre-write one.
            self._agent_settings = None
        if report.ready:
            self._ready = True
            return True, None
        return False, report.reason

    # --------------------------------------------------------------- public
    def tick(self, *, max_dispatch: int = 5) -> TickReport:
        """Run one full cycle. Safe to call repeatedly; safe after a crash."""
        report = TickReport()
        self.cp.register_session(self.worker_id, role="dispatcher")
        self.cp.heartbeat(self.worker_id, state="active")

        self._reconcile(report)
        self._advance_active(report)
        self._dispatch_queued(report, max_dispatch=max_dispatch)

        self.cp.heartbeat(self.worker_id, state="idle")
        return report

    def _touched(self, report: TickReport) -> set[str]:
        """Tasks this tick already moved. They are not re-dispatched now.

        Without this a task that just failed would be claimed again in the same
        tick, turning retry into a hot loop.
        """
        return set(report.requeued) | set(report.completed) | set(report.failed) | set(
            report.blocked
        )

    # ------------------------------------------------------------ internals
    def _reconcile(self, report: TickReport) -> None:
        """Reclaim dead leases before looking at the outside world."""
        report.reclaimed = self.cp.reclaim_expired()

    def _advance_active(self, report: TickReport) -> None:
        """Read each active task's conversation and move it on real evidence."""
        for task in self.cp.list_tasks():
            if task.state not in (
                TaskState.ASSIGNED,
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.RECOVERING,
            ):
                continue
            if not task.conversation_id:
                # Assigned but never bound: the crash happened between claim and
                # create. Requeue so the next tick redoes it idempotently.
                self._requeue(task, report, reason="unbound assignment")
                continue
            if task.owner != self.worker_id and task.lease_live:
                # Another live worker owns it. Leave it alone.
                continue
            try:
                view = self.client.get_conversation(task.conversation_id)
            except ApiUnavailable:
                # Transient. Keep the lease; try again next tick. Not a failure.
                report.unavailable.append(task.id)
                continue
            except ConversationGone:
                self._handle_gone(task, report)
                continue

            report.reconciled.append(task.id)
            self._apply_outcome(task, view.outcome, report)

    def _apply_outcome(
        self, task: Task, outcome: Outcome, report: TickReport
    ) -> None:
        if outcome is Outcome.RUNNING:
            if task.state is TaskState.ASSIGNED:
                self.cp.transition(task.id, TaskState.RUNNING, owner=task.owner)
            self.cp.renew_lease(task.id, task.owner or self.worker_id,
                                lease_seconds=self.lease_seconds)
            return

        if outcome is Outcome.IDLE:
            # Conversation exists but is not running. If we still hold the task,
            # it means the loop stopped without finishing: requeue safely.
            if task.state is TaskState.RUNNING:
                self._requeue(task, report, reason="stopped without finishing")
            return

        if outcome is Outcome.BLOCKED:
            # Waiting for a human. Ambiguous, so it goes to blocked, not failed.
            if task.state is not TaskState.BLOCKED:
                self.cp.transition(task.id, TaskState.BLOCKED, owner=task.owner)
            report.blocked.append(task.id)
            return

        if outcome is Outcome.FAILED:
            self._fail_from_runtime(task, report)
            return
        if outcome is Outcome.FINISHED:
            self._verify_finished(task, report)
            return

    def _fail_from_runtime(self, task: Task, report: TickReport) -> None:
        """Fail a task, but do not retry an error the runtime calls permanent.

        The runtime classifies its own failures. When it says
        ``retryable: false`` (bad credentials, bad config), retrying cannot
        succeed and would only consume the attempt budget and delay the
        dead-letter signal a human needs to see.
        """
        classification: dict[str, Any] | None = None
        try:
            classification = self.client.failure_classification(
                task.conversation_id or ""
            )
        except ApiUnavailable:
            # Cannot classify right now; fall through to the normal bounded
            # retry, which is the safe default.
            pass
        except ConversationGone:
            self._handle_gone(task, report)
            return

        if classification is not None and classification.get("retryable") is False:
            self.cp.transition(
                task.id,
                TaskState.FAILED,
                evidence=[
                    {
                        "reason": "non-retryable runtime failure",
                        "kind": classification.get("kind"),
                        "user_action": classification.get("user_action"),
                    }
                ],
            )
            self.cp.transition(task.id, TaskState.DEAD_LETTER)
            report.failed.append(task.id)
            return

        self._fail(task, report, reason="runtime reported failure")

    def _verify_finished(self, task: Task, report: TickReport) -> None:
        """Only complete a task when the runtime produced a real result."""
        if task.state is TaskState.RUNNING:
            self.cp.transition(task.id, TaskState.VERIFYING, owner=task.owner)
        try:
            result = self.client.final_response(task.conversation_id or "")
        except ApiUnavailable:
            report.unavailable.append(task.id)
            return
        except ConversationGone:
            self._handle_gone(task, report)
            return

        if result is None:
            # Finished with nothing to show. Ambiguous: a human must look.
            self.cp.transition(
                task.id,
                TaskState.BLOCKED,
                evidence=[{"reason": "finished without a final response"}],
            )
            report.blocked.append(task.id)
            return

        self.cp.set_result(task.id, result)
        self.cp.transition(
            task.id,
            TaskState.COMPLETED,
            result=result,
            evidence=[{"reason": "final response verified", "chars": len(result)}],
        )
        report.completed.append(task.id)

    def _handle_gone(self, task: Task, report: TickReport) -> None:
        """Conversation vanished. Requeue within the attempt budget."""
        self._fail(task, report, reason="conversation gone")

    def _fail(self, task: Task, report: TickReport, *, reason: str) -> None:
        """Record a failure and either retry or dead-letter."""
        attempt = task.attempt + 1
        if attempt >= task.max_attempts:
            self.cp.transition(
                task.id,
                TaskState.DEAD_LETTER,
                evidence=[{"reason": reason, "attempt": attempt}],
            )
            report.failed.append(task.id)
            return
        # FAILED increments the attempt counter; the next tick may reclaim it.
        self.cp.transition(
            task.id,
            TaskState.FAILED,
            evidence=[{"reason": reason, "attempt": attempt}],
        )
        self.cp.transition(task.id, TaskState.QUEUED)
        report.requeued.append(task.id)

    def _requeue(self, task: Task, report: TickReport, *, reason: str) -> None:
        try:
            if task.state in (TaskState.ASSIGNED, TaskState.RUNNING):
                self.cp.transition(
                    task.id,
                    TaskState.FAILED,
                    evidence=[{"reason": reason}],
                )
            self.cp.transition(task.id, TaskState.QUEUED)
            report.requeued.append(task.id)
        except InvalidTransition:
            # Already moved by another path; nothing to do.
            log.debug("requeue skipped for %s in state %s", task.id, task.state)

    def _dispatch_queued(self, report: TickReport, *, max_dispatch: int) -> None:
        """Claim queued work and hand it to a conversation, idempotently."""
        queued = self.cp.list_tasks(state=TaskState.QUEUED)
        if not queued:
            return
        # Readiness gate. Checked only when there is work to dispatch, so a
        # steady-state tick with an empty queue costs nothing. When the runtime
        # is not ready the tasks stay queued and untouched: no lease is taken
        # and no attempt is burned, because this is an environment problem, not
        # the task's.
        try:
            ready, reason = self._ensure_ready()
        except DesiredStateError as exc:
            log.error("desired state unusable, not dispatching: %s", exc)
            report.unavailable.extend(t.id for t in queued)
            return
        if not ready:
            log.warning("runtime not ready, deferring dispatch: %s", reason)
            report.unavailable.extend(t.id for t in queued)
            return

        dispatched = 0
        touched = self._touched(report)
        for task in self.cp.list_tasks(state=TaskState.QUEUED):
            if dispatched >= max_dispatch:
                break
            if task.id in touched:
                continue
            if task.requires_approval and task.approved is not True:
                # Approval gate is respected here, not bypassed.
                continue
            try:
                claimed = self.cp.claim_task(
                    task.id, self.worker_id, lease_seconds=self.lease_seconds
                )
            except (LeaseConflict, InvalidTransition):
                continue

            try:
                created = self._ensure_conversation(claimed)
            except ApiUnavailable:
                # Could not reach the runtime. Release the lease so a later tick
                # retries; do not count this as an attempt against the task.
                report.unavailable.append(claimed.id)
                self._release(claimed)
                continue

            if not created:
                continue
            dispatched += 1
            # Read back the bound id: `claimed` predates the bind, so its
            # conversation_id is still None.
            bound = self.cp.get_task(claimed.id)
            report.dispatched.append(
                {"task": bound.id, "conversation_id": bound.conversation_id}
            )

    def _ensure_conversation(self, task: Task) -> bool:
        """Create-or-adopt the conversation, then start it. Idempotent.

        The conversation id is derived from the task id, so a restart asks for
        the same id and the server returns the existing conversation instead of
        making a new one.
        """
        conversation_id = task.conversation_id or task_conversation_id(task.id)
        working_dir = f"{self.workspace_root}/{conversation_id}"

        view, created = self.client.create_conversation(
            conversation_id=conversation_id,
            working_dir=working_dir,
            title=f"task:{task.id}",
            agent_settings=self._settings(),
        )
        # Bind before starting: a crash here still leaves the link durable.
        if task.conversation_id != conversation_id:
            self.cp.bind_conversation(task.id, conversation_id)

        if created:
            self.client.send_task(conversation_id, task.goal, run=True)
        else:
            # Adopted an existing conversation. Nudge the loop; a 409 means it
            # was already running, which is exactly what we want.
            self.client.start(conversation_id)

        current = self.cp.get_task(task.id)
        if current.state is TaskState.ASSIGNED:
            self.cp.transition(task.id, TaskState.RUNNING, owner=self.worker_id)
        return True

    def _release(self, task: Task) -> None:
        try:
            self.cp.transition(task.id, TaskState.QUEUED)
        except InvalidTransition:
            log.debug("release skipped for %s in state %s", task.id, task.state)
