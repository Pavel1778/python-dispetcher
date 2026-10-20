"""Cloud dispatcher and Sr-tool tests.

These run against a real PostgreSQL database and a real local HTTP server that
speaks the Cloud V1 surface. Nothing is mocked: the store issues real SQL, the
client makes real HTTP requests.

The database is taken from ``TEST_SUPABASE_DB_URL`` (falling back to
``SUPABASE_DB_URL``). When neither is set the module is skipped, so the suite
still runs on a machine without PostgreSQL.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import (  # noqa: E402
    PostgresStore,
    TaskState,
)
from orchestration.sr_tools import SrTools  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run Cloud dispatcher tests"
)

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations",
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sr_tool_calls, task_events, conversations, "
                "conversation_routes, cloud_sandboxes, "
                "tasks, repositories, workflows CASCADE"
            )
            # Apply every migration, in order. Listing them by hand here meant a
            # new migration could be added without this suite exercising it.
            for name in sorted(os.listdir(MIGRATIONS_DIR)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(MIGRATIONS_DIR, name)).read())


@pytest.fixture(autouse=True)
def _schema():
    # Reset per test: claim_task picks the oldest ready task, so leftover rows
    # from another test would be claimed instead of this test's task.
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


def _repo(store, name="acme/repo", branches=("main", "dev")):
    store.add_repository(name, allowed_branches=list(branches))
    return name


# ------------------------------------------------------------ happy path
def test_queued_task_becomes_running_conversation(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("say hello", workflow_id=wf)
    report = dispatcher.tick()

    task = store.get_task(t.id)
    assert task.state is TaskState.RUNNING
    conv = store.conversation_for(t.id)
    assert conv["external_conversation_id"] in cloud.cloud.conversations
    assert t.id in report.started[0]["task"]


def test_finished_with_answer_completes(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf)
    dispatcher.tick()
    conv = store.conversation_for(t.id)["external_conversation_id"]
    cloud.cloud.conversations[conv]["execution_status"] = "finished"
    cloud.cloud.add_assistant_message(conv, "the verified answer")

    report = dispatcher.tick()
    task = store.get_task(t.id)
    assert task.state is TaskState.COMPLETED
    assert task.result == "the verified answer"
    assert t.id in report.completed


def test_finished_without_answer_blocks_not_completes(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf)
    dispatcher.tick()
    conv = store.conversation_for(t.id)["external_conversation_id"]
    cloud.cloud.conversations[conv]["execution_status"] = "finished"
    # no assistant message added

    dispatcher.tick()
    task = store.get_task(t.id)
    assert task.state is TaskState.BLOCKED
    assert task.result is None


def test_runtime_error_fails_then_dead_letters(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf, max_attempts=2)
    dispatcher.tick()
    conv = store.conversation_for(t.id)["external_conversation_id"]
    cloud.cloud.conversations[conv]["execution_status"] = "error"

    dispatcher.tick()  # attempt 1 -> FAILED -> QUEUED (retry budget remains)
    assert store.get_task(t.id).state is TaskState.QUEUED
    assert store.get_task(t.id).attempt == 1

    # Re-dispatch and fail again: the budget is now exhausted.
    dispatcher.tick()
    conv = store.conversation_for(t.id)["external_conversation_id"]
    cloud.cloud.conversations[conv]["execution_status"] = "error"
    dispatcher.tick()
    assert store.get_task(t.id).state is TaskState.DEAD_LETTER


# --------------------------------------------------------- idempotency
def test_restart_does_not_create_second_conversation(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf)
    dispatcher.tick()
    first = store.conversation_for(t.id)["external_conversation_id"]

    # A second dispatcher (a "restart") must reuse the recorded conversation.
    d2 = CloudDispatcher(store, dispatcher.client, worker_id="w2",
                         start_poll_interval=0.01)
    d2.tick()
    assert store.conversation_for(t.id)["external_conversation_id"] == first
    assert len(cloud.cloud.conversations) == 1


def test_double_submit_with_same_idempotency_key(store):
    wf = store.create_workflow("wf")
    a = store.submit_task("g", workflow_id=wf, idempotency_key="same")
    b = store.submit_task("g", workflow_id=wf, idempotency_key="same")
    assert a.id == b.id


# ------------------------------------------------------------ allowlist
def test_repository_outside_allowlist_is_rejected(store, dispatcher, tools):
    wf = store.create_workflow("wf")
    res = tools.create_task("x", workflow_id=wf, repository="evil/repo")
    assert res.ok is False
    assert "not allowed" in res.error


def test_branch_outside_allowlist_is_rejected(store, dispatcher, tools):
    wf = store.create_workflow("wf")
    _repo(store)
    res = tools.create_task("x", workflow_id=wf, repository="acme/repo",
                            branch="not-listed")
    assert res.ok is False


def test_allowed_repository_is_dispatched(store, cloud, dispatcher, tools):
    wf = store.create_workflow("wf")
    _repo(store)
    res = tools.create_task("x", workflow_id=wf, repository="acme/repo",
                            branch="dev")
    assert res.ok
    dispatcher.tick()
    task = store.get_task(res.data["task_id"])
    assert task.state is TaskState.RUNNING
    assert task.selected_repository == "acme/repo"


def test_task_for_unlisted_repo_already_queued_is_dead_lettered(store, cloud, dispatcher):
    """Defence in depth: even a row inserted without the tool is checked."""
    wf = store.create_workflow("wf")
    t = store.submit_task("x", workflow_id=wf, selected_repository="evil/repo")
    dispatcher.tick()
    assert store.get_task(t.id).state is TaskState.DEAD_LETTER


# ------------------------------------------------------------ Sr tools
def test_sr_tools_full_cycle(store, cloud, dispatcher, tools):
    wf = store.create_workflow("wf")
    res = tools.create_task("child work", workflow_id=wf)
    tid = res.data["task_id"]

    status = tools.get_task_status(tid, workflow_id=wf)
    assert status.ok and status.data["state"] == "queued"

    kids = tools.list_child_tasks(workflow_id=wf)
    assert len(kids.data["tasks"]) == 1

    dispatcher.tick()
    assert tools.get_task_status(tid, workflow_id=wf).data["state"] == "running"

    conv = store.conversation_for(tid)["external_conversation_id"]
    cloud.cloud.conversations[conv]["execution_status"] = "finished"
    cloud.cloud.add_assistant_message(conv, "result text")
    dispatcher.tick()

    report = tools.get_task_report(tid, workflow_id=wf)
    assert report.data["state"] == "completed"
    assert report.data["result"] == "result text"

    acc = tools.accept_task_result(tid, accepted=True, note="looks good", workflow_id=wf)
    assert acc.ok


def test_request_revision_delivers_and_requeues(store, cloud, dispatcher, tools):
    wf = store.create_workflow("wf")
    tid = tools.create_task("child work", workflow_id=wf).data["task_id"]
    dispatcher.tick()
    conv = store.conversation_for(tid)["external_conversation_id"]

    res = tools.request_task_revision(tid, "please add tests", workflow_id=wf)
    assert res.ok
    assert store.get_task(tid).state is TaskState.QUEUED
    assert cloud.cloud.messages[conv] == ["please add tests"]


def test_cancel_task(store, dispatcher, tools):
    wf = store.create_workflow("wf")
    tid = tools.create_task("child work", workflow_id=wf).data["task_id"]
    assert tools.cancel_task(tid, workflow_id=wf).ok
    assert store.get_task(tid).state is TaskState.CANCELLED


# ------------------------------------------------------------ recovery
def test_expired_lease_is_recovered_then_redispatched(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf, max_attempts=3)
    # Simulate a crashed dispatcher: claim with an already-expired lease.
    store.claim_task("dead-worker", lease_seconds=-1)
    assert store.get_task(t.id).state is TaskState.ASSIGNED

    report = dispatcher.tick()
    # It was reclaimed, then re-dispatched to a live conversation.
    assert any(a["task"] == t.id for a in report.reclaimed)
    assert store.get_task(t.id).state is TaskState.RUNNING


def test_recovery_dead_letters_when_attempts_exhausted(store, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf, max_attempts=1)
    store.claim_task("dead-worker", lease_seconds=-1)
    store.transition(t.id, TaskState.FAILED)  # attempt -> 1
    # force it back to an expired assigned state to test the limit
    with store._tx() as cur:  # noqa: SLF001 - test-only state shaping
        cur.execute(
            "UPDATE tasks SET state='assigned', owner='dead', "
            "lease_expires=now() - interval '1 hour', attempt=1 WHERE id=%s",
            (t.id,),
        )
    dispatcher.tick()
    assert store.get_task(t.id).state is TaskState.DEAD_LETTER


def test_auth_failure_dead_letters_with_evidence(store, dispatcher):
    wf = store.create_workflow("wf")
    t = store.submit_task("do work", workflow_id=wf)

    class _NoKey(CloudClient):
        # A bad key fails every call, including the orphan-search that now runs
        # before a start.
        def start_conversation(self, **kw):  # type: ignore[override]
            from orchestration.cloud_client import CloudAuthError
            raise CloudAuthError("401")

        def search_conversations(self, **kw):  # type: ignore[override]
            from orchestration.cloud_client import CloudAuthError
            raise CloudAuthError("401")

    d = CloudDispatcher(store, _NoKey("http://x", api_key="k"), worker_id="w1",
                        start_poll_interval=0.01)
    report = d.tick()
    assert store.get_task(t.id).state is TaskState.DEAD_LETTER
    assert t.id in report.dead_letter
    assert any("auth" in str(e) for e in store.get_task(t.id).evidence)
