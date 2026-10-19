"""D3: ``MCP_BEARER_TOKEN`` must be verified server-side on ``/mcp``.

The scope token authorises individual tool calls; the bearer token is the
outer guard that keeps unauthenticated callers out of the MCP handshake
entirely. These tests prove the guard actually runs on the mounted endpoint,
not just that a config value exists.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main as app_main
from app.asgi_auth import BearerTokenMiddleware
from app.store.memory import InMemoryStateStore

MCP_TOKEN = "mcp-secret-token"


# ---------------------------------------------------------------------------
# middleware unit tests (no MCP package required)
# ---------------------------------------------------------------------------


def _guarded_app(token: str = MCP_TOKEN) -> FastAPI:
    inner = FastAPI()

    @inner.get("/probe")
    async def probe():  # pragma: no cover - trivial
        return {"ok": True}

    outer = FastAPI()
    outer.mount("/mcp", BearerTokenMiddleware(inner, token))
    return outer


def test_missing_authorization_is_rejected():
    with TestClient(_guarded_app()) as client:
        resp = client.get("/mcp/probe")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    # The expected token must never be echoed back.
    assert MCP_TOKEN not in resp.text


def test_wrong_token_is_rejected():
    with TestClient(_guarded_app()) as client:
        resp = client.get("/mcp/probe", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_non_bearer_scheme_is_rejected():
    with TestClient(_guarded_app()) as client:
        resp = client.get("/mcp/probe", headers={"Authorization": f"Basic {MCP_TOKEN}"})
    assert resp.status_code == 401


def test_correct_token_passes_through():
    with TestClient(_guarded_app()) as client:
        resp = client.get("/mcp/probe", headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_www_authenticate_header_present():
    with TestClient(_guarded_app()) as client:
        resp = client.get("/mcp/probe")
    assert resp.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# integration: the guard is actually wired onto the mounted /mcp endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_client(settings):
    settings.mcp_enabled = True
    settings.mcp_bearer_token = MCP_TOKEN
    settings.jr_scope_secret = "scope-secret"
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    with TestClient(app_main.create_app()) as c:
        yield c
    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None


def test_mounted_mcp_rejects_unauthenticated(mcp_client):
    """An unauthenticated request must not reach the MCP handshake."""
    resp = mcp_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 401


def test_mounted_mcp_accepts_correct_bearer(mcp_client):
    """With the right token the request passes the guard (MCP then responds)."""
    resp = mcp_client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {MCP_TOKEN}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    # Not 401: the request got past the guard and into the MCP app.
    assert resp.status_code != 401


def test_no_bearer_configured_leaves_endpoint_open(settings):
    """Without MCP_BEARER_TOKEN the guard is not installed (scope token still guards calls)."""
    settings.mcp_enabled = True
    settings.mcp_bearer_token = None
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    with TestClient(app_main.create_app()) as c:
        resp = c.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    assert resp.status_code != 401
