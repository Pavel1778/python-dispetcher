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

from ..bridge_config import (
    build_plugin_spec_if_buildable,
    build_scope_instructions,
    mcp_tools_attached,
)
from ..config import Settings
from ..logging_config import get_logger, safe_extra, scrub_text
from ..scope import mint_scope_token
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
        stats = {
            "dispatched": 0,
            "advanced": 0,
            "reconciled": 0,
            "verified": 0,
            "expired": 0,
            "notifications": 0,
        }
        try:
            stats["expired"] = await self.expire_exhausted_tasks()
            stats["dispatched"] = await self.dispatch()
            stats["advanced"] = await self.advance()
            stats["reconciled"] = await self.reconcile()
            stats["verified"] = await self.verify_workflows()
            stats["notifications"] = await self.drain_notifications()
        except StoreUnavailableError:
            self.last_cycle_succeeded = False
            logger.warning("dispatch cycle aborted: store unavailable")
            return stats
        self.last_cycle_succeeded = True
        self.last_cycle_at = self._now()
        return stats

    async def expire_exhausted_tasks(self) -> int:
        """Fail queued tasks that have used up their retry budget.

        ``claim_due_tasks`` never hands out a task once ``attempts >=
        max_attempts``. Without this sweep such a task is neither retried nor
        failed: it stays non-terminal forever and its workflow can never finish.

        Only QUEUED tasks are swept. A task in STARTING/RUNNING/VERIFYING is
        still polled by ``advance``, so a high attempt count there does not mean
        it is stuck - failing it would kill a task that is making progress (for
        example a Sr legitimately waiting in VERIFYING for its children).
        """
        try:
            tasks = await self._store.list_tasks_by_status([TaskStatus.QUEUED.value])
        except StoreUnavailableError:
            logger.warning("expire skipped: store unavailable")
            return 0
        expired = 0
        for task in tasks:
            if task.attempts < task.max_attempts:
                continue
            await self._fail(
                task,
                f"retry budget exhausted after {task.attempts} attempts",
            )
            expired += 1
        if expired:
            logger.info("expired exhausted tasks", **safe_extra(count=expired))
        return expired

    async def drain_notifications(self) -> int:
        """Consume queued notifications so the queue cannot grow unbounded.

        No external delivery target is configured yet (Better Stack is
        deferred), so each notification is emitted as a structured log line and
        marked sent. This keeps the durable queue bounded on the free tiers
        while preserving the audit trail in logs.
        """
        try:
            pending = await self._store.list_pending_notifications(limit=50)
        except StoreUnavailableError:
            logger.warning("notification drain skipped: store unavailable")
            return 0
        drained = 0
        for note in pending:
            logger.info(
                "notification",
                **safe_extra(
                    notification_id=note.id,
                    kind=note.kind,
                    workflow_id=note.workflow_id,
                    task_id=note.task_id,
                ),
            )
            if note.id is not None:
                await self._store.mark_notification_sent(note.id)
            drained += 1
        return drained

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
                extra_body=self._bridge_start_overrides(task),
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

    def _bridge_start_overrides(self, task: Task) -> dict[str, Any]:
        """Extra fields that give an SR conversation access to the bridge tools.

        Only SR tasks get the bridge: a Jr task must not spawn grandchildren.
        The scope token is minted here and embedded in the system prompt; the
        MCP server config is attached only when an operator has configured a
        public base URL (otherwise the tools would point at an unreachable
        endpoint, which is worse than not offering them).
        """
        if task.role != TaskRole.SR.value:
            return {}
        if not self._settings.jr_scope_secret:
            logger.warning(
                "SR task started without bridge scope token (JR_SCOPE_SECRET unset)",
                **safe_extra(task_id=task.id),
            )
            return {}

        overrides: dict[str, Any] = {}
        token = mint_scope_token(
            self._settings.jr_scope_secret,
            workflow_id=task.workflow_id,
            task_id=task.id,
            ttl_seconds=self._settings.jr_scope_ttl_seconds,
        )

        # Only advertise the tools if they can actually be attached. Telling Sr
        # about tools it does not have makes it call a tool that does not exist.
        attached = mcp_tools_attached(self._settings)
        overrides["system_message_suffix"] = build_scope_instructions(
            workflow_id=task.workflow_id,
            task_id=task.id,
            scope_token=token,
            tools_attached=attached,
        )
        if not attached:
            logger.warning(
                "SR task started without MCP tools attached; bridge unavailable",
                **safe_extra(task_id=task.id),
            )

        # Attach the plugin only when its .mcp.json can actually be built, so we
        # never hand the sandbox a plugin pointing at an unreachable endpoint.
        try:
            plugin_spec = build_plugin_spec_if_buildable(self._settings)
        except ValueError:
            plugin_spec = None
            logger.warning(
                "MCP plugin configured but MCP_PUBLIC_BASE_URL unset; not attached",
                **safe_extra(task_id=task.id),
            )
        if plugin_spec is not None:
            overrides["plugins"] = [plugin_spec]
        return overrides

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
        # Not READY yet. A start task that never becomes READY must not hold the
        # task forever, so enforce the configured ceiling here.
        if self._start_task_timed_out(task):
            await self._fail(
                task,
                "start task timed out before becoming ready",
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

    def _start_task_timed_out(self, task: Task) -> bool:
        started = task.started_at or task.created_at
        if started is None:
            return False
        elapsed = (self._now() - started).total_seconds()
        return elapsed >= self._settings.dispatcher_start_task_timeout_seconds

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
            await self._complete_finished_conversation(task, conv)
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

    async def _complete_finished_conversation(self, task: Task, conv: Conversation) -> None:
        """Move a task whose conversation finished to its terminal/next state.

        A Jr child that finished with an actual result is COMPLETED - that is
        the transition the workflow-level verification waits for. A Jr that
        finished *without* producing a result is not a success: it is marked
        BLOCKED so it can never satisfy verification.

        The Sr is different: a finished Sr conversation only means its work is
        done, so it moves to VERIFYING and the dispatcher independently checks
        the children before the workflow can complete.
        """
        if task.role == TaskRole.JR.value:
            try:
                result = await self._child_result(conv.id)
            except (OpenHandsRateLimitError, OpenHandsTransientError) as exc:
                # Cannot read the child's result yet. Defer rather than decide:
                # treating an API failure as "no result" would permanently block
                # a child that actually succeeded.
                await self._defer(task, exc)
                return
            if not result:
                await self._fail(
                    task,
                    "child conversation finished without producing a result",
                    status=TaskStatus.BLOCKED.value,
                )
                return
            await self._store.update_task(
                task.id,
                status=TaskStatus.COMPLETED.value,
                sandbox_id=conv.sandbox_id,
                result_summary=result,
                finished_at=self._now(),
                release_lease=True,
            )
            await self._store.record_event(
                event_type="child_completed",
                workflow_id=task.workflow_id,
                task_id=task.id,
                message="child conversation finished with a result",
            )
            logger.info("child task completed", **safe_extra(task_id=task.id))
            return

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

    async def _child_result(self, conversation_id: str) -> str | None:
        """Read the child conversation's final message, or ``None`` if absent.

        Transport/rate-limit errors propagate to the caller so the task is
        retried; only a genuinely empty result returns ``None``.
        """
        text = await self._client.get_conversation_result(conversation_id)
        return (text or "").strip() or None

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

        # Every Jr child must be COMPLETED with a recorded result; a failed,
        # blocked, running, or result-less child means the workflow is not done.
        if any(
            t.status != TaskStatus.COMPLETED.value or not t.result_summary
            for t in jr_tasks
        ):
            return False

        # The Sr task must itself have reached VERIFYING (i.e. finished its work).
        if sr.status != TaskStatus.VERIFYING.value:
            return False

        await self._store.update_workflow_status(
            workflow_id,
            WorkflowStatus.COMPLETED.value,
            result_summary="all child tasks completed and Sr verification passed",
        )
        # Close the Sr task too: leaving it in VERIFYING (a non-terminal state)
        # would keep it in every advance() poll for a finished workflow.
        await self._store.update_task(
            sr.id,
            status=TaskStatus.COMPLETED.value,
            finished_at=self._now(),
            release_lease=True,
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
        # ``message`` can embed upstream text (e.g. a start-task ``detail``). Scrub
        # it before it is persisted, served to the Sr via get_jr_task_status, and
        # copied into notifications, so a secret echoed by the API cannot leak.
        message = scrub_text(message, self._settings.redaction_secrets())
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
