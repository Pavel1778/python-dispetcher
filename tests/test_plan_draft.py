"""The goal -> provider-plan-draft route, over real HTTP and PostgreSQL.

The provider is a real local HTTP server (see :mod:`tests.test_providers`); the
web server, auth, project/goal storage and membership checks are all real. The
test proves the route is reachable, authorized, and returns a *draft* that is
not stored and dispatches nothing.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.providers import ProviderCascade  # noqa: E402
from orchestration.web_auth import AuthService, hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.test_nexus_projects import request  # noqa: E402
from tests.test_providers import FakeProvider, chat_body  # noqa: E402
from orchestration.providers import GroqProvider  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def fake_provider():
    from http.server import ThreadingHTTPServer
    import threading

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    srv.script = [{"payload": chat_body()}]
    srv.calls = []
    srv.index = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join()
    srv.server_close()


@pytest.fixture
def http_server(fake_provider, monkeypatch):
    _reset()
    monkeypatch.setenv("OPENHANDS_API_KEY", "fake")
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("t"),
                           session_secret="test-session-secret-which-is-long-enough")
    # Point the cascade at the fake provider so the route makes a real call.
    app.providers = ProviderCascade([
        GroqProvider(base_url=f"http://127.0.0.1:{fake_provider.server_port}",
                     api_key="k", model="m", sleep=lambda _s: None)
    ])
    alice = app.auth._open_session("alice")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", alice, app
    server.stop()


def test_plan_draft_returns_a_provider_plan(http_server):
    base, alice, _ = http_server
    _, proj = request(base, "/api/v1/projects", "POST", {"name": "Lab"}, session=alice)
    pid = proj["project"]["id"]
    _, goal = request(base, f"/api/v1/projects/{pid}/goals", "POST", {"text": "Ship v1"}, session=alice)
    gid = goal["goal"]["id"]

    status, payload = request(base, f"/api/v1/projects/{pid}/goals/{gid}/plan-draft",
                              "POST", {}, session=alice)
    assert status == 200
    assert payload["provider"] == "groq"
    assert payload["draft"]["tasks"][0]["title"] == "Do the thing"
    assert payload["stored"] is False

    # Nothing was persisted and nothing was dispatched.
    status, plans = request(base, f"/api/v1/projects/{pid}/goals/{gid}/plans", session=alice)
    assert status == 200 and plans["plans"] == []
    with http_server[2].store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM workflows")
        assert cur.fetchone()["n"] == 0


def test_plan_draft_requires_a_session(http_server):
    base, alice, _ = http_server
    _, proj = request(base, "/api/v1/projects", "POST", {"name": "Lab"}, session=alice)
    pid = proj["project"]["id"]
    _, goal = request(base, f"/api/v1/projects/{pid}/goals", "POST", {"text": "x"}, session=alice)
    gid = goal["goal"]["id"]
    assert request(base, f"/api/v1/projects/{pid}/goals/{gid}/plan-draft", "POST", {})[0] == 401


def test_plan_draft_for_a_foreign_project_is_404(http_server):
    base, alice, app = http_server
    _, proj = request(base, "/api/v1/projects", "POST", {"name": "Lab"}, session=alice)
    pid = proj["project"]["id"]
    _, goal = request(base, f"/api/v1/projects/{pid}/goals", "POST", {"text": "x"}, session=alice)
    gid = goal["goal"]["id"]
    bob = app.auth._open_session("bob")
    assert request(base, f"/api/v1/projects/{pid}/goals/{gid}/plan-draft",
                   "POST", {}, session=bob)[0] == 404
