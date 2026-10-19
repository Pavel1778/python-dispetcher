"""Sr runtime (tool bridge) tests.

These prove the mechanism the report asked about: that Sr can *really* create a
Jr, not merely describe one. They run against a real PostgreSQL database and a
real local HTTP server that speaks the Cloud V1 surface (including the
``client_tools`` field and ``ActionEvent`` emission confirmed on the live API).

The database is taken from ``TEST_SUPABASE_DB_URL`` (falling back to
``SUPABASE_DB_URL``). When neither is set the module is skipped.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.sr_runtime import SrRuntime  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run Sr runtime tests"
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
def runtime(store, client, dispatcher):
    tools = SrTools(store, dispatcher)
    return SrRuntime(store, client, tools, start_poll_interval=0.01)


def _repo(store, name="acme/repo"):
    store.add_repository(name, allowed_branches=["main", "dev"])
    return name


# ------------------------------------------------------------------ ensure_sr
def test_ensure_sr_starts_with_client_tools(store, cloud, runtime):
    wf = store.create_workflow("build a thing")
    cid = runtime.ensure_sr(wf)
    specs = cloud.cloud.client_tools[cid]
    names = {s["name"] for s in specs}
    # The seven Sr tools must actually be attached to the Sr conversation.
    assert names == {
        "sr_create_task", "sr_list_child_tasks", "sr_get_task_status",
        "sr_get_task_report", "sr_request_task_revision", "sr_accept_task_result",
        "sr_cancel_task",
    }
    assert store.sr_for(wf)["sr_conversation_id"] == cid


def test_ensure_sr_is_idempotent(store, cloud, runtime):
    wf = store.create_workflow("wf")
    first = runtime.ensure_sr(wf)
    second = runtime.ensure_sr(wf)
    assert first == second
    # Only one conversation was created.
    assert len(cloud.cloud.conversations) == 1


# --------------------------------------------------- Sr really creates a Jr
def test_sr_tool_call_creates_a_real_jr(store, cloud, runtime):
    repo = _repo(store)
    wf = store.create_workflow("ship feature X")
    cid = runtime.ensure_sr(wf)

    # The Sr agent calls create_task (as the live server would emit it).
    cloud.cloud.add_client_tool_action(
        cid,
        "sr_create_task",
        {"goal": "write the parser", "repository": repo, "branch": "main"},
    )
    report = runtime.tick()
    assert len(report.handled) == 1
    assert report.handled[0]["name"] == "sr_create_task"
    assert report.handled[0]["ok"] is True

    # A real child task now exists, and the Jr dispatcher can pick it up.
    kids = store.list_tasks(workflow_id=wf)
    assert len(kids) == 1
    assert kids[0].goal == "write the parser"
    assert kids[0].selected_repository == repo

    # The result was delivered back into the Sr conversation.
    assert any("sr_create_task" in m for m in cloud.cloud.messages[cid])


def test_tool_call_is_executed_exactly_once_across_polls(store, cloud, runtime):
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "one unit", "repository": repo}
    )
    runtime.tick()
    runtime.tick()
    runtime.tick()
    # Exactly one task, despite three polls seeing the same ActionEvent.
    assert len(store.list_tasks(workflow_id=wf)) == 1
    assert len(store.tool_calls(workflow_id=wf)) == 1


def test_restart_does_not_recreate_sr_or_jr(store, cloud, runtime, client, dispatcher):
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "durable work", "repository": repo}
    )
    runtime.tick()

    # A fresh runtime object, as after a process restart.
    runtime2 = SrRuntime(store, client, SrTools(store, dispatcher),
                         start_poll_interval=0.01)
    assert runtime2.ensure_sr(wf) == cid
    runtime2.tick()
    assert len(store.list_tasks(workflow_id=wf)) == 1
    assert len(cloud.cloud.conversations) == 1


# ---------------------------------------------- bad calls and allowlist
def test_disallowed_repository_is_rejected_without_a_task(store, cloud, runtime):
    store.add_repository("acme/allowed", allowed_branches=["main"])
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "x", "repository": "evil/repo"}
    )
    report = runtime.tick()
    assert report.handled[0]["ok"] is False
    assert store.list_tasks(workflow_id=wf) == []


def test_invalid_arguments_are_reported_not_crashed(store, cloud, runtime):
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(cid, "sr_get_task_status", {"wrong": "arg"})
    report = runtime.tick()
    assert report.handled[0]["ok"] is False


def test_unknown_tool_is_ignored(store, cloud, runtime):
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(cid, "not_a_tool", {"x": 1})
    report = runtime.tick()
    assert report.handled == []


# ---------------------------------------------- full Sr -> Jr -> report cycle
def test_sr_reads_jr_result_after_dispatch(store, cloud, runtime, dispatcher):
    repo = _repo(store)
    wf = store.create_workflow("ship it")
    cid = runtime.ensure_sr(wf)

    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "do the work", "repository": repo}
    )
    runtime.tick()
    jr = store.list_tasks(workflow_id=wf)[0]

    # The Jr dispatcher runs the child task to completion.
    dispatcher.tick()
    jr_conv = store.conversation_for(jr.id)["external_conversation_id"]
    cloud.cloud.add_assistant_message(jr_conv, "Jr done: implemented and tested.")
    cloud.cloud.conversations[jr_conv]["execution_status"] = "finished"
    dispatcher.tick()
    assert store.get_task(jr.id).state is TaskState.COMPLETED

    # Sr asks for the report and receives the real result.
    cloud.cloud.add_client_tool_action(cid, "sr_get_task_report", {"task_id": jr.id})
    runtime.tick()
    delivered = cloud.cloud.messages[cid][-1]
    assert "implemented and tested" in delivered


def test_sr_accepts_jr_result(store, cloud, runtime, dispatcher):
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "work", "repository": repo}
    )
    runtime.tick()
    jr = store.list_tasks(workflow_id=wf)[0]
    dispatcher.tick()
    jr_conv = store.conversation_for(jr.id)["external_conversation_id"]
    cloud.cloud.add_assistant_message(jr_conv, "result text")
    cloud.cloud.conversations[jr_conv]["execution_status"] = "finished"
    dispatcher.tick()

    # Sr accepts the verified task.
    cloud.cloud.add_client_tool_action(
        cid, "sr_accept_task_result", {"task_id": jr.id, "accepted": True}
    )
    runtime.tick()
    assert store.get_task(jr.id).state is TaskState.COMPLETED
