"""Dispatcher core.

Responsibilities:
* ``dispatch``  - claim due tasks and create OpenHands conversations.
* ``advance``   - poll start tasks / conversations and move tasks forward.
* ``reconcile`` - resolve tasks whose local state may be stale (restart-safe).
* ``recover``   - re-queue tasks left non-terminal by a previous process.
* ``verify``    - decide whether a workflow may legitimately be COMPLETED.

Important design rule: a workflow is never marked COMPLETED unless every
expected Jr child task reached COMPLETED and the Sr verification step passed.
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..logging_config import get_logger, safe_extra
from ..openhands_client import (
    Conversation,
    OpenHandsAuthError,
    OpenHandsClient,
    OpenHandsError,
    OpenHandsRateLimitError,
    OpenHandsRequestError,
    OpenHandsTransientError,
    StartTask,
)
from ..status import (
    EXECUTION_ERROR,
    EXECUTION_FINISHED,
    EXECUTION_STUCK,
    START_TASK_ERROR,
    START_TASK_READY,
    TASK_STATUSES_NON_TERMINAL,
    TaskRole,
    TaskStatus,
    WorkflowStatus,
)
from ..store.base import StateStore, StoreUnavailableError
from ..store.models import Task

logger = get_logger(__name__)


class JrBridgeUnavailableError(RuntimeError):
    """Raised when a Sr workflow requests Jr creation but no bridge is wired.

    The bridge is the explicit mechanism by which a Sr task spawns Jr child
    conversations. Without it (no ``JR_BRIDGE_TOKEN`` / endpoint), Sr plans are
    not executed - the workflow is marked BLOCKED instead of silently claiming
    success.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Dispatcher:
    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        client: OpenHandsClient,
        worker_id: str | None = None,
        now: Any | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._worker_id = worker_id or settings.dispatcher_worker_id or _default_worker_id()
        self._now = now or _utcnow
        # Set True only after a full dispatch cycle completes without error.
        self.last_cycle_succeeded: bool = False
        self.last_cycle_at: datetime | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ------------------------------------------------------------------
    # recovery / reconciliation
    # ------------------------------------------------------------------

    async def recover(self) -> int:
        """Re-queue tasks left non-terminal by a previous process.

        Runs once at startup. Any task still holding a lease is released so it
        can be re-claimed; tasks mid-start get a fresh poll rather than a new
        conversation (idempotency is preserved by the stored start_task_id).
        """
        recovered = 0
        try:
            candidates = await self._store.list_tasks_by_status(list(TASK_STATUSES_NON_TERMINAL))
        except StoreUnavailableError:
            logger.warning("recover skipped: store unavailable")
            return 0
        for task in candidates:
            if task.app_conversation_id or task.start_task_id:
                # Already linked to a remote conversation: reconcile, don't recreate.
                continue
            if task.attempts >= task.max_attempts:
                continue
            await self._store.update_task(
                task.id,
                status=TaskStatus.QUEUED.value,
                next_attempt_at=self._now(),
                release_lease=True,
            )
            recovered += 1
            await self._store.record_event(
                event_type="task_recovered",
                workflow_id=task.workflow_id,
                task_id=task.id,
                message="task re-queued after restart",
            )
        if recovered:
            logger.info("recovered tasks after restart", **safe_extra(count=recovered))
        return recovered

    async def reconcile(self) -> int:
        """Resolve stale state for tasks already linked to remote objects.

        Polls start tasks / conversations and updates local rows, so a restart
        never creates a duplicate conversation for work that already started.
        """
        reconciled = 0
        try:
            tasks = await self._store.list_tasks_by_status(list(TASK_STATUSES_NON_TERMINAL))
        except StoreUnavailableError:
            logger.warning("reconcile skipped: store unavailable")
            return 0
        for task in tasks:
            if task.status == TaskStatus.QUEUED.value:
                continue
            try:
                if task.start_task_id and not task.app_conversation_id:
                    await self._advance_start_task(task)
                elif task.app_conversation_id:
                    await self._advance_conversation(task)
                reconciled += 1
            except OpenHandsError as exc:
                logger.warning(
                    "reconcile error",
                    **safe_extra(task_id=task.id, error=type(exc).__name__),
                )
        return reconciled

    # ------------------------------------------------------------------
    # main cycle
    # ------------------------------------------------------------------

    async def run_cycle(self) -> dict[str, int]:
        """One full dispatch cycle. Sets ``last_cycle_succeeded`` on success."""
        stats = {"dispatched": 0, "advanced": 0, "reconciled": 0, "verified": 0}
        try:
            stats["dispatched"] = await self.dispatch()
            stats["advanced"] = await self.advance()
            stats["reconciled"] = await self.reconcile()
            stats["verified"] = await self.verify_workflows()
        except StoreUnavailableError:
            self.last_cycle_succeeded = False
            logger.warning("dispatch cycle aborted: store unavailable")
            return stats
        self.last_cycle_succeeded = True
        self.last_cycle_at = self._now()
        return stats

    async def dispatch(self) -> int:
        """Claim due tasks and start their conversations. Idempotent."""
        claimed = await self._store.claim_due_tasks(
            worker_id=self._worker_id,
            lease_seconds=self._settings.dispatcher_lease_seconds,
            limit=self._settings.dispatcher_batch_size,
        )
        dispatched = 0
        for task in claimed:
            if task.app_conversation_id:
                # Already started in a previous cycle: advance, never restart.
                await self._advance_conversation(task)
                continue
            if task.start_task_id:
                await self._advance_start_task(task)
                continue
            await self._start_task(task)
            dispatched += 1
        return dispatched

    async def _start_task(self, task: Task) -> None:
        try:
            start = await self._client.start_conversation(
                title=task.title,
                initial_message=task.prompt,
                selected_repository=task.selected_repository,
                selected_branch=task.selected_branch,
                tags={"dispatcher_task_id": task.id, "dispatcher_role": task.role},
            )
        except (OpenHandsRateLimitError, OpenHandsTransientError) as exc:
            await self._defer(task, exc)
            return
        except OpenHandsAuthError:
            await self._fail(task, "authentication failed", status=TaskStatus.BLOCKED.value)
            return
        except OpenHandsRequestError as exc:
            await self._fail(task, f"request rejected: {exc}")
            return

        await self._store.update_task(
            task.id,
            status=TaskStatus.STARTING.value,
            start_task_id=start.id,
            app_conversation_id=start.app_conversation_id,
            sandbox_id=start.sandbox_id,
            started_at=task.started_at or self._now(),
            release_lease=True,
        )
        await self._store.record_event(
            event_type="conversation_start_requested",
            workflow_id=task.workflow_id,
            task_id=task.id,
            message="start task created",
            payload={"start_task_id": start.id, "status": start.status},
        )
        logger.info(
            "conversation start requested",
            **safe_extra(task_id=task.id, start_task_id=start.id),
        )
        if start.app_conversation_id:
            await self._store.update_task(
                task.id,
                status=TaskStatus.RUNNING.value,
                app_conversation_id=start.app_conversation_id,
            )

    async def advance(self) -> int:
        """Poll tasks that have a start task or conversation and move them on."""
        advanced = 0
        for status in (
            TaskStatus.STARTING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.VERIFYING.value,
        ):
            tasks = await self._store.list_tasks_by_status([status])
            for task in tasks:
                try:
                    if task.app_conversation_id:
                        await self._advance_conversation(task)
                    elif task.start_task_id:
                        await self._advance_start_task(task)
                    advanced += 1
                except (OpenHandsRateLimitError, OpenHandsTransientError) as exc:
                    await self._defer(task, exc)
                except OpenHandsError as exc:
                    logger.warning(
                        "advance error",
                        **safe_extra(task_id=task.id, error=type(exc).__name__),
                    )
        return advanced

    async def _advance_start_task(self, task: Task) -> None:
        start = await self._client.get_start_task(task.start_task_id or "")
        if start is None:
            await self._defer(task, OpenHandsTransientError("start task not found yet"))
            return
        await self._apply_start_task(task, start)

    async def _apply_start_task(self, task: Task, start: StartTask) -> None:
        if start.status == START_TASK_ERROR:
            await self._fail(task, f"start task error: {start.detail or 'unknown'}")
            return
        if start.status == START_TASK_READY and start.app_conversation_id:
            await self._store.update_task(
                task.id,
                status=TaskStatus.RUNNING.value,
                app_conversation_id=start.app_conversation_id,
                sandbox_id=start.sandbox_id,
                release_lease=True,
            )
            await self._store.record_event(
                event_type="conversation_ready",
                workflow_id=task.workflow_id,
                task_id=task.id,
                payload={"app_conversation_id": start.app_conversation_id},
            )
            logger.info(
                "conversation ready",
                **safe_extra(task_id=task.id, conversation_id=start.app_conversation_id),
            )
            return
        # Still progressing (WORKING/WAITING_FOR_SANDBOX/...): keep waiting.
        await self._store.update_task(
            task.id,
            status=TaskStatus.STARTING.value,
            start_task_id=start.id,
            next_attempt_at=self._now()
            + timedelta(seconds=self._settings.dispatcher_poll_interval_seconds),
            release_lease=True,
        )

    async def _advance_conversation(self, task: Task) -> None:
        conv = await self._client.get_conversation(task.app_conversation_id or "")
        if conv is None:
            await self._defer(task, OpenHandsTransientError("conversation not found yet"))
            return
        await self._apply_conversation(task, conv)

    async def _apply_conversation(self, task: Task, conv: Conversation) -> None:
        execution = conv.execution_status
        if execution == EXECUTION_ERROR:
            await self._fail(task, "conversation reported error")
            return
        if execution == EXECUTION_STUCK:
            await self._fail(task, "conversation stuck")
            return
        if execution == EXECUTION_FINISHED:
            await self._store.update_task(
                task.id,
                status=TaskStatus.VERIFYING.value,
                sandbox_id=conv.sandbox_id,
                release_lease=True,
            )
            await self._store.record_event(
                event_type="conversation_finished",
                workflow_id=task.workflow_id,
                task_id=task.id,
                message="conversation execution finished; verification pending",
            )
            return
        # idle / running / paused / waiting_for_confirmation -> keep polling.
        await self._store.update_task(
            task.id,
            status=TaskStatus.RUNNING.value,
            sandbox_id=conv.sandbox_id,
            next_attempt_at=self._now()
            + timedelta(seconds=self._settings.dispatcher_poll_interval_seconds),
            release_lease=True,
        )

    # ------------------------------------------------------------------
    # verification (guards against a false COMPLETED)
    # ------------------------------------------------------------------

    async def verify_workflows(self) -> int:
        """Advance workflows to COMPLETED only when children really finished."""
        completed = 0
        for status in (WorkflowStatus.RUNNING.value, WorkflowStatus.VERIFYING.value):
            workflows = await self._store.list_workflows(statuses=[status])
            for wf in workflows:
                if await self._verify_workflow(wf.id):
                    completed += 1
        return completed

    async def _verify_workflow(self, workflow_id: str) -> bool:
        tasks = await self._store.list_tasks_for_workflow(workflow_id)
        if not tasks:
            return False
        sr_tasks = [t for t in tasks if t.role == TaskRole.SR.value]
        jr_tasks = [t for t in tasks if t.role == TaskRole.JR.value]
        if not sr_tasks:
            return False
        sr = sr_tasks[0]

        # Every Jr child must be COMPLETED; none may be failed/blocked/running.
        if any(t.status != TaskStatus.COMPLETED.value for t in jr_tasks):
            return False

        # The Sr task must itself have reached VERIFYING (i.e. finished its work).
        if sr.status != TaskStatus.VERIFYING.value:
            return False

        await self._store.update_workflow_status(
            workflow_id,
            WorkflowStatus.COMPLETED.value,
            result_summary="all child tasks completed and Sr verification passed",
        )
        await self._store.record_event(
            event_type="workflow_completed",
            workflow_id=workflow_id,
            message="verified completion",
            payload={"jr_task_count": len(jr_tasks)},
        )
        await self._store.enqueue_notification(
            kind="workflow_completed",
            workflow_id=workflow_id,
            payload={"jr_task_count": len(jr_tasks)},
        )
        logger.info(
            "workflow verified and completed",
            **safe_extra(workflow_id=workflow_id, jr_task_count=len(jr_tasks)),
        )
        return True

    # ------------------------------------------------------------------
    # Jr bridge (explicit mechanism for Sr -> Jr)
    # ------------------------------------------------------------------

    async def create_junior_task(
        self,
        *,
        workflow_id: str,
        parent_task_id: str,
        title: str,
        prompt: str,
        idempotency_key: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
    ) -> tuple[Task, bool]:
        """Create a Jr child task under a Sr task.

        This is the dispatcher-side bridge. It is invoked either by the
        protected HTTP endpoint (``POST /jr/tasks``) that a Sr task calls, or
        directly in-process. Requires the bridge token to be configured.
        """
        if not self._settings.jr_bridge_token:
            raise JrBridgeUnavailableError(
                "Jr bridge is not configured (JR_BRIDGE_TOKEN missing)"
            )
        parent = await self._store.get_task(parent_task_id)
        if parent is None:
            raise JrBridgeUnavailableError(f"parent task {parent_task_id} not found")
        if parent.role != TaskRole.SR.value:
            raise JrBridgeUnavailableError("only SR tasks may create JR children")
        if parent.workflow_id != workflow_id:
            raise JrBridgeUnavailableError("parent task does not belong to workflow")

        task, created = await self._store.create_task(
            workflow_id=workflow_id,
            role=TaskRole.JR.value,
            title=title,
            prompt=prompt,
            idempotency_key=idempotency_key,
            parent_task_id=parent_task_id,
            selected_repository=selected_repository or parent.selected_repository,
            selected_branch=selected_branch or parent.selected_branch,
            max_attempts=self._settings.dispatcher_max_attempts,
        )
        if created:
            await self._store.record_event(
                event_type="jr_task_created",
                workflow_id=workflow_id,
                task_id=task.id,
                message="Jr child task created",
                payload={"parent_task_id": parent_task_id},
            )
            logger.info(
                "jr task created",
                **safe_extra(workflow_id=workflow_id, task_id=task.id),
            )
        return task, created

    async def mark_jr_completed(self, task_id: str, *, result_summary: str | None = None) -> None:
        await self._store.update_task(
            task_id,
            status=TaskStatus.COMPLETED.value,
            result_summary=result_summary,
            finished_at=self._now(),
            release_lease=True,
        )
        await self._store.record_event(
            event_type="jr_task_completed", task_id=task_id, message=result_summary
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _defer(self, task: Task, exc: Exception) -> None:
        delay = self._settings.dispatcher_poll_interval_seconds
        if isinstance(exc, OpenHandsRateLimitError) and exc.retry_after:
            delay = exc.retry_after
        await self._store.schedule_retry(
            task.id,
            error=f"{type(exc).__name__}: transient",
            next_attempt_at=self._now() + timedelta(seconds=delay),
            release_lease=True,
        )
        logger.info(
            "task deferred",
            **safe_extra(task_id=task.id, error=type(exc).__name__, delay_seconds=delay),
        )

    async def _fail(self, task: Task, message: str, *, status: str = TaskStatus.FAILED.value) -> None:
        await self._store.update_task(
            task.id,
            status=status,
            last_error=message,
            finished_at=self._now(),
            release_lease=True,
        )
        await self._store.record_event(
            event_type="task_failed", workflow_id=task.workflow_id, task_id=task.id, message=message
        )
        await self._store.enqueue_notification(
            kind="task_failed", workflow_id=task.workflow_id, task_id=task.id, payload={"error": message}
        )
        logger.warning("task failed", **safe_extra(task_id=task.id, status=status))


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
