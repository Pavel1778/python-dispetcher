"""CLI and MCP: thin clients that must not weaken the server's authorization.

Both surfaces are exercised against a real HTTP server and a real PostgreSQL:

* The CLI is a bearer client; it must return stable exit codes and never print a
  secret. It must not be able to read another principal's project.
* The MCP server speaks JSON-RPC over stdio; it exposes only the limited tool
  set, has no shell/SQL/eval tool, and enforces project membership on every
  call.

The project-isolation cases here are the CLI/MCP half of the negative security
tests; the HTTP half lives in :mod:`tests.test_project_binding` and
:mod:`tests.test_evidence_trust`.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import cli  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.mcp_server import McpServer, serve  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run CLI/MCP tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "test-token"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    a = App(DSN, poll_interval=0.2, token=TOKEN)
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    a.dispatcher.client = a.client
    a.runtime.client = a.client
    a.verifier.client = a.client
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


# ------------------------------------------------------------------------ CLI
def test_cli_exit_codes_are_stable():
    assert cli.EXIT == {
        "ok": 0, "error": 1, "usage": 2, "config": 3, "not_found": 4,
        "conflict": 5, "unavailable": 6, "timeout": 7, "refused": 8,
    }


def test_cli_requires_configuration(monkeypatch, capsys):
    monkeypatch.delenv("NEXUS_URL", raising=False)
    monkeypatch.delenv("NEXUS_BASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_TOKEN", raising=False)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    code = cli.main(["status"])
    assert code == cli.EXIT["config"]


def test_cli_project_lifecycle(base, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_URL", base)
    monkeypatch.setenv("NEXUS_TOKEN", TOKEN)
    assert cli.main(["project", "create", "Demo", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    pid = created["project"]["id"]
    assert cli.main(["project", "list", "--json"]) == 0
    projects = json.loads(capsys.readouterr().out)["projects"]
    assert [p["id"] for p in projects] == [pid]


def test_cli_does_not_print_the_token(base, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_URL", base)
    monkeypatch.setenv("NEXUS_TOKEN", TOKEN)
    cli.main(["doctor", "--json"])
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "token_present" in out


def test_cli_task_create_requires_project(base, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_URL", base)
    monkeypatch.setenv("NEXUS_TOKEN", TOKEN)
    # argparse rejects a missing --project with its usage exit code (2).
    with pytest.raises(SystemExit) as exc:
        cli.main(["task", "create", "goal"])
    assert exc.value.code == cli.EXIT["usage"]


def test_cli_unreachable_server_is_unavailable(monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NEXUS_TOKEN", TOKEN)
    code = cli.main(["status"])
    assert code == cli.EXIT["unavailable"]


# ------------------------------------------------------------------------ MCP
def test_mcp_initialize_and_tool_list(app):
    server = McpServer(app.store, "user:owner")
    init = server.handle("initialize", {})
    assert init["serverInfo"]["name"] == "nexus"
    tools = {t["name"] for t in server.handle("tools/list", {})["tools"]}
    assert tools == {
        "list_projects", "get_project", "list_repositories", "list_tasks",
        "get_task", "create_task", "dispatch_task", "get_execution_status",
        "list_evidence", "verify_evidence",
    }


def test_mcp_has_no_dangerous_tools(app):
    server = McpServer(app.store, "user:owner")
    names = {t["name"] for t in server.handle("tools/list", {})["tools"]}
    for forbidden in ("eval", "exec", "shell", "sql", "run", "subprocess"):
        assert forbidden not in names


def test_mcp_unknown_tool_is_an_error(app):
    from orchestration.mcp_server import McpError

    server = McpServer(app.store, "user:owner")
    with pytest.raises(McpError):
        server.handle("tools/call", {"name": "eval", "arguments": {}})


def test_mcp_project_isolation(app):
    from orchestration.mcp_server import McpError

    projects = app.projects
    alice = projects.create("user:alice", "Alice Project")["id"]
    server = McpServer(app.store, "user:bob")
    with pytest.raises(McpError):
        server.handle("tools/call", {"name": "get_project",
                                     "arguments": {"project_id": str(alice)}})


def test_mcp_create_task_refuses_unlinked_repository(app):
    from orchestration.mcp_server import McpError

    app.store.add_repository("acme/repo", allowed_branches=["main"])
    pid = app.projects.create("user:owner", "P")["id"]
    server = McpServer(app.store, "user:owner")
    with pytest.raises(McpError):
        server.handle("tools/call", {"name": "create_task", "arguments": {
            "project_id": str(pid), "goal": "x", "repository": "acme/repo"}})


def test_mcp_stdio_round_trip(app):
    server = McpServer(app.store, "user:owner")
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" +
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
    )
    stdout = io.StringIO()
    serve(server, stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["result"]["serverInfo"]["name"] == "nexus"
    assert len(lines[1]["result"]["tools"]) == 10


def test_mcp_parse_error_is_reported(app):
    server = McpServer(app.store, "user:owner")
    stdin = io.StringIO("this is not json\n")
    stdout = io.StringIO()
    serve(server, stdin=stdin, stdout=stdout)
    error = json.loads(stdout.getvalue().strip())
    assert error["error"]["code"] == -32700


# ------------------------------------------------------------ MCP over HTTP
def _mcp_post(base, message, token=TOKEN):
    req = urllib.request.Request(base + "/mcp", data=json.dumps(message).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, (json.loads(body) if body else None)


def test_mcp_http_requires_auth(base):
    status, _ = _mcp_post(base, {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                          token="wrong")
    assert status == 401


def test_mcp_http_initialize_and_tools(base):
    status, body = _mcp_post(base, {"jsonrpc": "2.0", "id": 1,
                                    "method": "initialize", "params": {}})
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "nexus"
    status, body = _mcp_post(base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(body["result"]["tools"]) == 10


def test_mcp_http_project_isolation(base, app):
    other = app.projects.create("user:someone-else", "Theirs")["id"]
    status, body = _mcp_post(base, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "get_project",
                                               "arguments": {"project_id": str(other)}}})
    assert status == 200
    assert "error" in body and body["error"]["code"] == -32004


def test_mcp_http_parse_error_is_400(base):
    req = urllib.request.Request(base + "/mcp", data=b"not json", method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400


def test_mcp_http_notification_is_202(base):
    req = urllib.request.Request(base + "/mcp",
                                 data=json.dumps({"jsonrpc": "2.0",
                                                  "method": "tools/list"}).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 202
