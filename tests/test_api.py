"""/health and /ready endpoint behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.store.memory import InMemoryStateStore


@pytest.fixture
def client(settings, fake_client, monkeypatch):
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = fake_client
    app_main.runtime.dispatcher = None
    with TestClient(app_main.app) as c:
        yield c
    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None


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
    with TestClient(app_main.app) as c:
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
    with TestClient(app_main.app) as c:
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
