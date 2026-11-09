"""Dispatch authorization end to end, through the real dispatcher.

These tests drive the actual :class:`CloudDispatcher` (against the fake Cloud
server over a socket) and assert that a project-scoped task is only dispatched
when its repository is linked to its project, enabled, and on an allowed branch.
A task that fails the check is dead-lettered with the reason as evidence — never
dispatched, never reported as success.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run dispatch-auth tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def store():
    s = PostgresStore(DSN)
    yield s
    s.close()


@pytest.fixture
def cloud():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def client(cloud):
    return CloudClient(cloud.base_url, api_key="test-key", timeout=5.0)


@pytest.fixture
def dispatcher(store, client):
    return CloudDispatcher(store, client, worker_id="w1", start_poll_interval=0.01)


@pytest.fixture
def tools(store, dispatcher):
    return SrTools(store, dispatcher)


def _project(store, name="P") -> str:
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_projects(name) VALUES (%s) RETURNING id", (name,))
        pid = str(cur.fetchone()["id"])
        cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                    "VALUES (%s,'user:owner','owner')", (pid,))
    return pid


def _link(store, project_id, full_name):
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name=%s",
                    (project_id, full_name))


def test_linked_repository_is_dispatched(store, cloud, dispatcher):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store)
    _link(store, pid, "acme/repo")
    wid = store.create_workflow("goal", project_id=pid)
    task = store.submit_task("work", workflow_id=wid, project_id=pid,
                             selected_repository="acme/repo", selected_branch="main")
    dispatcher.tick()
    assert store.get_task(task.id).state is TaskState.RUNNING


def test_sr_child_task_inherits_project_and_is_authorized(store, cloud, dispatcher, tools):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store)
    _link(store, pid, "acme/repo")
    wid = store.create_workflow("goal", project_id=pid)
    res = tools.create_task("child", workflow_id=wid, repository="acme/repo",
                            branch="main")
    assert res.ok, res.error
    task = store.get_task(res.data["task_id"])
    assert task.project_id == pid
    dispatcher.tick()
    assert store.get_task(task.id).state is TaskState.RUNNING


def test_sr_child_task_with_unlinked_repository_is_refused(store, cloud, dispatcher, tools):
    store.add_repository("acme/repo", allowed_branches=["main"])
    store.add_repository("acme/other", allowed_branches=["main"])
    pid = _project(store)
    _link(store, pid, "acme/repo")
    wid = store.create_workflow("goal", project_id=pid)
    res = tools.create_task("child", workflow_id=wid, repository="acme/other",
                            branch="main")
    assert res.ok is False
    assert "not linked" in res.error


def test_unlinked_repository_task_already_queued_is_dead_lettered(store, cloud, dispatcher):
    """Defence in depth: a row inserted without the tool is still checked.

    A legacy (project-less) task cannot reach a project repository, but a task
    whose project lost the link (or a direct insert) must not be dispatched.
    """
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store)
    _link(store, pid, "acme/repo")
    wid = store.create_workflow("goal", project_id=pid)
    task = store.submit_task("work", workflow_id=wid, project_id=pid,
                             selected_repository="acme/repo", selected_branch="main")
    # Remove the link while the task is still queued (no in-flight task yet).
    with store._tx() as cur:
        cur.execute("DELETE FROM nexus_project_repositories WHERE project_id=%s", (pid,))
    dispatcher.tick()
    assert store.get_task(task.id).state is TaskState.DEAD_LETTER
    evidence = store.get_task(task.id).evidence
    assert any("not linked" in (e.get("detail") or "") or
               "not linked" in (e.get("reason") or "") for e in evidence)


def test_disabled_repository_is_dead_lettered(store, cloud, dispatcher):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store)
    _link(store, pid, "acme/repo")
    wid = store.create_workflow("goal", project_id=pid)
    task = store.submit_task("work", workflow_id=wid, project_id=pid,
                             selected_repository="acme/repo", selected_branch="main")
    with store._tx() as cur:
        cur.execute("UPDATE repositories SET enabled=false WHERE full_name='acme/repo'")
    dispatcher.tick()
    assert store.get_task(task.id).state is TaskState.DEAD_LETTER


def test_legacy_task_without_project_still_dispatches(store, cloud, dispatcher):
    """Backward compatibility: a legacy task (no project) is unaffected."""
    store.add_repository("acme/repo", allowed_branches=["main"])
    wid = store.create_workflow("goal")
    task = store.submit_task("work", workflow_id=wid,
                             selected_repository="acme/repo", selected_branch="main")
    dispatcher.tick()
    assert store.get_task(task.id).state is TaskState.RUNNING
