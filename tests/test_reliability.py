"""Reliability tests: the failure paths the audit found.

Each test reproduces a concrete way the dispatcher could lose work, duplicate
work, or claim false completion, and asserts the fix. They run against a real
PostgreSQL database and the real local Cloud-shaped HTTP server; nothing about
the store or the client is mocked.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import (  # noqa: E402
    CloudClient,
    CloudUnavailable,
    ExecutionStatus,
)
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.sr_runtime import SrRuntime  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402
from orchestration.workflow_verifier import WorkflowVerifier  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run reliability tests"
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
            for name in ("0001_init.sql", "0002_sr_runtime.sql",
                         "0003_tool_call_recovery.sql"):
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
def runtime(store, client, dispatcher):
    return SrRuntime(store, client, SrTools(store, dispatcher),
                     start_poll_interval=0.01)


def _repo(store, name="acme/repo"):
    store.add_repository(name, allowed_branches=["main", "dev"])
    return name


# ---------------------------------------------------------- Sr call recovery
def test_claimed_but_unfinished_call_is_reexecuted(store, cloud, runtime):
    """Crash between claim and finish must not lose the call forever.

    The old claim was a one-shot row insert, so a crash after the claim lost the
    Sr call permanently. Now the claim is a lease and a stale claim is retried.
    """
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    eid = cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "recovered work", "repository": repo}
    )
    # Simulate a crash: claim the row but never finish it, and age the claim.
    assert store.claim_tool_call(
        eid, workflow_id=wf, name="sr_create_task",
        args={"goal": "recovered work", "repository": repo},
    )
    with store._tx() as cur:  # noqa: SLF001 - test-only state shaping
        cur.execute(
            "UPDATE sr_tool_calls SET claimed_at = now() - interval '1 hour' "
            "WHERE event_id=%s",
            (eid,),
        )

    report = runtime.tick()
    assert any(h["event_id"] == eid and h["ok"] for h in report.handled)
    kids = store.list_tasks(workflow_id=wf)
    assert len(kids) == 1 and kids[0].goal == "recovered work"


def test_finished_but_undelivered_result_is_redelivered(store, cloud, runtime):
    """A crash between finish and send-message must not strand the result."""
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    eid = cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "work", "repository": repo}
    )
    assert store.claim_tool_call(
        eid, workflow_id=wf, name="sr_create_task", args={"goal": "work"}
    )
    store.finish_tool_call(eid, result={"ok": True, "task_id": "t1"}, is_error=False)
    before = len(cloud.cloud.messages[cid])

    runtime.tick()  # redelivery pass must resend it
    assert len(cloud.cloud.messages[cid]) == before + 1
    assert store.tool_call(eid)["delivered_at"] is not None
    # Idempotent: a second tick does not deliver again.
    runtime.tick()
    assert len(cloud.cloud.messages[cid]) == before + 1


def test_reexecuted_create_task_does_not_duplicate(store, cloud, runtime):
    """The idempotency key makes re-execution safe.

    Recovery re-runs a call whose claim was stale. Without the key that would
    create a second child task for the same Sr request.
    """
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    eid = cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "one task", "repository": repo}
    )
    assert store.claim_tool_call(
        eid, workflow_id=wf, name="sr_create_task", args={"goal": "one task"}
    )
    with store._tx() as cur:  # noqa: SLF001
        cur.execute(
            "UPDATE sr_tool_calls SET claimed_at = now() - interval '1 hour' "
            "WHERE event_id=%s",
            (eid,),
        )
    runtime.tick()  # re-executes with idempotency_key = sr:<event_id>
    assert len(store.list_tasks(workflow_id=wf)) == 1


# ------------------------------------------------------ Jr orphan recovery
def test_orphan_sr_conversation_is_reused(store, cloud, runtime):
    """A crash after starting Sr, before recording it, must not start a second."""
    wf = store.create_workflow("wf")
    value = wf.replace("-", "")
    cloud.cloud.make_conversation(title="orphan sr", tags={"orchestratorsr": value})
    cid = runtime.ensure_sr(wf)
    assert len(cloud.cloud.conversations) == 1
    assert store.sr_for(wf)["sr_conversation_id"] == cid


def test_orphan_conversation_is_reused_not_duplicated(store, cloud, dispatcher):
    """Crash after start_conversation, before the row was written.

    The conversation exists on Cloud but locally we only have the start task id
    (or nothing). The tag lets the dispatcher find and reuse it.
    """
    repo = _repo(store)
    wf = store.create_workflow("wf")
    task = store.submit_task("do work", workflow_id=wf,
                             selected_repository=repo)
    # A conversation exists on Cloud, tagged for this task, with no local row.
    tag = ("orchestratortaskid", task.id.replace("-", ""))
    cloud.cloud.make_conversation(title="orphan", repo=repo, tags={tag[0]: tag[1]})

    report = dispatcher.tick()
    assert any(a.get("recovered") for a in report.started)
    assert len(cloud.cloud.conversations) == 1
    assert store.conversation_for(task.id)["external_conversation_id"] is not None


# ------------------------------------------------------------ 429 handling
def test_rate_limit_is_transient_not_dead_letter(store, dispatcher):
    """429 (quota) must not burn an attempt or dead-letter the task."""
    wf = store.create_workflow("wf")
    task = store.submit_task("do work", workflow_id=wf)

    class _Limited(CloudClient):
        def search_conversations(self, **kw):  # type: ignore[override]
            raise CloudUnavailable("429")

    d = CloudDispatcher(store, _Limited("http://x", api_key="k"), worker_id="w1",
                        start_poll_interval=0.01)
    report = d.tick()
    assert store.get_task(task.id).state is not TaskState.DEAD_LETTER
    assert task.id in report.reclaimed or store.get_task(task.id).state in (
        TaskState.QUEUED, TaskState.ASSIGNED
    )


# ---------------------------------------------------------- event pagination
def test_action_event_beyond_first_page_is_handled(store, cloud, runtime):
    """A tool call older than the first 100 events must still be seen."""
    repo = _repo(store)
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    # Fill the conversation past one page with noise, then add the real call.
    for i in range(120):
        cloud.cloud.events[cid].append(
            {"kind": "MessageEvent",
             "llm_message": {"role": "assistant",
                             "content": [{"type": "text", "text": f"n{i}"}]}}
        )
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "late call", "repository": repo}
    )
    report = runtime.tick()
    assert any(h["ok"] for h in report.handled)
    assert len(store.list_tasks(workflow_id=wf)) == 1


# ------------------------------------------------------------ workflow scope
def test_task_tool_rejects_other_workflows_task(store, cloud, runtime):
    """A Sr conversation cannot touch a task from a different workflow."""
    repo = _repo(store)
    wf_a = store.create_workflow("a")
    wf_b = store.create_workflow("b")
    other = store.submit_task("other work", workflow_id=wf_b,
                              selected_repository=repo)
    cid = runtime.ensure_sr(wf_a)
    cloud.cloud.add_client_tool_action(
        cid, "sr_get_task_status", {"task_id": other.id}
    )
    report = runtime.tick()
    assert report.handled[0]["ok"] is False
    assert store.get_task(other.id).state is TaskState.QUEUED


def test_child_task_limit_is_enforced(store, cloud, runtime):
    repo = _repo(store)
    wf = store.create_workflow("wf")
    tools = SrTools(store, runtime.client)
    tools.MAX_CHILD_TASKS = 2
    assert tools.create_task("a", workflow_id=wf, repository=repo).ok
    assert tools.create_task("b", workflow_id=wf, repository=repo).ok
    over = tools.create_task("c", workflow_id=wf, repository=repo)
    assert over.ok is False
    assert "limit" in over.error


# --------------------------------------------------------- workflow verifier
def _finish_all(store, wf, n=1):
    ids = []
    for i in range(n):
        t = store.submit_task(f"work {i}", workflow_id=wf)
        store.transition(t.id, TaskState.ASSIGNED)
        store.transition(t.id, TaskState.RUNNING)
        store.transition(t.id, TaskState.VERIFYING)
        store.transition(t.id, TaskState.COMPLETED, result="done")
        ids.append(t.id)
    return ids


def test_verifier_does_not_complete_while_work_is_active(store, cloud, client):
    wf = store.create_workflow("wf")
    store.submit_task("pending", workflow_id=wf)  # stays queued
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] != "completed"


def test_verifier_requires_sr_to_stop_before_completing(store, cloud, client, runtime):
    wf = store.create_workflow("wf")
    cid = runtime.ensure_sr(wf)
    _finish_all(store, wf)
    cloud.cloud.conversations[cid]["execution_status"] = ExecutionStatus.RUNNING.value
    vf = WorkflowVerifier(store, client)
    vf.tick()
    # Child work is done, but Sr is still running: not complete yet.
    assert store.sr_for(wf)["state"] == "verifying"
    cloud.cloud.conversations[cid]["execution_status"] = ExecutionStatus.FINISHED.value
    vf.tick()
    assert store.sr_for(wf)["state"] == "completed"


def test_verifier_blocks_on_failed_child(store, cloud, client):
    wf = store.create_workflow("wf")
    t = store.submit_task("work", workflow_id=wf)
    store.transition(t.id, TaskState.ASSIGNED)
    store.transition(t.id, TaskState.RUNNING)
    store.transition(t.id, TaskState.FAILED)
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] == "failed"


def test_verifier_waits_on_pending_tool_call(store, cloud, client):
    """A finished-but-undelivered call must hold the workflow open."""
    wf = store.create_workflow("wf")
    _finish_all(store, wf)
    store.claim_tool_call("e1", workflow_id=wf, name="sr_create_task", args={})
    store.finish_tool_call("e1", result={"ok": True})  # not delivered
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] != "completed"
