"""Dispatcher lifecycle: async creation, idempotency, recovery, verification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.openhands_client import (
    Conversation,
    OpenHandsRateLimitError,
    OpenHandsTransientError,
    StartTask,
)
from app.status import TaskStatus, WorkflowStatus


def _start_task(sid: str, status: str = "WORKING", conv: str | None = None) -> StartTask:
    return StartTask(
        id=sid,
        status=status,
        app_conversation_id=conv,
        sandbox_id="sandbox-1" if conv else None,
        detail=None,
        created_at=None,
        updated_at=None,
    )


def _conv(cid: str, execution: str) -> Conversation:
    return Conversation(
        id=cid,
        sandbox_id="sandbox-1",
        title=None,
        sandbox_status="RUNNING",
        execution_status=execution,
        parent_conversation_id=None,
        sub_conversation_ids=[],
    )


async def _make_workflow(store, key="wf-1"):
    wf = await store.create_workflow(name="test wf", idempotency_key=key)
    task, _ = await store.create_task(
        workflow_id=wf.id,
        role="SR",
        title="sr task",
        prompt="plan",
        idempotency_key=f"{key}-sr",
    )
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value, sr_task_id=task.id)
    return wf, task


async def test_dispatch_creates_conversation_and_stores_start_task_id(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))

    dispatched = await dispatcher.dispatch()

    assert dispatched == 1
    assert len(fake_client.start_calls) == 1
    updated = await store.get_task(task.id)
    assert updated.start_task_id == "start-1"
    assert updated.status == TaskStatus.STARTING.value
    # Conversation id not yet available (async creation).
    assert updated.app_conversation_id is None


async def test_start_task_ready_records_both_ids(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))
    await dispatcher.dispatch()

    fake_client.start_task_lookup["start-1"] = _start_task("start-1", "READY", conv="conv-9")
    await dispatcher.advance()

    updated = await store.get_task(task.id)
    assert updated.start_task_id == "start-1"
    assert updated.app_conversation_id == "conv-9"
    assert updated.status == TaskStatus.RUNNING.value


async def test_repeated_dispatch_does_not_create_duplicate_conversation(
    dispatcher, store, fake_client
):
    wf, task = await _make_workflow(store)
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))
    await dispatcher.dispatch()
    # Simulate lease expiry so the task becomes claimable again.
    await store.update_task(task.id, release_lease=True, next_attempt_at=None)

    await dispatcher.dispatch()

    # Only one conversation start call despite two dispatch passes.
    assert len(fake_client.start_calls) == 1


async def test_idempotent_task_creation(store):
    wf, task = await _make_workflow(store)
    same, created = await store.create_task(
        workflow_id=wf.id,
        role="SR",
        title="sr task",
        prompt="plan",
        idempotency_key="wf-1-sr",
    )
    assert created is False
    assert same.id == task.id


async def test_recover_requeues_unstarted_tasks(dispatcher, store):
    wf, task = await _make_workflow(store)
    # Simulate a task left mid-flight (non-terminal, no remote ids).
    await store.update_task(task.id, status=TaskStatus.STARTING.value, release_lease=False)

    recovered = await dispatcher.recover()

    assert recovered == 1
    updated = await store.get_task(task.id)
    assert updated.status == TaskStatus.QUEUED.value


async def test_recover_skips_tasks_already_linked_to_conversation(dispatcher, store):
    wf, task = await _make_workflow(store)
    await store.update_task(
        task.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-x", release_lease=True
    )

    recovered = await dispatcher.recover()

    assert recovered == 0
    updated = await store.get_task(task.id)
    # Untouched: reconcile handles it, not recover.
    assert updated.status == TaskStatus.RUNNING.value


async def test_reconcile_advances_linked_conversation(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    await store.update_task(
        task.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-1", release_lease=True
    )
    fake_client.queue_conversation(_conv("conv-1", "finished"))

    await dispatcher.reconcile()

    updated = await store.get_task(task.id)
    assert updated.status == TaskStatus.VERIFYING.value


async def test_transient_error_schedules_retry(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    fake_client.fail_with = OpenHandsTransientError("network")

    await dispatcher.dispatch()

    updated = await store.get_task(task.id)
    assert updated.attempts == 1
    assert updated.status == TaskStatus.QUEUED.value
    assert updated.next_attempt_at is not None
    assert "transient" in (updated.last_error or "")


async def test_rate_limit_defers_with_retry_after(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    fake_client.fail_with = OpenHandsRateLimitError("429", retry_after=42)

    await dispatcher.dispatch()

    updated = await store.get_task(task.id)
    assert updated.attempts == 1
    delta = (updated.next_attempt_at - datetime.now(timezone.utc)).total_seconds()
    assert 35 < delta <= 45


async def test_conversation_error_marks_task_failed(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    await store.update_task(
        task.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-1", release_lease=True
    )
    fake_client.queue_conversation(_conv("conv-1", "error"))

    await dispatcher.advance()

    updated = await store.get_task(task.id)
    assert updated.status == TaskStatus.FAILED.value


async def test_workflow_not_completed_while_children_running(dispatcher, store, fake_client):
    wf, task = await _make_workflow(store)
    # Sr finished its work and is verifying.
    await store.update_task(task.id, status=TaskStatus.VERIFYING.value, release_lease=True)
    # One Jr still running.
    await store.create_task(
        workflow_id=wf.id,
        role="JR",
        title="jr",
        prompt="do",
        idempotency_key="wf-1-jr",
        parent_task_id=task.id,
    )
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value)

    completed = await dispatcher.verify_workflows()

    assert completed == 0
    wf_after = await store.get_workflow(wf.id)
    assert wf_after.status != WorkflowStatus.COMPLETED.value


async def test_workflow_completed_only_when_all_children_completed(dispatcher, store):
    wf, task = await _make_workflow(store)
    await store.update_task(task.id, status=TaskStatus.VERIFYING.value, release_lease=True)
    jr, _ = await store.create_task(
        workflow_id=wf.id,
        role="JR",
        title="jr",
        prompt="do",
        idempotency_key="wf-1-jr",
        parent_task_id=task.id,
    )
    await store.update_task(jr.id, status=TaskStatus.COMPLETED.value, release_lease=True)
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value)

    completed = await dispatcher.verify_workflows()

    assert completed == 1
    wf_after = await store.get_workflow(wf.id)
    assert wf_after.status == WorkflowStatus.COMPLETED.value


async def test_jr_bridge_creates_child_task(dispatcher, store):
    wf, task = await _make_workflow(store)

    jr, created = await dispatcher.create_junior_task(
        workflow_id=wf.id,
        parent_task_id=task.id,
        title="jr child",
        prompt="work",
        idempotency_key="wf-1-jr-bridge",
    )

    assert created is True
    assert jr.role == "JR"
    assert jr.parent_task_id == task.id


async def test_jr_bridge_requires_configuration(settings, store, fake_client):
    from app.dispatcher.core import JrBridgeUnavailableError, Dispatcher

    settings.jr_bridge_token = None
    d = Dispatcher(settings=settings, store=store, client=fake_client, worker_id="w")
    wf, task = await _make_workflow(store)

    with pytest.raises(JrBridgeUnavailableError):
        await d.create_junior_task(
            workflow_id=wf.id,
            parent_task_id=task.id,
            title="x",
            prompt="y",
            idempotency_key="k",
        )


async def test_cycle_marks_success_flag(dispatcher, store, fake_client):
    await _make_workflow(store)
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))

    stats = await dispatcher.run_cycle()

    assert dispatcher.last_cycle_succeeded is True
    assert stats["dispatched"] == 1
    assert dispatcher.last_cycle_at is not None
