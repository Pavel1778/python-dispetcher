"""Project isolation at the HTTP route boundary: a non-member gets 404, not 403.

Companion to ``tests/test_project_isolation.py`` (which proves isolation across
the *service* layer for every project-scoped service). This module adds the one
thing a service-layer battery cannot: it drives the real web server over HTTP and
asserts that each project-scoped **route** answers a non-member with **404** -
never 403 (which would confirm the resource exists), never 200.

Both files are kept on purpose. The service-layer file covers more services
(files, executors, accounts, the membership lifecycle); this one covers the
route layer end to end. A route that forgot to scope a lookup passes neither.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectBindingService  # noqa: E402
from orchestration.nexus_context import ContextService, MemoryService  # noqa: E402
from orchestration.nexus_conversations import ConversationService  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

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
def world():
    """Two projects owned by two principals, with resources in each."""
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    lab = LabStore(store)
    conversations = ConversationService(store)
    memory = MemoryService(store)
    context = ContextService(store)
    binding = ProjectBindingService(store)

    alice, bob = "user:alice", "user:bob"
    a = str(svc.create(alice, "Alpha")["id"])
    b = str(svc.create(bob, "Beta")["id"])
    goal = str(svc.create_goal(alice, a, "Alice goal")[0]["id"])
    run = lab.start_run(alice, a, goal, policy="durable")
    task = lab.add_task(alice, a, run["id"], task_key="t1", title="Alice task")
    conv = conversations.create(alice, a, title="Alice chat")
    conversations.add_message(alice, a, conv["id"], role="user", content="hello")
    memory.remember(alice, kind="fact", content="Alice fact",
                    provenance="repository_observed", project_id=a)
    context.refresh(alice, a, repository="owner/repo", revision="abc",
                    summary={"languages": {"python": 3}})

    yield {
        "store": store, "svc": svc, "lab": lab, "conversations": conversations,
        "memory": memory, "context": context, "binding": binding,
        "alice": alice, "bob": bob, "a": a, "b": b,
        "goal": goal, "run": run["id"], "task": task["id"], "conv": conv["id"],
    }
    store.close()


def _expect_not_found(fn, *args, **kwargs):
    with pytest.raises(NotFound):
        fn(*args, **kwargs)


# ------------------------------------------------------------------ the project
def test_a_project_and_all_its_children_are_invisible_to_a_non_member(world):
    a, b, bob = world["a"], world["b"], world["bob"]
    svc = world["svc"]
    _expect_not_found(svc.get, bob, a)
    _expect_not_found(svc.update, bob, a, {"name": "stolen"})
    _expect_not_found(svc.list_goals, bob, a)
    _expect_not_found(svc.events, bob, a)
    _expect_not_found(svc.create_goal, bob, a, "stolen")
    assert [str(p["id"]) for p in svc.list(bob)] == [b]
    assert [str(p["id"]) for p in svc.list(world["alice"])] == [a]


# ----------------------------------------------------------------------- goals
def test_goals_are_project_scoped(world):
    bob, a, goal = world["bob"], world["a"], world["goal"]
    svc = world["svc"]
    _expect_not_found(svc.get_goal, bob, a, goal)
    _expect_not_found(svc.get_goal, bob, world["b"], goal)


# ------------------------------------------------------------------------ runs
def test_runs_and_tasks_are_project_scoped(world):
    bob, a, run, task = world["bob"], world["a"], world["run"], world["task"]
    lab = world["lab"]
    _expect_not_found(lab.get_run, bob, a, run)
    _expect_not_found(lab.list_tasks, bob, a, run)
    _expect_not_found(lab.set_run_state, bob, a, run, "cancelled")
    _expect_not_found(lab.get_task, bob, world["b"], task)


# --------------------------------------------------------------- conversations
def test_conversations_and_messages_are_project_scoped(world):
    bob, a, conv = world["bob"], world["a"], world["conv"]
    conversations = world["conversations"]
    _expect_not_found(conversations.get, bob, a, conv)
    _expect_not_found(conversations.list_messages, bob, a, conv)
    _expect_not_found(conversations.add_message, bob, a, conv, role="user", content="stolen")
    _expect_not_found(conversations.rename, bob, a, conv, title="stolen")
    _expect_not_found(conversations.set_archived, bob, a, conv, True)
    _expect_not_found(conversations.export, bob, a, conv)


# ------------------------------------------------------------ memory and context
def test_memory_and_context_are_project_scoped(world):
    bob, a = world["bob"], world["a"]
    _expect_not_found(world["context"].history, bob, a)
    _expect_not_found(world["context"].current, bob, a)
    _expect_not_found(world["context"].refresh, bob, a, repository="r", revision="x",
                      summary={})
    _expect_not_found(world["memory"].remember, bob, kind="fact", content="stolen",
                      provenance="agent_reported", project_id=a)


# ------------------------------------------------------------------ credentials
def test_a_bound_repository_is_not_readable_across_projects(world):
    bob, a = world["bob"], world["a"]
    binding = world["binding"]
    _expect_not_found(binding.get, bob, a)


# ----------------------------------------------------------------- notifications
def test_notifications_are_project_scoped(world):
    bob, a = world["bob"], world["a"]
    lab = world["lab"]
    _expect_not_found(lab.notifications, bob, a)
    _expect_not_found(lab.mark_all_notifications_read, bob, a)


# ----------------------------------------------------------- the same over HTTP
@pytest.fixture
def http_server(world):
    from orchestration.web_auth import AuthService, hash_password
    from orchestration.web_server import App, WebServer

    os.environ.setdefault("NEXUS_ALLOW_PRIVATE_TARGETS", "1")
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice",
                           password_hash=hash_password("test"),
                           session_secret="test-session-secret-which-is-long-enough")
    alice = app.auth._open_session("alice")
    bob = app.auth._open_session("bob")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", alice, bob
    server.stop()


def _http(base, path, session, method="GET", body=None):
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Cookie", "dispatcher_session=" + session.token)
    req.add_header("X-CSRF-Token", session.csrf_token)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_every_project_scoped_route_answers_404_to_a_non_member(http_server, world):
    """Over real HTTP: a non-member gets 404 (never 403, never 200) from every
    project-scoped read of a resource that exists in another project."""
    base, _alice, bob = http_server
    a = world["a"]
    paths = [
        f"/api/v1/projects/{a}",
        f"/api/v1/projects/{a}/goals",
        f"/api/v1/projects/{a}/runs",
        f"/api/v1/projects/{a}/conversations",
        f"/api/v1/projects/{a}/memory",
        f"/api/v1/projects/{a}/context",
        f"/api/v1/projects/{a}/notifications",
        f"/api/v1/projects/{a}/binding",
        f"/api/v1/projects/{a}/files",
        f"/api/v1/projects/{a}/workspaces",
        f"/api/v1/projects/{a}/voice",
        f"/api/v1/projects/{a}/evidence",
    ]
    for path in paths:
        assert _http(base, path, bob) == 404, f"{path} leaked to a non-member"
    assert _http(base, f"/api/v1/projects/{a}/goals", bob, "POST", {"text": "x"}) == 404
