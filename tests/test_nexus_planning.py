"""DAG validation and durable, project-scoped planning contracts."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestration.cloud_store import NotFound, PostgresStore
from orchestration.nexus_planning import PlanService, validate_plan
from orchestration.nexus_projects import IdempotencyConflict, ProjectService
from tests.test_nexus_projects import request

pytest_plugins = ["tests.test_nexus_projects"]

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable database")


def example():
    return {"tasks": [
        {"id": "audit", "title": "Audit", "acceptance": ["Document findings"]},
        {"id": "backend", "title": "Backend", "acceptance": ["Tests pass"], "dependencies": ["audit"]},
        {"id": "frontend", "title": "Frontend", "acceptance": ["Build passes"], "dependencies": ["audit"]},
        {"id": "qa", "title": "QA", "acceptance": ["Review results"], "dependencies": ["backend", "frontend"]},
    ]}


def test_validate_valid_dag_and_defaults():
    tasks = validate_plan(example())
    assert [t["id"] for t in tasks] == ["audit", "backend", "frontend", "qa"]
    assert tasks[0]["max_attempts"] == 3
    assert tasks[-1]["dependencies"] == ["backend", "frontend"]


@pytest.mark.parametrize("payload", [
    {}, {"tasks": []}, {"tasks": "bad"}, {"tasks": [], "action": "execute"},
    {"tasks": [{"id": "x", "title": "x", "acceptance": []}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"], "max_attempts": True}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"], "max_attempts": 11}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"], "dependencies": ["x"]}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"], "dependencies": ["missing"]}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"]},
               {"id": "x", "title": "x", "acceptance": ["a"]}]},
    {"tasks": [{"id": "a", "title": "a", "acceptance": ["a"], "dependencies": ["b"]},
               {"id": "b", "title": "b", "acceptance": ["b"], "dependencies": ["a"]}]},
    {"tasks": [{"id": "x", "title": "x", "acceptance": ["a"], "action": "merge"}]},
])
def test_invalid_plans_fail_closed(payload):
    with pytest.raises(ValueError):
        validate_plan(payload)


def make_goal(store, actor="user:alice", name="A"):
    projects = ProjectService(store)
    pid = str(projects.create(actor, name)["id"])
    goal, _ = projects.create_goal(actor, pid, "Goal")
    return pid, str(goal["id"])


@needs_db
def test_plans_are_versioned_idempotent_scoped_and_not_executed(store):
    pid, gid = make_goal(store)
    other, other_goal = make_goal(store, "user:bob", "B")
    svc = PlanService(store)
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks")
        before = cur.fetchone()["n"]
    plan, created = svc.create("user:alice", pid, gid, example(), "request")
    assert created and plan["version"] == 1 and plan["state"] == "draft"
    assert plan["tasks"][-1]["dependencies"] == ["backend", "frontend"]
    again, created = svc.create("user:alice", pid, gid, example(), "request")
    assert not created and again["id"] == plan["id"]
    changed = example()
    changed["tasks"][0]["title"] = "changed"
    with pytest.raises(IdempotencyConflict):
        svc.create("user:alice", pid, gid, changed, "request")
    with pytest.raises(NotFound):
        svc.create("user:alice", pid, other_goal, example())
    with pytest.raises(NotFound):
        svc.get("user:bob", other, str(plan["id"]))
    with pytest.raises(NotFound):
        svc.list("user:bob", pid, gid)
    with pytest.raises(NotFound):
        svc.approve("user:bob", pid, str(plan["id"]))
    approved = svc.approve("user:alice", pid, str(plan["id"]))
    assert approved["state"] == "approved"
    assert svc.approve("user:alice", pid, str(plan["id"]))["state"] == "approved"
    second, _ = svc.create("user:alice", pid, gid, changed)
    assert second["version"] == 2
    svc.approve("user:alice", pid, str(second["id"]))
    assert svc.get("user:alice", pid, str(plan["id"]))["state"] == "superseded"
    with pytest.raises(ValueError):
        svc.approve("user:alice", pid, str(plan["id"]))
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks")
        assert cur.fetchone()["n"] == before


@needs_db
def test_owner_only_approval_and_db_scope_constraints(store):
    pid, gid = make_goal(store)
    svc = PlanService(store)
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_members(project_id,principal,role) VALUES (%s,'user:bob','member')", (pid,))
    plan, _ = svc.create("user:bob", pid, gid, example())
    assert svc.get("user:bob", pid, str(plan["id"]))["state"] == "draft"
    with pytest.raises(NotFound):
        svc.approve("user:bob", pid, str(plan["id"]))
    other, _ = make_goal(store, name="other")
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_plans(project_id,goal_id,version,created_by,payload_hash) "
                        "VALUES (%s,%s,1,'x','x')", (other, gid))
    assert svc.approve("user:alice", pid, str(plan["id"]))["state"] == "approved"


@needs_db
def test_concurrent_plan_retries_and_versions(store):
    pid, gid = make_goal(store)

    def create(key):
        other = PostgresStore(DSN, auto_migrate=False)
        try:
            return PlanService(other).create("user:alice", pid, gid, example(), key)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(create, ["same"] * 4 + ["a", "b", "c", "d"]))
    assert len({str(x[0]["id"]) for x in results[:4]}) == 1
    assert sum(x[1] for x in results[:4]) == 1
    assert sorted(p["version"] for p in PlanService(store).list("user:alice", pid, gid)) == [1, 2, 3, 4, 5]


@needs_db
def test_http_plan_api_is_scoped_and_csrf_protected(http_server):
    base, alice, bob = http_server
    pid = request(base, "/api/v1/projects", "POST", {"name": "A"}, session=alice)[1]["project"]["id"]
    goals = f"/api/v1/projects/{pid}/goals"
    gid = request(base, goals, "POST", {"text": "goal"}, session=alice)[1]["goal"]["id"]
    path = f"{goals}/{gid}/plans"
    assert request(base, path, "POST", example(), session=alice, csrf=False)[0] == 403
    assert request(base, path, "POST", example(), session=bob)[0] == 404
    status, body = request(base, path, "POST", example(), session=alice, headers={"Idempotency-Key": "abc"})
    assert status == 201
    plan_id = body["plan"]["id"]
    assert request(base, path, "POST", example(), session=alice, headers={"Idempotency-Key": "abc"})[0] == 200
    assert request(base, path, session=alice)[1]["plans"][0]["id"] == plan_id
    plan_path = f"/api/v1/projects/{pid}/plans/{plan_id}"
    assert request(base, plan_path, session=bob)[0] == 404
    assert request(base, plan_path, session=alice)[1]["plan"]["tasks"][0]["id"] == "audit"
    assert request(base, plan_path + "/approve", "POST", {"action": "merge"}, session=alice)[0] == 400
    assert request(base, plan_path + "/approve", "POST", {}, session=alice)[1]["plan"]["state"] == "approved"
    assert request(base, goals + "/not-a-uuid/plans", session=alice)[0] == 404


@needs_db
def test_invalid_plan_leaves_no_rows_or_events(store):
    pid, gid = make_goal(store)
    service = PlanService(store)
    before = ProjectService(store).events("user:alice", pid)
    invalid = example()
    invalid["tasks"][0]["dependencies"] = ["qa"]
    with pytest.raises(ValueError, match="cycle"):
        service.create("user:alice", pid, gid, invalid)
    assert service.list("user:alice", pid, gid) == []
    assert ProjectService(store).events("user:alice", pid) == before


@needs_db
def test_plan_tables_have_rls_without_public_access(store):
    tables = ["nexus_plans", "nexus_plan_tasks", "nexus_plan_dependencies"]
    with store._tx() as cur:
        cur.execute("SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = "
                    "'public'::regnamespace AND relname = ANY(%s)", (tables,))
        rows = cur.fetchall()
        assert len(rows) == 3 and all(row["relrowsecurity"] for row in rows)
        cur.execute("SELECT c.relname FROM pg_class c, LATERAL "
                    "aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
                    "WHERE c.relname = ANY(%s) AND c.relnamespace = 'public'::regnamespace "
                    "AND a.grantee=0", (tables,))
        assert cur.fetchall() == []


@needs_db
def test_concurrent_approvals_leave_only_one_approved_plan(store):
    pid, gid = make_goal(store)
    service = PlanService(store)
    first, _ = service.create("user:alice", pid, gid, example())
    second, _ = service.create("user:alice", pid, gid, example())

    def approve(plan_id):
        other = PostgresStore(DSN, auto_migrate=False)
        try:
            return PlanService(other).approve("user:alice", pid, str(plan_id))
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(approve, [first["id"], second["id"]]))
    states = [plan["state"] for plan in service.list("user:alice", pid, gid)]
    assert sorted(states) == ["approved", "superseded"]


@needs_db
def test_plan_body_size_limit(http_server):
    import http.client
    from urllib.parse import urlparse

    base, _, _ = http_server
    url = urlparse(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    # Advertise an oversized body without actually sending megabytes; the
    # server must reject it before trying to read or parse it.
    conn.request("POST", "/api/v1/projects", body=b"{}", headers={
        "Authorization": "Bearer machine-token", "Content-Length": str(4 * 1024 * 1024 + 1),
    })
    try:
        # Too much to accept is 413, as it already was for file uploads; the
        # answer names the size sent and the size this route takes.
        response = conn.getresponse()
        assert response.status == 413
        assert str(4 * 1024 * 1024) in response.read().decode()
    finally:
        conn.close()
