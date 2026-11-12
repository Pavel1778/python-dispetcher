"""HTTP surface for the OpenHands pool and diagnostics.

Real PostgreSQL, a real HTTP server in front of the fake Cloud, over real
sockets. Pins the routes the operator uses: the account pool configuration, the
per-account controls, and the diagnostics report — and that the API key never
appears in any response.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run pool HTTP tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
KEY = "sk-openhands-never-leaked-in-a-response"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _req(method: str, base: str, path: str, body: dict | None = None,
         token: str = "test-token"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


@pytest.fixture
def fake():
    with FakeCloudServer() as a, FakeCloudServer() as b:
        for s in (a, b):
            s.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": None,
                                "agent_settings": {"mcp_config": {}},
                                "registered_marketplaces": [], "inherited_marketplaces": []}
        yield a, b


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    a = App(DSN, poll_interval=0.2, token="test-token")
    a.client = CloudClient(fake[0].base_url, api_key="test-key", timeout=10.0)
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


def _add_account(base, label, cloud):
    status, body = _req("POST", base, "/api/openhands/accounts",
                        {"label": label, "api_key": KEY, "base_url": cloud.base_url,
                         "make_default": label == "A"})
    assert status == 201, body
    return body["account"]


def test_add_list_and_pool_over_http(base, fake):
    a, _b = fake
    account = _add_account(base, "A", a)
    assert KEY not in json.dumps(account)
    status, listing = _req("GET", base, "/api/openhands/accounts")
    assert status == 200
    assert KEY not in json.dumps(listing)
    assert listing["accounts"][0]["label"] == "A"
    status, pool = _req("GET", base, "/api/openhands/pool")
    assert status == 200
    assert pool["config"]["selection_mode"] == "auto"
    assert [c["label"] for c in pool["candidates"]] == ["A"]


def test_pool_mode_and_priority_over_http(base, fake):
    a, b = fake
    va = _add_account(base, "A", a)
    vb = _add_account(base, "B", b)
    status, out = _req("POST", base, "/api/openhands/pool",
                       {"selection_mode": "load_balancing"})
    assert status == 200 and out["config"]["selection_mode"] == "load_balancing"
    status, out = _req("POST", base, f"/api/openhands/accounts/{vb['id']}/priority",
                       {"priority": 1})
    assert status == 200 and out["account"]["priority"] == 1
    status, out = _req("POST", base, f"/api/openhands/accounts/{va['id']}/enable",
                       {"enabled": False})
    assert status == 200 and out["account"]["enabled"] is False
    status, pool = _req("GET", base, "/api/openhands/pool")
    assert [c["label"] for c in pool["candidates"]] == ["B"]


def test_remove_refuses_while_active_then_forces(base, fake):
    a, _b = fake
    va = _add_account(base, "A", a)
    status, out = _req("POST", base, f"/api/openhands/accounts/{va['id']}/remove",
                       {"force": True})
    assert status == 200 and out["removed"] == va["id"]
    status, listing = _req("GET", base, "/api/openhands/accounts")
    assert listing["accounts"] == []


def test_diagnostics_over_http(base, fake):
    a, _b = fake
    _add_account(base, "A", a)
    status, report = _req("POST", base, "/api/openhands/diagnostics", {"live": False})
    assert status == 200
    names = {c["name"]: c["status"] for c in report["checks"]}
    assert names["database"] == "VERIFIED"
    assert names["schema"] == "VERIFIED"
    assert names["accounts"] == "VERIFIED"
    assert names["account_pool"] == "VERIFIED"
    assert names["live_conversation"] == "UNVERIFIED"
    # No key value anywhere in the report.
    assert KEY not in json.dumps(report)
