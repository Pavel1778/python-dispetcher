"""Orchestrator tests: caps, allowlist, idempotency, scope enforcement."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.orchestrator import (
    CapExceeded,
    Orchestrator,
    OrchestratorError,
    RepositoryNotAllowed,
    ScopeViolation,
)
from app.status import TaskRole, TaskStatus
from app.store.memory import InMemoryStateStore


def _settings(**over) -> Settings:
    base = dict(
        jr_allowed_repositories="acme/widgets",
        jr_max_children_per_workflow=3,
        jr_bridge_token="bridge-token",
        jr_scope_secret="scope-secret",
    )
    base.update(over)
    return Settings(**base)


async def _sr_task(store, *, repo="acme/widgets"):
    wf = await store.create_workflow(name="wf", idempotency_key="wf-1")
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role=TaskRole.SR.value,
        title="sr",
        prompt="do the thing",
        idempotency_key="sr-1",
        selected_repository=repo,
    )
    return wf, sr


async def test_create_jr_task_happy_path():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)

    view = await orch.create_jr_task(
        workflow_id=wf.id,
        task_id=sr.id,
        title="child",
        prompt="do child work",
        idempotency_key="wf-1/jr/1",
    )
    assert view.created is True
    assert view.status == TaskStatus.QUEUED.value

    tasks = await store.list_tasks_for_workflow(wf.id)
    child = next(t for t in tasks if t.role == TaskRole.JR.value)
    assert child.parent_task_id == sr.id
    # Repository inherited from the Sr task.
    assert child.selected_repository == "acme/widgets"


async def test_create_jr_task_is_idempotent():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)

    first = await orch.create_jr_task(
        workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
        idempotency_key="k1",
    )
    second = await orch.create_jr_task(
        workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
        idempotency_key="k1",
    )
    assert first.task_id == second.task_id
    assert second.created is False
    tasks = await store.list_tasks_for_workflow(wf.id)
    assert len([t for t in tasks if t.role == TaskRole.JR.value]) == 1


async def test_child_cap_enforced():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(jr_max_children_per_workflow=2), store=store)

    for i in range(2):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title=f"c{i}", prompt="p",
            idempotency_key=f"k{i}",
        )
    with pytest.raises(CapExceeded):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title="c3", prompt="p",
            idempotency_key="k3",
        )


async def test_repository_not_allowlisted_rejected():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store, repo="acme/widgets")
    orch = Orchestrator(settings=_settings(), store=store)

    with pytest.raises(RepositoryNotAllowed):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
            idempotency_key="k1", selected_repository="evil/other",
        )


async def test_empty_allowlist_denies_all():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(jr_allowed_repositories=""), store=store)

    with pytest.raises(RepositoryNotAllowed):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
            idempotency_key="k1",
        )


async def test_scope_violation_wrong_workflow():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)

    with pytest.raises(ScopeViolation):
        await orch.create_jr_task(
            workflow_id="some-other-workflow", task_id=sr.id, title="c",
            prompt="p", idempotency_key="k1",
        )


async def test_jr_cannot_orchestrate():
    """A Jr task must not be able to spawn children (no grandchildren)."""
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)
    jr = await orch.create_jr_task(
        workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
        idempotency_key="k1",
    )
    with pytest.raises(ScopeViolation, match="only a Sr task"):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=jr.task_id, title="g", prompt="p",
            idempotency_key="k2",
        )


async def test_invalid_inputs_rejected():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)

    with pytest.raises(OrchestratorError):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title="", prompt="p",
            idempotency_key="k1",
        )
    with pytest.raises(OrchestratorError):
        await orch.create_jr_task(
            workflow_id=wf.id, task_id=sr.id, title="c", prompt="p",
            idempotency_key="bad key with spaces",
        )


async def test_get_status_rejects_foreign_child():
    store = InMemoryStateStore()
    await store.connect()
    wf, sr = await _sr_task(store)
    orch = Orchestrator(settings=_settings(), store=store)
    jr = await orch.create_jr_task(
        workflow_id=wf.id, task_id=sr.id, title="c", prompt="p", idempotency_key="k1"
    )
    # Another workflow's Sr cannot read this child.
    wf2 = await store.create_workflow(name="wf2", idempotency_key="wf-2")
    sr2, _ = await store.create_task(
        workflow_id=wf2.id, role=TaskRole.SR.value, title="sr2", prompt="p",
        idempotency_key="sr-2",
    )
    with pytest.raises(ScopeViolation):
        await orch.get_jr_task_status(
            workflow_id=wf2.id, task_id=sr2.id, jr_task_id=jr.task_id
        )
