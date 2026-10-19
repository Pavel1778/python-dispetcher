"""Resilience fixes: start-task timeout (D4), retry exhaustion (D5),
and notification-queue draining (D7).

Each test is written to fail against the pre-fix code so the behaviour is
genuinely covered rather than merely exercised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.dispatcher.core import Dispatcher
from app.status import TaskStatus, WorkflowStatus


def _start_task(sid: str, status: str = "WORKING", conv: str | None = None):
    from app.openhands_client import StartTask

    return StartTask(
        id=sid,
        status=status,
        app_conversation_id=conv,
        sandbox_id="sandbox-1" if conv else None,
        detail=None,
        created_at=None,
        updated_at=None,
    )


async def _sr_task(store, key="wf-1", max_attempts=5):
    wf = await store.create_workflow(name="wf", idempotency_key=key)
    task, _ = await store.create_task(
        workflow_id=wf.id,
        role="SR",
        title="sr",
        prompt="plan",
        idempotency_key=f"{key}-sr",
        max_attempts=max_attempts,
    )
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value, sr_task_id=task.id)
    return wf, task


async def _burn_attempts(store, task, count: int) -> None:
    """Spend retry attempts the same way production does (via schedule_retry)."""
    for _ in range(count):
        await store.schedule_retry(
            task.id,
            error="transient",
            next_attempt_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# D4: a start task that never becomes READY must time out
# ---------------------------------------------------------------------------


async def test_start_task_timeout_fails_stuck_task(settings, store, fake_client):
    settings.dispatcher_start_task_timeout_seconds = 60
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    _, task = await _sr_task(store)

    # Start the conversation, then make the start task stay WORKING forever.
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))
    await dispatcher.dispatch()

    running = await store.get_task(task.id)
    assert running.status == TaskStatus.STARTING.value

    # Pretend the start request began well past the timeout window.
    await store.update_task(
        task.id,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    await dispatcher.advance()

    after = await store.get_task(task.id)
    assert after.status == TaskStatus.FAILED.value
    assert "timed out" in (after.last_error or "")


async def test_start_task_within_timeout_keeps_waiting(settings, store, fake_client):
    settings.dispatcher_start_task_timeout_seconds = 3600
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    wf, task = await _sr_task(store, key="wf-2")
    fake_client.queue_start_task(_start_task("start-2", "WORKING"))
    await dispatcher.dispatch()
    await dispatcher.advance()

    assert task.workflow_id == wf.id
    after = await store.get_task(task.id)
    assert after.status == TaskStatus.STARTING.value
    assert after.last_error is None


# ---------------------------------------------------------------------------
# D5: a task that exhausts its retry budget must reach a terminal state
# ---------------------------------------------------------------------------


async def test_exhausted_task_is_failed_not_stalled(settings, store, fake_client):
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    _, task = await _sr_task(store, key="wf-3", max_attempts=2)
    await _burn_attempts(store, task, 2)

    expired = await dispatcher.expire_exhausted_tasks()
    assert expired == 1

    after = await store.get_task(task.id)
    assert after.status == TaskStatus.FAILED.value
    assert "retry budget exhausted" in (after.last_error or "")


async def test_task_with_budget_left_is_not_expired(settings, store, fake_client):
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    _, task = await _sr_task(store, key="wf-4", max_attempts=5)
    await _burn_attempts(store, task, 1)

    expired = await dispatcher.expire_exhausted_tasks()
    assert expired == 0
    after = await store.get_task(task.id)
    assert after.status != TaskStatus.FAILED.value


async def test_verifying_task_with_high_attempts_is_not_expired(settings, store, fake_client):
    """A Sr legitimately waiting in VERIFYING must not be killed by the sweep.

    Its children may still be running, so a high attempt count is not evidence
    that the Sr itself is stuck.
    """
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    _, task = await _sr_task(store, key="wf-4b", max_attempts=1)
    await _burn_attempts(store, task, 1)
    await store.update_task(task.id, status=TaskStatus.VERIFYING.value)

    expired = await dispatcher.expire_exhausted_tasks()
    assert expired == 0
    after = await store.get_task(task.id)
    assert after.status == TaskStatus.VERIFYING.value


# ---------------------------------------------------------------------------
# D7: queued notifications must be drained
# ---------------------------------------------------------------------------


async def test_pending_notifications_are_drained(settings, store, fake_client):
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    wf, task = await _sr_task(store, key="wf-5")
    await store.enqueue_notification(kind="task_failed", workflow_id=wf.id, task_id=task.id)

    assert len(await store.list_pending_notifications()) == 1

    drained = await dispatcher.drain_notifications()
    assert drained == 1
    # Nothing left pending: the queue is bounded.
    assert await store.list_pending_notifications() == []


async def test_run_cycle_reports_expired_and_notification_counts(settings, store, fake_client):
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    wf, task = await _sr_task(store, key="wf-6", max_attempts=1)
    await _burn_attempts(store, task, 1)
    await store.enqueue_notification(kind="task_failed", workflow_id=wf.id, task_id=task.id)

    stats = await dispatcher.run_cycle()
    assert stats["expired"] == 1
    # One notification from the pre-seeded row, one enqueued by the expiry sweep.
    assert stats["notifications"] == 2
    assert await store.list_pending_notifications() == []
