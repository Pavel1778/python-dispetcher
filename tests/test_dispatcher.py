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
    await store.update_task(
        jr.id,
        status=TaskStatus.COMPLETED.value,
        result_summary="child output",
        release_lease=True,
    )
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value)

    completed = await dispatcher.verify_workflows()

    assert completed == 1
    wf_after = await store.get_workflow(wf.id)
    assert wf_after.status == WorkflowStatus.COMPLETED.value


async def test_completed_child_without_result_does_not_complete_workflow(dispatcher, store):
    """A child marked COMPLETED with no recorded result is not a success."""
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

    assert completed == 0
    wf_after = await store.get_workflow(wf.id)
    assert wf_after.status != WorkflowStatus.COMPLETED.value


async def test_orchestrator_creates_child_task(settings, store):
    """The orchestrator is the single path that creates Jr children.

    Both transports (MCP and HTTP /jr/tasks) go through it, so caps and the
    repository allowlist cannot be bypassed by picking a transport.
    """
    from app.orchestrator import Orchestrator

    settings.jr_allowed_repositories = "acme/widgets"
    settings.jr_scope_secret = "scope-secret"
    orchestrator = Orchestrator(settings=settings, store=store)
    wf, task = await _make_workflow(store)

    view = await orchestrator.create_jr_task(
        workflow_id=wf.id,
        task_id=task.id,
        title="jr child",
        prompt="work",
        idempotency_key="wf-1-jr-bridge",
    )

    assert view.created is True
    assert view.status == TaskStatus.QUEUED.value
    tasks = await store.list_tasks_for_workflow(wf.id)
    child = next(t for t in tasks if t.role == "JR")
    assert child.parent_task_id == task.id


async def test_cycle_marks_success_flag(dispatcher, store, fake_client):
    await _make_workflow(store)
    fake_client.queue_start_task(_start_task("start-1", "WORKING"))

    stats = await dispatcher.run_cycle()

    assert dispatcher.last_cycle_succeeded is True
    assert stats["dispatched"] == 1
    assert dispatcher.last_cycle_at is not None


# ---------------------------------------------------------------------------
# Full lifecycle regression (D1)
#
# These tests drive the *dispatcher* end to end. They never call
# ``store.update_task(..., COMPLETED)`` themselves: the only way a task reaches
# COMPLETED is through the dispatcher's own state machine, which is the whole
# point of the regression.
# ---------------------------------------------------------------------------


async def _make_full_workflow(store, key="wf-full"):
    """A workflow with one Sr and one Jr child, both freshly created."""
    wf = await store.create_workflow(name="full", idempotency_key=key)
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role="SR",
        title="sr",
        prompt="plan",
        idempotency_key=f"{key}-sr",
        selected_repository="acme/widgets",
    )
    await store.update_workflow_status(wf.id, WorkflowStatus.RUNNING.value, sr_task_id=sr.id)
    jr, _ = await store.create_task(
        workflow_id=wf.id,
        role="JR",
        title="jr",
        prompt="do the work",
        idempotency_key=f"{key}-jr",
        parent_task_id=sr.id,
        selected_repository="acme/widgets",
    )
    return wf, sr, jr


async def _drive_to_completion(dispatcher, fake_client, *, max_cycles=20):
    """Run full cycles until nothing changes. Returns the cycle count."""
    for i in range(max_cycles):
        await dispatcher.run_cycle()
        if not await _has_pending_work(dispatcher):
            return i + 1
    return max_cycles


async def _has_pending_work(dispatcher) -> bool:
    tasks = await dispatcher._store.list_tasks_by_status(
        [TaskStatus.QUEUED.value, TaskStatus.STARTING.value, TaskStatus.RUNNING.value]
    )
    return bool(tasks)


async def test_full_lifecycle_reaches_completed_without_manual_status(
    dispatcher, store, fake_client
):
    """Sr and Jr both finish -> workflow COMPLETED, driven only by the dispatcher."""
    wf, sr, jr = await _make_full_workflow(store)

    # Two start tasks, one per conversation, then both become READY.
    fake_client.queue_start_task(_start_task("start-sr", "WORKING"))
    fake_client.queue_start_task(_start_task("start-jr", "WORKING"))
    await dispatcher.dispatch()

    fake_client.start_task_lookup["start-sr"] = _start_task("start-sr", "READY", conv="conv-sr")
    fake_client.start_task_lookup["start-jr"] = _start_task("start-jr", "READY", conv="conv-jr")
    await dispatcher.advance()

    # Both conversations finish, each with a real result.
    fake_client.queue_conversation(_conv("conv-sr", "finished"))
    fake_client.queue_conversation(_conv("conv-jr", "finished"))
    fake_client.queue_result("conv-sr", "Sr summary: delegated and verified.")
    fake_client.queue_result("conv-jr", "Jr summary: implemented the change.")

    await _drive_to_completion(dispatcher, fake_client)

    jr_after = await store.get_task(jr.id)
    sr_after = await store.get_task(sr.id)
    wf_after = await store.get_workflow(wf.id)

    # The Jr reached COMPLETED through the dispatcher, not by a manual update.
    assert jr_after.status == TaskStatus.COMPLETED.value
    assert jr_after.result_summary == "Jr summary: implemented the change."
    assert sr_after.status == TaskStatus.COMPLETED.value
    assert wf_after.status == WorkflowStatus.COMPLETED.value


