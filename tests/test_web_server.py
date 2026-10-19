"""Web server tests: real HTTP against a real PostgreSQL and a fake Cloud.

The server under test is the real :class:`orchestration.web_server.WebServer`;
requests go over a real socket. Only the Cloud API is a local fake (the same one
the dispatcher tests use), so the full request path is exercised.
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
    not DSN, reason="set TEST_SUPABASE_DB_URL to run web server tests"
)

_MIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sr_tool_calls, task_events, conversations, "
                "tasks, repositories, workflows CASCADE"
            )
            cur.execute(open(os.path.join(_MIG, "0001_init.sql")).read())
            cur.execute(open(os.path.join(_MIG, "0002_sr_runtime.sql")).read())


class Client:
    """Tiny JSON HTTP client for the tests."""

    def __init__(self, base: str):
        self.base = base

    def get(self, path: str):
        return self._req("GET", path)

    def get_text(self, path: str):
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()

    def post(self, path: str, body=None):
        return self._req("POST", path, body)

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def server(fake, monkeypatch):
    # Point the App's CloudClient at the fake by URL, and give it a key.
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    app = App(DSN, poll_interval=0.2)
    app.client = CloudClient(fake.base_url, api_key="test-key", timeout=5.0)
    app.dispatcher.client = app.client
    app.runtime.client = app.client
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    # The background loop must not race the assertions; keep it idle by not
    # relying on it. Tests drive ticks explicitly where needed.
    yield ws
    ws.stop()


@pytest.fixture
def http(server):
    return Client(f"http://127.0.0.1:{server.port}")


# ------------------------------------------------------------------- static
def test_page_is_served(http):
    st, text = http.get_text("/")
    assert st == 200
    assert "Sr/Jr dispatcher" in text
    assert "/api/workflows" in text


def test_config_reports_cloud_key(http):
    st, body = http.get("/api/config")
    assert st == 200 and body["cloud_key"] is True


def test_health(http):
    st, body = http.get("/api/health")
    assert st == 200 and body["store"] is True


# -------------------------------------------------------------- repositories
def test_add_and_list_repository(http):
    st, body = http.post("/api/repositories",
                         {"full_name": "acme/repo", "allowed_branches": ["main"]})
    assert st == 200 and body["repository_id"]
    st, body = http.get("/api/repositories")
    names = [r["full_name"] for r in body["repositories"]]
    assert "acme/repo" in names


def test_add_repository_rejects_bad_name(http):
    st, body = http.post("/api/repositories", {"full_name": "not-a-repo"})
    assert st == 400 and "owner/name" in body["error"]


# --------------------------------------------------------------- workflows
def test_start_workflow_creates_sr(http, server, fake):
    st, body = http.post("/api/workflows", {"goal": "ship the feature"})
    assert st == 200
    wf = body["workflow_id"]
    assert body["sr_conversation_id"]
    # A real Sr conversation exists on the fake Cloud, with the Sr tools.
    cid = body["sr_conversation_id"]
    names = {s["name"] for s in fake.cloud.client_tools[cid]}
    assert "sr_create_task" in names
    # And it is persisted against the workflow.
    st, detail = http.get(f"/api/workflows/{wf}")
    assert st == 200
    assert detail["workflow"]["sr_conversation_id"] == cid


def test_start_workflow_requires_goal(http):
    st, body = http.post("/api/workflows", {"goal": "   "})
    assert st == 400


def test_start_workflow_rejects_disallowed_repository(http):
    st, body = http.post("/api/workflows",
                         {"goal": "x", "repository": "evil/repo"})
    assert st == 400 and "not allowed" in body["error"]


def test_workflow_detail_lists_tasks_and_tool_calls(http, server, fake):
    st, body = http.post("/api/workflows", {"goal": "build"})
    wf = body["workflow_id"]
    cid = body["sr_conversation_id"]

    # The Sr agent calls sr_create_task.
    fake.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "do a unit"}
    )
    server.app.runtime.tick()

    st, detail = http.get(f"/api/workflows/{wf}")
    assert st == 200
    assert len(detail["tasks"]) == 1
    assert detail["tasks"][0]["goal"] == "do a unit"
    assert detail["tool_calls"][0]["name"] == "sr_create_task"


def test_unknown_workflow_is_404(http):
    st, _ = http.get("/api/workflows/does-not-exist")
    assert st == 404


# ------------------------------------------------------------- Sr messaging
def test_message_to_missing_sr_is_409(http):
    # A workflow with no Sr conversation cannot receive messages.
    st, body = http.post("/api/workflows", {"goal": "wf"})
    wf = body["workflow_id"]
    # Simulate "Sr not ready" by clearing the bound conversation.
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE workflows SET sr_conversation_id=NULL WHERE id=%s", (wf,))
    st, body = http.post(f"/api/workflows/{wf}/message", {"text": "hi"})
    assert st == 409


def test_send_message_reaches_cloud(http, server, fake):
    st, body = http.post("/api/workflows", {"goal": "wf"})
    wf = body["workflow_id"]
    cid = body["sr_conversation_id"]
    st, body = http.post(f"/api/workflows/{wf}/message", {"text": "status?"})
    assert st == 200 and body["sent"] is True
    assert "status?" in fake.cloud.messages[cid]


def test_send_message_rejects_empty(http):
    st, body = http.post("/api/workflows", {"goal": "wf"})
    wf = body["workflow_id"]
    st, body = http.post(f"/api/workflows/{wf}/message", {"text": ""})
    assert st == 400
