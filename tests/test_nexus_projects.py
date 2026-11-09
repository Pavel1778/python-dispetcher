"""Project isolation over real PostgreSQL and the real HTTP server.

Use a disposable database: these tests truncate NEXUS project tables.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestration.cloud_store import NotFound, PostgresStore
from orchestration.nexus_projects import IdempotencyConflict, ProjectService
from orchestration.web_auth import AuthService, hash_password
from orchestration.web_server import App, WebServer

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")


@pytest.fixture
def store():
    instance = PostgresStore(DSN)
    with instance._tx() as cur:
        # Every NEXUS table that references nexus_projects must be listed, or
        # TRUNCATE is refused with a foreign-key error. CASCADE also clears
        # workflows/tasks (D-2 project binding), which these tests never rely on.
        cur.execute(
            "TRUNCATE nexus_evidence, nexus_project_repositories, "
            "nexus_voice_notes, nexus_pending_actions, nexus_workspace_files, "
            "nexus_workspaces, nexus_files, nexus_plan_dependencies, nexus_plan_tasks, "
            "nexus_plans, nexus_project_events, nexus_goals, nexus_project_members, "
            "nexus_projects RESTART IDENTITY CASCADE"
        )
    yield instance
    instance.close()


def test_projects_and_goals_are_scoped_and_audited(store):
    svc = ProjectService(store)
    alice, bob = "user:alice", "user:bob"
    a = svc.create(alice, "Alpha", "Backend")
    b = svc.create(bob, "Beta")
    aid, bid = str(a["id"]), str(b["id"])
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM workflows")
        before = cur.fetchone()["n"]
    goal, created = svc.create_goal(alice, aid, "  Deliver API  ", "req-1")
    assert created and goal["text"] == "Deliver API" and goal["state"] == "draft"
    assert [str(p["id"]) for p in svc.list(alice)] == [aid]
    assert [str(p["id"]) for p in svc.list(bob)] == [bid]
    assert len(svc.list_goals(alice, aid)) == 1
    assert [e["kind"] for e in svc.events(alice, aid)] == ["goal.created", "project.created"]
    for operation in (
        lambda: svc.get(bob, aid), lambda: svc.update(bob, aid, {"name": "stolen"}),
        lambda: svc.list_goals(bob, aid), lambda: svc.create_goal(bob, aid, "stolen"),
        lambda: svc.events(bob, aid),
    ):
        with pytest.raises(NotFound):
            operation()
    assert svc.get(alice, aid)["name"] == "Alpha"
    assert svc.update(alice, aid, {"name": "Alpha 2"})["name"] == "Alpha 2"
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM workflows")
        assert cur.fetchone()["n"] == before  # goals cannot dispatch old workflows


def test_goal_idempotency_is_scoped_and_rejects_changed_body(store):
    svc = ProjectService(store)
    a = str(svc.create("user:alice", "A")["id"])
    b = str(svc.create("user:alice", "B")["id"])
    first, created = svc.create_goal("user:alice", a, "task", "key")
    again, created_again = svc.create_goal("user:alice", a, "task", "key")
    assert created and not created_again and first["id"] == again["id"]
    with pytest.raises(IdempotencyConflict):
        svc.create_goal("user:alice", a, "different", "key")
    other, _ = svc.create_goal("user:alice", b, "different", "key")
    assert other["id"] != first["id"]
    assert len(svc.events("user:alice", a)) == 2


def test_concurrent_goal_retries_create_one_row(store):
    svc = ProjectService(store)
    project_id = str(svc.create("user:alice", "concurrent")["id"])

    def create():
        # Separate PostgreSQL connections, not just the store's in-process lock.
        other = PostgresStore(DSN, auto_migrate=False)
        try:
            return ProjectService(other).create_goal("user:alice", project_id, "same", "same-key")
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: create(), range(8)))
    assert len({str(result[0]["id"]) for result in results}) == 1
    assert sum(result[1] for result in results) == 1
    assert len(svc.list_goals("user:alice", project_id)) == 1


def test_validation(store):
    svc = ProjectService(store)
    for name in (None, "  ", "x" * 121, 42, "bad\x00name"):
        with pytest.raises(ValueError):
            svc.create("user:alice", name)
    project_id = str(svc.create("user:alice", "ok")["id"])
    for bad in (None, " ", "x" * 20001, []):
        with pytest.raises(ValueError):
            svc.create_goal("user:alice", project_id, bad)
    with pytest.raises(ValueError):
        svc.update("user:alice", project_id, {"role": "owner"})


@pytest.fixture
def http_server(store, monkeypatch):
    monkeypatch.setenv("OPENHANDS_API_KEY", "fake")
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("test"),
                           session_secret="test-session-secret-which-is-long-enough")
    alice = app.auth._open_session("alice")
    bob = app.auth._open_session("bob")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", alice, bob
    server.stop()


def request(base, path, method="GET", body=None, *, token=None, session=None, csrf=True, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if session:
        req.add_header("Cookie", "dispatcher_session=" + session.token)
        if csrf:
            req.add_header("X-CSRF-Token", session.csrf_token)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_session_and_bearer_scope_with_csrf(http_server):
    base, alice, bob = http_server
    assert request(base, "/api/v1/projects")[0] == 401
    assert request(base, "/api/v1/projects", "POST", {"name": "x"}, session=alice, csrf=False)[0] == 403
    status, payload = request(base, "/api/v1/projects", "POST", {"name": "A"}, session=alice)
    assert status == 201
    aid = payload["project"]["id"]
    # The existing machine bearer represents the configured owner, not a
    # request-supplied actor; both clients see the same projects.
    assert request(base, "/api/v1/projects", token="machine-token")[1]["projects"][0]["id"] == aid
    assert request(base, f"/api/v1/projects/{aid}", session=bob, headers={"X-User": "alice"})[0] == 404
    assert request(base, f"/api/v1/projects/{aid}", "PATCH", {"name": "stolen"}, session=bob)[0] == 404
    assert request(base, f"/api/v1/projects/{aid}/goals", "POST", {"text": "bad"}, session=bob)[0] == 404
    assert request(base, "/api/v1/projects/not-a-uuid", token="machine-token")[0] == 404
    assert request(base, f"/api/v1/projects/{aid}", "PATCH", {"name": "updated"}, session=alice)[0] == 200
    headers = {"Idempotency-Key": "http-key"}
    path = f"/api/v1/projects/{aid}/goals"
    assert request(base, path, "POST", {"text": "goal"}, session=alice, headers=headers)[0] == 201
    assert request(base, path, "POST", {"text": "goal"}, session=alice, headers=headers)[0] == 200
    assert request(base, path, "POST", {"text": "changed"}, session=alice, headers=headers)[0] == 409
    assert request(base, path, "POST", [], session=alice)[0] == 400
    assert request(base, path, session=bob)[0] == 404
    assert len(request(base, path, session=alice)[1]["goals"]) == 1


def test_nexus_tables_enable_rls_and_do_not_grant_public_access(store):
    with store._tx() as cur:
        cur.execute("SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = "
                    "'public'::regnamespace AND relname = ANY(%s)",
                    (["nexus_projects", "nexus_project_members", "nexus_goals", "nexus_project_events"],))
        rows = cur.fetchall()
        assert len(rows) == 4 and all(row["relrowsecurity"] for row in rows)
        cur.execute("SELECT c.relname FROM pg_class c, LATERAL "
                    "aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
                    "WHERE c.relname = ANY(%s) AND c.relnamespace = 'public'::regnamespace "
                    "AND a.grantee=0", (["nexus_projects", "nexus_project_members", "nexus_goals", "nexus_project_events"],))
        assert cur.fetchall() == []
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
        roles = [row["rolname"] for row in cur.fetchall()]
        for role in roles:
            for table in ("nexus_projects", "nexus_project_members", "nexus_goals", "nexus_project_events"):
                cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS allowed", (role, table))
                assert cur.fetchone()["allowed"] is False


def test_migration_is_repeatable_and_preserves_legacy_rows(store):
    from pathlib import Path

    legacy = store.create_workflow("existing legacy workflow")
    sql = (Path(__file__).resolve().parent.parent / "migrations/0009_nexus_projects.sql").read_text()
    # Run the migration again against a populated schema, as a DBA might do.
    with store._tx() as cur:
        cur.execute(sql)
    with store._tx() as cur:
        cur.execute("SELECT goal FROM workflows WHERE id=%s", (legacy,))
        assert cur.fetchone()["goal"] == "existing legacy workflow"