async def test_full_lifecycle_blocks_child_without_result(dispatcher, store, fake_client):
    """A Jr that finishes with no result is BLOCKED and never completes the wf."""
    wf, sr, jr = await _make_full_workflow(store, key="wf-noresult")

    fake_client.queue_start_task(_start_task("start-sr", "WORKING"))
    fake_client.queue_start_task(_start_task("start-jr", "WORKING"))
    await dispatcher.dispatch()
    fake_client.start_task_lookup["start-sr"] = _start_task("start-sr", "READY", conv="conv-sr")
    fake_client.start_task_lookup["start-jr"] = _start_task("start-jr", "READY", conv="conv-jr")
    await dispatcher.advance()

    fake_client.queue_conversation(_conv("conv-sr", "finished"))
    fake_client.queue_conversation(_conv("conv-jr", "finished"))
    fake_client.queue_result("conv-sr", "Sr done")
    # conv-jr produced nothing at all.

    await _drive_to_completion(dispatcher, fake_client)

    jr_after = await store.get_task(jr.id)
    wf_after = await store.get_workflow(wf.id)
    assert jr_after.status == TaskStatus.BLOCKED.value
    assert wf_after.status != WorkflowStatus.COMPLETED.value


async def test_child_result_lookup_error_defers_not_blocks(dispatcher, store, fake_client):
    """A transient error reading the child result must retry, not fail the child."""
    wf, sr, jr = await _make_full_workflow(store, key="wf-lookup-error")
    await store.update_task(
        jr.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-jr", release_lease=True
    )
    fake_client.queue_conversation(_conv("conv-jr", "finished"))
    fake_client.result_fail_with = OpenHandsTransientError("network")

    await dispatcher.advance()

    jr_after = await store.get_task(jr.id)
    assert jr_after.status != TaskStatus.COMPLETED.value
    assert jr_after.status != TaskStatus.BLOCKED.value
    assert jr_after.attempts == 1


async def test_sr_cannot_complete_workflow_while_child_running(dispatcher, store, fake_client):
    """Sr finishing is not enough: a still-running child withholds COMPLETED."""
    wf, sr, jr = await _make_full_workflow(store, key="wf-sr-early")
    await store.update_task(
        sr.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-sr", release_lease=True
    )
    await store.update_task(
        jr.id, status=TaskStatus.RUNNING.value, app_conversation_id="conv-jr", release_lease=True
    )
    fake_client.queue_conversation(_conv("conv-sr", "finished"))
    fake_client.queue_result("conv-sr", "Sr claims done")
    # Jr is still running.
    fake_client.queue_conversation(_conv("conv-jr", "running"))

    await dispatcher.advance()
    await dispatcher.verify_workflows()

    sr_after = await store.get_task(sr.id)
    wf_after = await store.get_workflow(wf.id)
    assert sr_after.status == TaskStatus.VERIFYING.value
    assert wf_after.status != WorkflowStatus.COMPLETED.value


async def test_upstream_error_detail_is_scrubbed_before_persist(settings, store, fake_client):
    """D8: a secret echoed in an upstream ``detail`` must not be persisted."""
    from app.dispatcher.core import Dispatcher
    from app.openhands_client import OpenHandsRequestError

    settings.openhands_cloud_api_key = "sk-live-secret-value-abcdef"
    dispatcher = Dispatcher(settings=settings, store=store, client=fake_client)
    wf, task = await _make_workflow(store, key="wf-leak")
    # The upstream error text echoes the API key back at us.
    fake_client.fail_with = OpenHandsRequestError(
        "bad request: key sk-live-secret-value-abcdef rejected", status_code=400
    )

    await dispatcher.dispatch()

    updated = await store.get_task(task.id)
    assert updated.status == TaskStatus.FAILED.value
    assert "sk-live-secret-value-abcdef" not in (updated.last_error or "")
    assert "<redacted>" in (updated.last_error or "")

