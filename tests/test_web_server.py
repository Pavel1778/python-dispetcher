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
                "DROP TABLE IF EXISTS sessions, login_attempts, sr_tool_calls, "
                "task_events, conversations, tasks, repositories, workflows CASCADE"
            )
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class Client:
    """Tiny JSON HTTP client for the tests.

    Sends the bearer token on every request, because reads are now protected
    too: the machine token authenticates both reads and writes.
    """

    def __init__(self, base: str, token: str | None = None):
        self.base = base
        self.token = token

    def get(self, path: str):
        return self._req("GET", path)

    def get_text(self, path: str):
        return self._req("GET", path, raw=True)

    def post(self, path: str, body=None):
        return self._req("POST", path, body)

    def _req(self, method: str, path: str, body=None, raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                rawbody = r.read().decode()
                if raw:
                    return r.status, rawbody
                return r.status, (json.loads(rawbody) if rawbody else None)
        except urllib.error.HTTPError as e:
            rawbody = e.read().decode()
            if raw:
                return e.code, rawbody
            return e.code, (json.loads(rawbody) if rawbody else None)


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
    app = App(DSN, poll_interval=0.2, token="test-token")
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
    return Client(f"http://127.0.0.1:{server.port}", token="test-token")


# ------------------------------------------------------------------- static
def test_page_is_served(http):
    st, text = http.get_text("/")
    assert st == 200
    assert "NEXUS" in text
    assert "/static/app.js" in text


def test_page_has_the_lab_and_github_tabs(http):
    st, text = http.get_text("/")
    assert st == 200
    # Every button in the UI must target a real element; these ids are what the
    # new panels and their handlers rely on.
    for marker in ('id="tab-lab"', 'id="panel-lab"', 'id="tab-github"',
                   'id="panel-github"', 'id="upload-form"', 'id="rec-start"',
                   'id="gh-create-form"', 'id="gh-confirm-card"'):
        assert marker in text, marker


def test_page_is_the_ai_ide_shell(http):
    """The first screen is the AI IDE workspace, not the old dashboard."""
    st, text = http.get_text("/")
    assert st == 200
    for marker in ('id="sidebar"', 'id="chat-log"', 'id="chat-input"', 'id="chat-send"',
                   'id="panel-chat"', 'id="panel-ide"', 'id="panel-agents"',
                   'id="ws-project"', 'id="repo-picker"', 'id="repo-list"',
                   'id="repo-account"', 'id="branch-list"', 'id="repo-bind"',
                   'id="rightpanel"', 'id="rp-toggle"', 'id="new-chat"',
                   'id="menu-toggle"', 'class="mode-chip"'):
        assert marker in text, marker
    # The dashboard metric strip is no longer the first thing a user sees.
    assert 'id="metrics"' not in text


def test_client_script_contains_the_new_capabilities(http):
    st, text = http.get_text("/static/app.js")
    assert st == 200
    for fn in ("initLab", "initGithub", "loadFiles", "startRecording",
               "proposeCreate", "showConfirm", "uploadFile"):
        assert fn in text, fn


def test_static_assets_never_embed_a_secret_value(server, monkeypatch):
    # A secret *value* must never be served to the browser. Env var *names*
    # (e.g. GITHUB_TOKEN shown in an operator hint) are fine; values are not.
    monkeypatch.setenv("OPENHANDS_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("WEB_AUTH_TOKEN", "machine-token")
    client = Client(f"http://127.0.0.1:{server.port}", token="test-token")
    for path in ("/", "/static/app.js", "/static/app.css", "/login"):
        st, text = client.get_text(path)
        assert st == 200
        assert "sk-super-secret-value" not in text
        assert "test-key" not in text


def _get_with_headers(base: str, path: str, token: str | None = None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_favicon_is_served_as_svg(server):
    """The browser's automatic /favicon.ico probe must not 404."""
    base = f"http://127.0.0.1:{server.port}"
    st, headers, body = _get_with_headers(base, "/favicon.ico")
    assert st == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert b"<svg" in body

    st, headers, body = _get_with_headers(base, "/favicon.svg")
    assert st == 200
    assert headers["Content-Type"] == "image/svg+xml"


def test_favicon_is_public_and_has_no_secret(server):
    """The icon loads before login, so it must reveal nothing."""
    base = f"http://127.0.0.1:{server.port}"
    st, _, body = _get_with_headers(base, "/favicon.svg")
    assert st == 200
    text = body.decode()
    for needle in ("key", "token", "password", "secret"):
        assert needle not in text.lower()


def test_pages_declare_the_favicon(http):
    """Both pages must point at the icon so the tab shows the mark."""
    for path in ("/", "/login"):
        st, text = http.get_text(path)
        assert st == 200
        assert 'rel="icon"' in text and "/favicon.svg" in text


def test_login_page_is_public(server):
    anon = Client(f"http://127.0.0.1:{server.port}")
    st, text = anon.get_text("/login")
    assert st == 200
    assert "Войти" in text


def test_healthz_is_public_and_minimal(server):
    anon = Client(f"http://127.0.0.1:{server.port}")
    st, body = anon.get("/healthz")
    assert st == 200 and body == {"status": "ok"}


def test_config_reports_cloud_key(http):
    st, body = http.get("/api/config")
    assert st == 200 and body["cloud_key"] is True


def test_health(http):
    st, body = http.get("/api/health")
    assert st == 200 and body["store"] is True


def test_dashboard_counts(http):
    st, body = http.get("/api/dashboard")
    assert st == 200 and "counts" in body
    assert set(body["counts"]) >= {"tasks", "queued", "running", "completed", "failed"}


def test_system_info_hides_secrets(http, monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "super-secret-value")
    st, body = http.get("/api/system")
    assert st == 200
    assert "super-secret-value" not in json.dumps(body)
    assert body["store"]["ok"] is True
    assert "login_missing" in body["config"]


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


# ------------------------------------------------------ github auto-connect
@pytest.fixture
def gh_server():
    from tests.fake_github import FakeGitHubServer

    with FakeGitHubServer() as s:
        yield s


@pytest.fixture
def gh_http(server, gh_server):
    """The web client, with the App's GitHub client pointed at the fake."""
    from orchestration.github_client import GitHubClient

    server.app.github = GitHubClient(
        gh_server.base_url, token="ghp_test_token", timeout=5.0
    )
    return Client(f"http://127.0.0.1:{server.port}", token="test-token")


def test_sync_imports_repositories_without_typing_names(gh_http):
    st, body = gh_http.post("/api/repositories/sync", {})
    assert st == 200
    assert body["added"] >= 2
    assert body["total"] >= 2

    st, listed = gh_http.get("/api/repositories")
    names = [r["full_name"] for r in listed["repositories"]]
    assert "Pavel1778/python-dispetcher" in names
    assert "Pavel1778/Klyrshift" in names


def test_sync_skips_archived_repositories(gh_http):
    gh_http.post("/api/repositories/sync", {})
    st, listed = gh_http.get("/api/repositories")
    names = [r["full_name"] for r in listed["repositories"]]
    assert "Pavel1778/old-archive" not in names


def test_sync_keeps_default_branch_from_github(gh_http):
    gh_http.post("/api/repositories/sync", {})
    st, listed = gh_http.get("/api/repositories")
    row = {r["full_name"]: r for r in listed["repositories"]}["Pavel1778/python-dispetcher"]
    assert row["default_branch"] == "main"


def test_sync_is_idempotent(gh_http):
    st, first = gh_http.post("/api/repositories/sync", {})
    assert first["added"] >= 2
    st, second = gh_http.post("/api/repositories/sync", {})
    assert second["added"] == 0
    assert second["total"] == first["total"]


def test_sync_does_not_reenable_a_repository_an_operator_disabled(gh_http, server):
    """A sync refreshes metadata; it must not undo an operator's decision."""
    gh_http.post("/api/repositories/sync", {})
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "UPDATE repositories SET enabled=false WHERE full_name=%s",
            ("Pavel1778/Klyrshift",),
        )
    gh_http.post("/api/repositories/sync", {})
    st, listed = gh_http.get("/api/repositories")
    names = [r["full_name"] for r in listed["repositories"]]
    assert "Pavel1778/Klyrshift" not in names


def test_sync_reports_auth_failure_without_corrupting_the_list(http, server):
    from orchestration.github_client import GitHubClient

    server.app.github = GitHubClient("http://127.0.0.1:1", token="x", timeout=2.0)
    st, body = http.post("/api/repositories/sync", {})
    assert st == 503
    assert "unavailable" in body["error"]
    # The allowlist is untouched by a failed discovery.
    st, listed = http.get("/api/repositories")
    assert listed["repositories"] == []


def test_github_preview_does_not_write(gh_http):
    st, body = gh_http.get("/api/github/repositories")
    assert st == 200
    assert body["count"] >= 2
    # Preview only: nothing landed in the allowlist.
    st, listed = gh_http.get("/api/repositories")
    assert listed["repositories"] == []


def test_github_preview_needs_a_token(server, http, monkeypatch):
    from orchestration.github_client import GitHubClient

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    server.app.github = GitHubClient(token="", timeout=2.0)
    st, body = http.get("/api/github/repositories")
    assert st == 400 and "auth" in body["error"]


def test_remove_repository(http):
    http.post("/api/repositories", {"full_name": "acme/gone"})
    st, body = http.post("/api/repositories/remove", {"full_name": "acme/gone"})
    assert st == 200 and body["removed"] == "acme/gone"
    st, listed = http.get("/api/repositories")
    assert "acme/gone" not in [r["full_name"] for r in listed["repositories"]]


def test_remove_unknown_repository_is_404(http):
    st, body = http.post("/api/repositories/remove", {"full_name": "nope/none"})
    assert st == 404


def test_repository_writes_require_auth(server):
    anon = Client(f"http://127.0.0.1:{server.port}")
    for path in ("/api/repositories/sync", "/api/repositories/remove"):
        st, _ = anon.post(path, {"full_name": "a/b"})
        assert st == 401, path


def test_system_reports_github_token_presence_not_value(http, server, monkeypatch):
    from orchestration.github_client import GitHubClient

    server.app.github = GitHubClient(token="ghp_super_secret_value", timeout=2.0)
    st, body = http.get("/api/system")
    assert st == 200
    assert body["config"]["github_token"] is True
    assert "ghp_super_secret_value" not in json.dumps(body)


def test_capabilities_report_github_discovery_state(gh_http):
    st, body = gh_http.get("/api/capabilities")
    assert st == 200
    assert body["github"]["token_configured"] is True
    assert body["github"]["allowlisted"] == 0
    gh_http.post("/api/repositories/sync", {})
    st, body = gh_http.get("/api/capabilities")
    assert body["github"]["allowlisted"] >= 2


def test_capabilities_still_report_github_when_cloud_is_down(http, server):
    """A Cloud outage must not hide whether the allowlist is usable."""
    from orchestration.cloud_client import CloudClient

    http.post("/api/repositories", {"full_name": "acme/known"})
    server.app.client = CloudClient("http://127.0.0.1:1", api_key="x", timeout=2.0)
    st, body = http.get("/api/capabilities")
    assert st == 502
    assert body["github"]["allowlisted"] == 1


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


# ------------------------------------------------------------------- auth
def test_writes_require_auth(server):
    anon = Client(f"http://127.0.0.1:{server.port}")  # no token, no session
    st, payload = anon.post("/api/repositories", {"full_name": "a/b"})
    assert st == 401
    assert "unauthorized" in payload["error"]
    # Reads are protected too: no token and no session is unauthorized.
    st, payload = anon.get("/api/workflows")
    assert st == 401


def test_wrong_token_is_rejected(server):
    bad = Client(f"http://127.0.0.1:{server.port}", token="nope")
    st, _ = bad.post("/api/repositories", {"full_name": "a/b"})
    assert st == 401
