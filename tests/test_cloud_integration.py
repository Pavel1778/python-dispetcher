"""Integration tests: reuse of the existing OpenHands Cloud account.

These run against the real local Cloud-shaped HTTP server (``tests/fake_cloud``)
and a real PostgreSQL store, so the full request path is exercised without
consuming live Cloud conversation quota. Only the LLM behind a conversation is
faked.

What is asserted here is the *supported* mechanism only: the start request is
constructed from documented fields, account-level configuration is inherited by
omission (never overwritten), and a client-tool call is serviced from the event
stream with no browser session.
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
from orchestration.workflow_verifier import WorkflowVerifier  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run integration tests"
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


def _runtime(store, client, **kw):
    disp = CloudDispatcher(store, client, worker_id="w1", start_poll_interval=0.01)
    return SrRuntime(store, client, SrTools(store, disp),
                     start_poll_interval=0.01, **kw)


# ------------------------------------------------- request construction
def test_start_request_carries_only_documented_fields(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    body = cloud.cloud.start_bodies[cid]
    # The fields the dispatcher relies on, all documented on the start schema.
    assert body["trigger"] == "openhands_api"
    assert body["initial_message"]["role"] == "user"
    assert body["initial_message"]["run"] is True
    assert body["tags"]["orchestratorsr"] == wf.replace("-", "")
    assert [t["name"] for t in body["client_tools"]][0].startswith("sr_")
    # Account selection fields are omitted by default, so the account's own
    # active profile and MCP/skills are inherited rather than overwritten.
    assert "agent_profile_id" not in body
    assert "plugins" not in body
    assert "agent_settings" not in body
    assert "conversation_settings" not in body


def test_profile_and_plugins_are_sent_when_configured(store, cloud, client):
    plugins = [{"source": "github:acme/plugin", "ref": "main"}]
    rt = _runtime(store, client, agent_profile_id="prof-1", plugins=plugins)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    body = cloud.cloud.start_bodies[cid]
    assert body["agent_profile_id"] == "prof-1"
    assert body["plugins"] == plugins


def test_secrets_are_not_sent_unless_provided(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    assert "secrets" not in cloud.cloud.start_bodies[cid]


# ------------------------------------------------- capability inheritance
def test_capabilities_read_only_snapshot(store, cloud, client):
    caps = client.account_capabilities()
    assert caps["llm_api_key_set"] is True
    assert "playwright" in caps["mcp_servers"]
    assert caps["provider_tokens_set"] == {"github": False}
    assert caps["registered_marketplaces"] == 1
    # No secret value is ever present in the snapshot.
    blob = repr(caps)
    assert "api_key" not in blob.lower() or "llm_api_key_set" in blob


def test_conversation_skills_are_observable(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    names = client.conversation_skills(cid)
    assert "work_hosts" in names


def test_missing_skill_is_reported_not_assumed(store, cloud, client):
    cloud.cloud.skills = []
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    assert client.conversation_skills(cid) == []


# ------------------------------------------------- client-tool servicing
def test_client_tool_call_is_serviced_without_a_browser(store, cloud, client):
    """Sr's tool call is executed from the event stream, no browser involved.

    The test never opens a browser or a websocket: it only calls
    ``SrRuntime.tick()``, which polls ``events/search`` and delivers the result
    with ``send-message``. That is the supported mechanism.
    """
    store.add_repository("acme/repo", allowed_branches=["main"])
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "work", "repository": "acme/repo"}
    )
    report = rt.tick()
    assert report.handled and report.handled[0]["ok"] is True
    # The result reached Sr as a message.
    assert any("sr_create_task result" in m for m in cloud.cloud.messages[cid])
    assert len(store.list_tasks(workflow_id=wf)) == 1


def test_duplicate_event_is_executed_once(store, cloud, client):
    """The same ActionEvent id seen twice must not create two tasks."""
    store.add_repository("acme/repo", allowed_branches=["main"])
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    eid = "evt-fixed-1"
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "once", "repository": "acme/repo"},
        event_id=eid,
    )
    rt.tick()
    # A re-poll returns the same event; it must be ignored.
    cloud.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "once", "repository": "acme/repo"},
        event_id=eid,
    )
    rt.tick()
    assert len(store.list_tasks(workflow_id=wf)) == 1


def test_sr_tool_result_reaches_a_live_looking_sr(store, cloud, client):
    """After the call is delivered, Sr sees the result and can keep planning."""
    store.add_repository("acme/repo", allowed_branches=["main"])
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        cid, "sr_list_child_tasks", {}
    )
    rt.tick()
    last = cloud.cloud.messages[cid][-1]
    assert "sr_list_child_tasks result" in last


# ------------------------------------------------- Sr/Jr relationships
def test_jr_task_keeps_its_own_conversation(store, cloud, client):
    store.add_repository("acme/repo", allowed_branches=["main"])
    disp = CloudDispatcher(store, client, worker_id="w1", start_poll_interval=0.01)
    rt = SrRuntime(store, client, SrTools(store, disp), start_poll_interval=0.01)
    wf = store.create_workflow("wf")
    sr_cid = rt.ensure_sr(wf)
    cloud.cloud.add_client_tool_action(
        sr_cid, "sr_create_task", {"goal": "work", "repository": "acme/repo"}
    )
    rt.tick()
    jr = store.list_tasks(workflow_id=wf)[0]
    disp.tick()
    jr_conv = store.conversation_for(jr.id)
    assert jr_conv is not None
    # Sr and Jr must be different conversations.
    assert jr_conv["external_conversation_id"] != sr_cid
    assert jr_conv["external_conversation_id"] in cloud.cloud.conversations


# ------------------------------------------------- quota and interruption
def test_quota_exhaustion_does_not_kill_the_task(store, cloud, client):
    """A 429 while starting Jr must leave the task retryable, not dead."""
    from orchestration.cloud_client import CloudUnavailable

    store.add_repository("acme/repo", allowed_branches=["main"])
    wf = store.create_workflow("wf")
    task = store.submit_task("work", workflow_id=wf, selected_repository="acme/repo")

    class _Quota(CloudClient):
        def start_conversation(self, **kw):  # type: ignore[override]
            raise CloudUnavailable("429 daily_conversation_limit_reached")

    d = CloudDispatcher(store, _Quota(cloud.base_url, api_key="k"),
                        worker_id="w1", start_poll_interval=0.01)
    d.tick()
    assert store.get_task(task.id).state is not TaskState.DEAD_LETTER


def test_interrupted_sr_does_not_hang_the_workflow(store, cloud, client):
    """An Sr that errored must fail the workflow, not leave it queued forever."""
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    cloud.cloud.conversations[cid]["execution_status"] = "error"
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] == "failed"


def test_sr_that_stops_without_delegating_blocks(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    cloud.cloud.conversations[cid]["execution_status"] = "finished"
    vf = WorkflowVerifier(store, client)
    vf.tick()
    # No child work and Sr has stopped: needs a human, not a false success.
    assert store.sr_for(wf)["state"] == "blocked"


def test_vanished_sr_conversation_blocks(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    del cloud.cloud.conversations[cid]  # conversation gone
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] == "blocked"


def test_sr_awaiting_confirmation_blocks(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    cloud.cloud.conversations[cid]["execution_status"] = "waiting_for_confirmation"
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] == "blocked"


def test_workflow_completes_only_after_sr_stops(store, cloud, client):
    rt = _runtime(store, client)
    wf = store.create_workflow("wf")
    cid = rt.ensure_sr(wf)
    t = store.submit_task("work", workflow_id=wf)
    for st in (TaskState.ASSIGNED, TaskState.RUNNING, TaskState.VERIFYING):
        store.transition(t.id, st)
    store.transition(t.id, TaskState.COMPLETED, result="done")
    cloud.cloud.conversations[cid]["execution_status"] = "running"
    vf = WorkflowVerifier(store, client)
    vf.tick()
    assert store.sr_for(wf)["state"] == "verifying"
    cloud.cloud.conversations[cid]["execution_status"] = "finished"
    vf.tick()
    assert store.sr_for(wf)["state"] == "completed"


# ------------------------------------------------- secret redaction
def test_capabilities_never_leak_secret_values(store, cloud, client):
    """Even if the account reported a token, the snapshot must not echo it."""
    cloud.cloud.settings["provider_tokens_set"] = {"github": "ghp_SUPERSECRET"}
    caps = client.account_capabilities()
    assert "SUPERSECRET" not in repr(caps)
    assert caps["provider_tokens_set"] == {"github": True}
