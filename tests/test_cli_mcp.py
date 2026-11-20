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
from orchestration.nexus_lab_store import LabStore  # noqa: E402
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
        "list_runs", "get_run", "cancel_run", "restart_run", "duplicate_run",
        "list_run_task_outputs", "get_run_task_output",
        "list_project_executors", "assign_project_executor", "get_execution_profile",
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


def test_mcp_output_tools_are_honest(app):
    """The two output tools: an unbound task says so, and a foreign project is
    "not found". On the app transport (the fixture) the full output cannot be
    reached, so the tool says ``unsupported`` rather than returning nothing."""
    from orchestration.mcp_server import McpError

    projects = app.projects
    pid = projects.create("user:owner", "Out")["id"]
    goal = projects.create_goal("user:owner", pid, "g")[0]["id"]
    lab = LabStore(app.store)
    run = lab.start_run("user:owner", pid, goal)
    task = lab.add_task("user:owner", pid, run["id"], task_key="t", title="t",
                        repository="", branch="")
    server = McpServer(app.store, "user:owner")
    # No conversation yet: an empty list, not an error.
    result = server.handle("tools/call", {"name": "list_run_task_outputs", "arguments": {
        "project_id": str(pid), "run_id": str(run["id"]), "task_id": str(task["id"])}})
    listed = json.loads(result["content"][0]["text"])
    assert listed["outputs"] == [] and listed["conversation_id"] is None
    # A project the caller is not a member of is not found.
    other = projects.create("user:someone", "Theirs")["id"]
    with pytest.raises(McpError):
        server.handle("tools/call", {"name": "list_run_task_outputs", "arguments": {
            "project_id": str(other), "run_id": str(run["id"]), "task_id": str(task["id"])}})


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
    assert len(lines[1]["result"]["tools"]) == 20


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
    assert len(body["result"]["tools"]) == 20


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


def test_cli_registers_run_restart_and_duplicate():
    """The CLI exposes `run restart` and `run duplicate` (the new run actions)."""
    parser = cli.build_parser()
    # Find the `run` subparser's choices.
    subs = next(a for a in parser._subparsers._group_actions if a.dest == "command")
    run_parser = subs.choices["run"]
    run_subs = next(a for a in run_parser._subparsers._group_actions if a.dest == "sub")
    assert "restart" in run_subs.choices
    assert "duplicate" in run_subs.choices


def test_mcp_registers_restart_and_duplicate(app):
    """The MCP server exposes restart_run and duplicate_run, scoped for control."""
    server = McpServer(app.store, "user:owner")
    names = {t["name"] for t in server.handle("tools/list", {})["tools"]}
    assert "restart_run" in names
    assert "duplicate_run" in names
    from orchestration.nexus_policy import MCP_TOOL_SCOPES, SCOPE_RUN_CONTROL
    assert MCP_TOOL_SCOPES["restart_run"] == SCOPE_RUN_CONTROL
    assert MCP_TOOL_SCOPES["duplicate_run"] == SCOPE_RUN_CONTROL


def test_profile_cli_uses_authenticated_http_and_distinguishes_blocked(base, monkeypatch, capsys):
    from tests.test_execution_profile_barriers import desired
    monkeypatch.setenv('NEXUS_URL', base)
    monkeypatch.setenv('NEXUS_TOKEN', TOKEN)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(desired())))
    assert cli.main(['openhands','profile','save','--json']) == 0
    assert json.loads(capsys.readouterr().out)['profile']['revision'] == 1
    assert cli.main(['openhands','profile','sync','--json']) == cli.EXIT['conflict']
    assert TOKEN not in capsys.readouterr().out
    assert cli.main(['openhands','profile','show','--json']) == 0
    assert json.loads(capsys.readouterr().out)['profile']['observation']['state'] == 'blocked'


def test_verifier_capability_checks_current_membership_commit_and_expiry(base, app, monkeypatch):
    import secrets
    import time
    from orchestration.evidence_trust import EvidenceTrustEngine, Keyring
    from orchestration.nexus_evidence import EvidenceService
    from orchestration.verification_grant import VerificationGrant
    from tests.test_lab_evidence import _signer_env

    actor, token = 'agent:reviewer', secrets.token_hex(32)
    pid = str(app.projects.create(actor,'probe')['id'])
    foreign = str(app.projects.create(actor,'not delegated')['id'])  # membership alone is insufficient
    signer = _signer_env(monkeypatch)
    record = EvidenceService(app.store,EvidenceTrustEngine(Keyring.from_env(),signer)).record(
        subject_kind='project', subject_id=pid, project_id=pid, commit_sha='a'*40, detail={'report':'private details'})
    for k,v in [('TOKEN',token),('PRINCIPAL',actor),('PROJECT',pid),('EXPIRES',str(int(time.time()+60)))]:
        monkeypatch.setenv('NEXUS_MCP_VERIFY_'+k,v)
    app.verification_grant = VerificationGrant.from_env(owner_token=TOKEN)
    def send(method,params):
        return _mcp_post(base, {'jsonrpc':'2.0','id':1,'method':method,'params':params}, token)
    def call(name, **args):
        return send('tools/call', {'name':name, 'arguments':args})[1]
    assert {s['name'] for s in send('tools/list',{})[1]['result']['tools']} == {
        'list_evidence','verify_evidence','get_execution_profile'}
    assert call('list_evidence',project_id=foreign)['error']['code'] == -32003
    assert call('cancel_run',project_id=pid)['error']['code'] == -32003
    for sha,expected in [('a'*40,'VERIFIED'),('b'*40,'UNVERIFIED')]:
        answer = call('verify_evidence',project_id=pid,evidence_id=record['id'],expected_commit=sha)
        payload = json.loads(answer['result']['content'][0]['text'])
        assert payload['evidence']['verdict'] == expected and 'private details' not in json.dumps(payload)
    assert app.store.list_evidence(project_id=pid)[0]['verdict'] == 'VERIFIED'  # no verifier write
    req = urllib.request.Request(base+'/api/v1/projects',headers={'Authorization':'Bearer '+token})
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(req)
    assert denied.value.code == 401
    with app.store._tx() as c:
        c.execute('DELETE FROM nexus_project_members WHERE project_id=%s AND principal=%s',(pid,actor))
    assert call('list_evidence',project_id=pid)['error']['code'] == -32004
    monkeypatch.setenv('NEXUS_MCP_VERIFY_EXPIRES','1')
    app.verification_grant = VerificationGrant.from_env(owner_token=TOKEN)
    assert send('tools/list',{})[0] == 401


def test_empty_evidence_is_not_qa_success(base,app,monkeypatch,capsys):
    project = cli.Client(base,TOKEN).post('/api/v1/projects',{'name':'empty'})['project']['id']
    monkeypatch.setenv('NEXUS_URL',base)
    monkeypatch.setenv('NEXUS_TOKEN',TOKEN)
    assert cli.main(['qa','verify','--project',project,'--json']) == cli.EXIT['conflict']
    assert json.loads(capsys.readouterr().out)['ok'] is False
