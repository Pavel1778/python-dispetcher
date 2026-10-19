"""State-store contract tests.

Runs against the in-memory backend always. The same suite runs against a real
PostgreSQL when ``TEST_DATABASE_URL`` is set, which is how the Supabase schema
and lease semantics get real coverage.
"""

from __future__ import annotations

import os

import pytest

from app.status import TaskStatus
from app.store.memory import InMemoryStateStore

pg_url = os.environ.get("TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(not pg_url, reason="TEST_DATABASE_URL not set")


async def _store_under_test(pg_url: str | None):
    if pg_url:
        from app.store.postgres import PostgresStateStore

        store = PostgresStateStore(pg_url)
        await store.connect()
        await store.apply_migrations()
        return store
    store = InMemoryStateStore()
    await store.connect()
    return store


async def _cleanup(store):
    if isinstance(store, InMemoryStateStore):
        await store.close()
        return
    pool = store._require_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "TRUNCATE notifications, task_events, tasks, workflows RESTART IDENTITY CASCADE"
        )
    await store.close()


@pytest.fixture(params=["memory", "postgres"])
async def any_store(request):
    if request.param == "postgres" and not pg_url:
        pytest.skip("TEST_DATABASE_URL not set")
    store = await _store_under_test(pg_url)
    yield store
    await _cleanup(store)


async def test_workflow_idempotency(any_store):
    a = await any_store.create_workflow(name="w", idempotency_key="k1")
    b = await any_store.create_workflow(name="w2", idempotency_key="k1")
    assert a.id == b.id


async def test_task_idempotency(any_store):
    wf = await any_store.create_workflow(name="w", idempotency_key="wk")
    t1, c1 = await any_store.create_task(
        workflow_id=wf.id, role="SR", title="t", prompt="p", idempotency_key="tk"
    )
    t2, c2 = await any_store.create_task(
        workflow_id=wf.id, role="SR", title="t", prompt="p", idempotency_key="tk"
    )
    assert c1 is True and c2 is False
    assert t1.id == t2.id


async def test_claim_is_exclusive(any_store):
    wf = await any_store.create_workflow(name="w", idempotency_key="ck")
    await any_store.create_task(
        workflow_id=wf.id, role="SR", title="t", prompt="p", idempotency_key="ctk"
    )
    first = await any_store.claim_due_tasks(worker_id="w1", lease_seconds=60, limit=10)
    second = await any_store.claim_due_tasks(worker_id="w2", lease_seconds=60, limit=10)
    assert len(first) == 1
    assert len(second) == 0


async def test_schedule_retry_increments_attempts(any_store):
    from datetime import datetime, timezone

    wf = await any_store.create_workflow(name="w", idempotency_key="rk")
    task, _ = await any_store.create_task(
        workflow_id=wf.id, role="SR", title="t", prompt="p", idempotency_key="rtk"
    )
    await any_store.schedule_retry(
        task.id, error="boom", next_attempt_at=datetime.now(timezone.utc)
    )
    updated = await any_store.get_task(task.id)
    assert updated.attempts == 1
    assert updated.last_error == "boom"


async def test_events_and_notifications(any_store):
    wf = await any_store.create_workflow(name="w", idempotency_key="ek")
    await any_store.record_event(event_type="x", workflow_id=wf.id, message="hi")
    events = await any_store.list_events(workflow_id=wf.id)
    assert any(e.event_type == "x" for e in events)

    nid = await any_store.enqueue_notification(kind="test", workflow_id=wf.id)
    pending = await any_store.list_pending_notifications()
    assert any(n.id == nid for n in pending)
    await any_store.mark_notification_sent(nid)
    pending_after = await any_store.list_pending_notifications()
    assert all(n.id != nid for n in pending_after)


async def test_healthcheck_true_when_connected(any_store):
    assert await any_store.healthcheck() is True
