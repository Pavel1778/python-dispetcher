"""/health and /ready endpoint behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.store.memory import InMemoryStateStore


@pytest.fixture
def client(settings, fake_client):
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = fake_client
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    # Fresh app per test so the mounted MCP session manager is not reused.
    with TestClient(app_main.create_app()) as c:
        yield c
    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_ready_reports_dependencies(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] is True
    assert body["api_key_configured"] is True
    assert body["ready"] is True
    # No secrets leaked.
    assert "sk-" not in resp.text
    assert "test-bridge-token" not in resp.text


def test_ready_not_ready_without_api_key(settings, fake_client):
    settings.openhands_cloud_api_key = None
    settings.openhands_api_key = None
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = fake_client
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    with TestClient(app_main.create_app()) as c:
        resp = c.get("/ready")
    app_main.runtime.store = None
    app_main.runtime.client = None
    body = resp.json()
    assert body["api_key_configured"] is False
    assert body["ready"] is False


def test_ready_reports_database_failure(settings, fake_client):
    broken = InMemoryStateStore(fail_healthcheck=True)
    app_main.runtime.settings = settings
    app_main.runtime.store = broken
    app_main.runtime.client = fake_client
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    with TestClient(app_main.create_app()) as c:
        resp = c.get("/ready")
    app_main.runtime.store = None
    app_main.runtime.client = None
    body = resp.json()
    assert body["database"] is False
    assert body["ready"] is False


def test_jr_bridge_requires_token(client):
    resp = client.post(
        "/jr/tasks",
        json={
            "workflow_id": "w",
            "parent_task_id": "p",
            "title": "t",
            "prompt": "p",
            "idempotency_key": "k",
        },
    )
    assert resp.status_code == 401


def test_jr_bridge_rejects_bad_token(client):
    resp = client.post(
        "/jr/tasks",
        headers={"Authorization": "Bearer wrong"},
        json={
            "workflow_id": "w",
            "parent_task_id": "p",
            "title": "t",
            "prompt": "p",
            "idempotency_key": "k",
        },
    )
    assert resp.status_code == 401


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-bridge-token"}


async def _seed_sr(store):
    from app.status import TaskRole

    wf = await store.create_workflow(name="wf", idempotency_key="wf-api")
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role=TaskRole.SR.value,
        title="sr",
        prompt="p",
        idempotency_key="sr-api",
        selected_repository="acme/widgets",
    )
    return wf, sr


async def test_jr_bridge_creates_child_through_orchestrator(settings, fake_client):
    """The HTTP transport enforces the same rules as the MCP transport."""
    store = InMemoryStateStore()
    await store.connect()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = fake_client
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    wf, sr = await _seed_sr(store)

    with TestClient(app_main.create_app()) as c:
        ok = c.post(
            "/jr/tasks",
            headers=_auth(),
            json={
                "workflow_id": wf.id,
                "parent_task_id": sr.id,
                "title": "child",
                "prompt": "do it",
                "idempotency_key": "wf-api/jr/1",
            },
        )
        assert ok.status_code == 200
        assert ok.json()["created"] is True

        # A repo outside the allowlist is rejected on this transport too.
        denied = c.post(
            "/jr/tasks",
            headers=_auth(),
            json={
                "workflow_id": wf.id,
                "parent_task_id": sr.id,
                "title": "evil",
                "prompt": "x",
                "idempotency_key": "wf-api/jr/2",
                "selected_repository": "evil/repo",
            },
        )
        assert denied.status_code == 409

    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.orchestrator = None

    from app.status import TaskRole

    tasks = await store.list_tasks_for_workflow(wf.id)
    children = [t for t in tasks if t.role == TaskRole.JR.value]
    assert len(children) == 1
    assert children[0].title == "child"
