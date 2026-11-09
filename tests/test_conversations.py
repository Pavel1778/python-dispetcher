"""Persistent conversations and the chat turn, over real HTTP and PostgreSQL.

The provider is a real local HTTP server; the web server, auth, project storage,
conversation persistence and membership checks are all real. The test proves the
chat is real (a provider answer is stored, an outage is an error, not a fabricated
reply), conversations persist and restore, and project isolation holds.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import AuthService, hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.test_nexus_projects import request  # noqa: E402
from tests.test_providers import FakeProvider, chat_body  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PLAN = {"tasks": [{"id": "t1", "title": "Fix the bug", "description": "d",
                   "dependencies": []}]}


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
    srv.script = [{"payload": {"choices": [{"message": {"content": "Привет! Готов помочь."}}]}}]
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
    cloud = FakeCloudServer()
    cloud.__enter__()
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("t"),
                           session_secret="test-session-secret-which-is-long-enough")
    app.providers = ProviderCascade([
        GroqProvider(base_url=f"http://127.0.0.1:{fake_provider.server_port}",
                     api_key="k", model="m", sleep=lambda _s: None)])
    # Point the Cloud transport at the fake so code/autonomous dispatch is real.
    app.client = CloudClient(cloud.base_url, api_key="fake", timeout=10.0)
    app.dispatcher.client = app.client
    app.runtime.client = app.client
    app.verifier.client = app.client
    # Rebuild the chat responder so it uses the pointed cascade and dispatch.
    from orchestration.nexus_chat import ChatResponder
    app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                             start_autonomous=app._chat_start_autonomous)
    alice = app.auth._open_session("alice")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    try:
        yield f"http://127.0.0.1:{server.port}", alice, app, fake_provider
    finally:
        server.stop()
        cloud.__exit__(None, None, None)


def _project(base, session, name="Lab"):
    _, proj = request(base, "/api/v1/projects", "POST", {"name": name}, session=session)
    return proj["project"]["id"]


def _conversation(base, session, pid, **body):
    st, conv = request(base, f"/api/v1/projects/{pid}/conversations", "POST",
                       body or {"title": "Чат"}, session=session)
    assert st == 201
    return conv["conversation"]["id"]


def test_create_list_and_restore_conversations(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid, title="Обсуждение")
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert st == 200 and listing["conversations"][0]["id"] == cid
    # Search by a Cyrillic title.
    from urllib.parse import quote
    q = quote("обсуж")
    st, found = request(base, f"/api/v1/projects/{pid}/conversations?q={q}", session=alice)
    assert st == 200 and len(found["conversations"]) == 1
    st, none = request(base, f"/api/v1/projects/{pid}/conversations?q=xyz", session=alice)
    assert none["conversations"] == []


def test_chat_turn_stores_a_real_provider_answer(http_server):
    base, alice, _app, provider = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                      "POST", {"text": "Привет, что делаешь?", "mode": "ask"}, session=alice)
    assert st == 200
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"]
    assert out["messages"][1]["content"] == "Привет! Готов помочь."
    assert provider.calls, "the provider was really called"
    # The transcript persists and restores.
    st, restored = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                           session=alice)
    assert st == 200 and len(restored["messages"]) == 2
    assert restored["messages"][1]["content"] == "Привет! Готов помочь."


def test_chat_mode_plan_returns_a_real_plan(http_server):
    base, alice, _app, provider = http_server
    provider.script = [{"payload": chat_body(PLAN)}]
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                      "POST", {"text": "Спланируй", "mode": "plan"}, session=alice)
    assert st == 200
    assistant = out["messages"][-1]
    assert "Fix the bug" in assistant["content"]
    assert assistant["meta"]["plan"][0]["title"] == "Fix the bug"


def test_chat_failure_is_an_error_not_a_fabricated_answer(http_server):
    base, alice, _app, provider = http_server
    provider.script = [{"status": 401, "payload": {}}]
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                      "POST", {"text": "привет", "mode": "ask"}, session=alice)
    assert st == 502
    assert out["messages"][-1]["role"] == "system"
    assert out["messages"][-1]["meta"]["error"] is True
    # The user turn is preserved even though the assistant failed.
    st, restored = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                           session=alice)
    assert any(m["role"] == "user" for m in restored["messages"])
    # The conversation is marked as errored, not idle.
    st, conv = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", session=alice)
    assert conv["conversation"]["state"] == "error"


def test_chat_code_mode_starts_a_real_workflow(http_server):
    base, alice, app, _ = http_server
    pid = _project(base, alice)
    # Bind a repository so code mode has a target.
    app.store.add_repository("Pavel1778/python-dispetcher", allowed_branches=["main"])
    st, _ = request(base, f"/api/v1/projects/{pid}/binding", "POST",
                    {"full_name": "Pavel1778/python-dispetcher", "branch": "main"},
                    session=alice)
    assert st == 200
    cid = _conversation(base, alice, pid)
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                      "POST", {"text": "Добавь тест", "mode": "code"}, session=alice)
    assert st == 200, out
    tool = next(m for m in out["messages"] if m["role"] == "tool")
    assert tool["meta"]["tool"] == "start_workflow"
    assert tool["meta"]["workflow_id"]
    with app.store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM workflows")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM nexus_agent_sessions")
        assert cur.fetchone()["n"] == 1


def test_conversations_are_project_isolated(http_server):
    base, alice, app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    bob = app.auth._open_session("bob")
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", session=bob)
    assert st == 404
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                    "POST", {"text": "hi"}, session=bob)
    assert st == 404


def test_rename_archive_and_mode_change(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "PATCH",
                      {"title": "Новое имя"}, session=alice)
    assert st == 200 and out["conversation"]["title"] == "Новое имя"
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "PATCH",
                      {"mode": "plan"}, session=alice)
    assert out["conversation"]["mode"] == "plan"
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "DELETE",
                    session=alice)
    assert st == 200
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert listing["conversations"] == []
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations?archived=1",
                          session=alice)
    assert len(listing["conversations"]) == 1


def test_conversation_routes_require_auth(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    assert request(base, f"/api/v1/projects/{pid}/conversations")[0] == 401
    assert request(base, f"/api/v1/projects/{pid}/conversations", "POST",
                   {"title": "x"})[0] == 401
