"""Sr lifecycle: no duplicate Sr, no silent hang, visible state.

Three defects, each reproduced here against a real PostgreSQL and the fake
Cloud over real sockets:

1. **A second Sr for one workflow.** ``SrRuntime.ensure_sr`` treats a failed
   orphan lookup as "no orphan". ``_find_orphan_sr`` swallows *every*
   ``CloudError``, including a transient ``CloudUnavailable``, so a search
   outage during a retry makes the dispatcher start a brand-new Sr even though
   a real, tagged conversation already exists for that workflow. ``workflows``
   had no idempotency for the start task either: ``sr_start_task_id`` was
   written but never read back, so a crash after ``start_conversation`` could
   not be resumed the way the Jr path resumes ``external_start_task_id``.

2. **A workflow stuck 'waiting' forever.** The verifier only blocked a
   workflow with no children when Sr had *stopped*. An Sr conversation that
   exists but whose status the Cloud never advances past ``idle``/``running``
   (a wedged sandbox, a start that never becomes READY) left the workflow
   non-terminal forever with no operator signal.

3. **State that lies.** ``start_workflow`` starts Sr inline but leaves
   ``workflows.state='queued'``, so the UI shows a queued task while an Sr is
   already running for it.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_server import App  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run Sr lifecycle tests"
)

_MIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sessions, login_attempts, sr_tool_calls, "
                "task_events, conversations, tasks, repositories, workflows CASCADE"
            )
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    a = App(DSN, poll_interval=0.2, token="test-token")
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    a.dispatcher.client = a.client
    a.runtime.client = a.client
    a.verifier.client = a.client
    a.runtime.sr_start_backoff_base = 0.0
    a.runtime.start_poll_interval = 0.0
    yield a
    a.store.close()


# ---------------------------------------------------------------------------
# 1. A search outage during a retry must not start a second Sr.
# ---------------------------------------------------------------------------
def test_search_outage_does_not_start_a_second_sr(app, fake):
    """The recorded start task is resumed instead of minting a new Sr.

    A crash between ``start_conversation`` and ``bind_sr`` leaves a real, tagged
    Sr conversation with no local conversation id, but with ``sr_start_task_id``
    recorded. The retry must resume that start task. Before the fix the retry
    called ``_find_orphan_sr``; when the Cloud search was unavailable it got
    ``None`` and started a second conversation for the same workflow.
    """
    wf = app.store.create_workflow_once("count 17 x 24")[0]

    # The Sr conversation the crashed attempt really created, tagged so it can
    # be found again, plus the start task that produced it.
    cid = fake.cloud.make_conversation(
        title="Sr.count", tags={"orchestratorsr": wf.replace("-", "")}
    )
    st_id = "st-orphan-1"
    fake.cloud.start_tasks[st_id] = {
        "id": st_id, "status": "READY", "app_conversation_id": cid,
        "_cid": cid, "sandbox_id": "sbx", "agent_server_url": None,
        "detail": None, "_polls": 0,
    }
    # What the crashed attempt managed to record: the start task, no conversation.
    app.store.bind_sr(wf, start_task_id=st_id, status="starting")

    # The Cloud search is down while the retry runs.
    fake.cloud.search_status = 503

    report = app.runtime.tick()

    assert [r["workflow"] for r in report.started] == [wf]
    # Exactly one Sr conversation exists, and it is the one already created.
    assert len(fake.cloud.conversations) == 1
    assert app.store.sr_for(wf)["sr_conversation_id"] == cid


def test_orphan_search_failure_is_not_treated_as_absence(app, fake):
    """A transient search failure must not be read as 'no orphan exists'."""
    wf = app.store.create_workflow_once("g")[0]
    fake.cloud.search_status = 503
    from orchestration.cloud_client import CloudUnavailable

    with pytest.raises(CloudUnavailable):
        app.runtime._find_orphan_sr(wf)


# ---------------------------------------------------------------------------
# 2. A wedged Sr must not leave the workflow waiting forever.
# ---------------------------------------------------------------------------
def test_wedged_sr_without_children_is_blocked_not_waiting(app, fake):
    """Sr exists, has delegated nothing, and never stops -> block, don't wait.

    The conversation status is one the Cloud can leave a conversation in
    indefinitely (``idle``); once the idle window passes the workflow must not
    sit non-terminal forever with no operator signal.
    """
    app.verifier.sr_idle_timeout_seconds = 0.0
    wf = app.store.create_workflow_once("g")[0]
    cid = fake.cloud.make_conversation(
        title="Sr.g", tags={"orchestratorsr": wf.replace("-", "")},
        execution_status="idle",
    )
    app.store.bind_sr(wf, conversation_id=cid, status="running")

    report = app.verifier.tick()

    assert report.blocked == [wf]
    assert app.store.sr_for(wf)["state"] == "blocked"


def test_recently_started_sr_is_still_waiting(app, fake):
    """A just-started Sr must not be blocked before it can delegate."""
    app.verifier.sr_idle_timeout_seconds = 1800.0
    wf = app.store.create_workflow_once("g")[0]
    cid = fake.cloud.make_conversation(
        title="Sr.g", tags={"orchestratorsr": wf.replace("-", "")},
        execution_status="running",
    )
    app.store.bind_sr(wf, conversation_id=cid, status="running")

    report = app.verifier.tick()

    assert report.waiting == [wf]
    assert app.store.sr_for(wf)["state"] == "queued"


# ---------------------------------------------------------------------------
# 3. The workflow state must not read 'queued' while an Sr is running.
# ---------------------------------------------------------------------------
def test_starting_sr_marks_the_workflow_running(app, fake):
    app.start_workflow("count 17 x 24")
    wf = app.store.list_workflows()[0]["id"]
    assert app.store.sr_for(wf)["sr_conversation_id"]
    assert app.store.sr_for(wf)["state"] == "running"


# ---------------------------------------------------------------------------
# 4. Failure paths: a bad start, a missing conversation id, a lost connection.
# ---------------------------------------------------------------------------
def test_sr_start_error_is_recorded_and_the_workflow_recovers(app, fake):
    """A 5xx on the inline start is recorded with evidence, then recovered."""
    fake.cloud.start_status = 500
    fake.cloud.start_status_times = 1

    with pytest.raises(Exception):
        app.start_workflow("g")

    wf = app.store.list_workflows()[0]["id"]
    assert app.store.sr_for(wf)["sr_last_error"]

    report = app.runtime.tick()
    assert [r["workflow"] for r in report.started] == [wf]
    assert app.store.sr_for(wf)["sr_conversation_id"]


def test_missing_conversation_id_is_not_a_successful_start(app, fake):
    """A start task stuck WORKING past the poll budget must not bind a Sr.

    The poll budget is shortened; the start task never becomes READY, so the
    start must report *pending* rather than record a bogus conversation id. It
    is ``SrStartPending`` and not a failure: the same start task is resumed on a
    later tick, so no attempt is spent and no second conversation is created.
    """
    from orchestration.cloud_client import SrStartPending

    app.runtime.start_poll_attempts = 2
    app.runtime.start_poll_interval = 0.0
    fake.cloud.start_polls_required = 99  # never READY

    wf = app.store.create_workflow_once("g")[0]
    with pytest.raises(SrStartPending):
        app.runtime.ensure_sr(wf)
    # No conversation was bound, and the workflow has no Sr.
    assert app.store.sr_for(wf)["sr_conversation_id"] is None
    assert len(fake.cloud.conversations) == 1  # the conversation Cloud created


def test_recording_a_start_task_keeps_the_start_lease(app, fake):
    """Binding only a start task must not release the Sr start lease.

    ``bind_sr`` runs twice per start: once with the start task (before polling)
    and once with the conversation. If the first call released the lease, the
    workflow would look unclaimed between "start task accepted" and
    "conversation READY" -- ``pending_sr_starts`` excludes only workflows with a
    live lease -- and the next tick would start a *second* Sr. This pins the
    invariant that makes the two-phase start safe when one tick's poll budget
    cannot cover the whole start.
    """
    wf = app.store.create_workflow_once("g")[0]
    assert app.store.claim_sr_start(wf, "w1", lease_seconds=300.0)

    # The first bind records the start task and nothing else.
    app.store.bind_sr(wf, start_task_id="st-1", status="starting")
    row = app.store.sr_for(wf)
    assert row["sr_start_task_id"] == "st-1"
    assert row["sr_conversation_id"] is None
    # Still leased: no other worker may claim it, and it is not "pending".
    assert app.store.claim_sr_start(wf, "w2") is False
    assert [r["id"] for r in app.store.pending_sr_starts()] == []

    # Binding the conversation is what releases the lease.
    app.store.bind_sr(wf, conversation_id="conv-1", status="running")
    assert app.store.sr_for(wf)["sr_conversation_id"] == "conv-1"
    assert [r["id"] for r in app.store.pending_sr_starts()] == []


def test_cloud_connection_loss_during_start_backs_off_without_duplicate(app, fake):
    """A dropped connection keeps the lease and retries without a second Sr."""
    # First attempt fails at the transport layer (search is the first call).
    fake.cloud.search_status = 503
    wf = app.store.create_workflow_once("g")[0]
    first = app.runtime.tick()
    assert first.retried and first.retried[0]["workflow"] == wf
    assert app.store.sr_for(wf)["sr_conversation_id"] is None

    # The outage clears; the retry starts exactly one Sr.
    fake.cloud.search_status = 0
    second = app.runtime.tick()
    assert [r["workflow"] for r in second.started] == [wf]
    assert len(fake.cloud.conversations) == 1


def test_jr_creation_error_leaves_the_sr_tool_call_recorded(app, fake):
    """A disallowed repository fails the tool call and creates no task."""
    app.store.add_repository("acme/ok", allowed_branches=["main"])
    wf = app.store.create_workflow_once("g")[0]
    cid = app.runtime.ensure_sr(wf)
    fake.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "x", "repository": "evil/repo"}
    )

    report = app.runtime.tick()

    assert report.handled[0]["ok"] is False
    assert app.store.list_tasks(workflow_id=wf) == []
    # The failed call is recorded, so it is diagnosable and not retried.
    assert len(app.store.tool_calls(workflow_id=wf)) == 1


def test_event_redelivery_does_not_create_a_second_jr(app, fake):
    """A tool event seen on two polls creates exactly one Jr."""
    app.store.add_repository("acme/ok", allowed_branches=["main"])
    wf = app.store.create_workflow_once("g")[0]
    cid = app.runtime.ensure_sr(wf)
    fake.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "one unit", "repository": "acme/ok"}
    )

    app.runtime.tick()
    app.runtime.tick()
    app.runtime.tick()

    assert len(app.store.list_tasks(workflow_id=wf)) == 1
    assert len(app.store.tool_calls(workflow_id=wf)) == 1


def test_restart_after_start_does_not_duplicate_sr(app, fake):
    """A fresh runtime (restart) reuses the recorded Sr conversation."""
    wf = app.store.create_workflow_once("g")[0]
    cid = app.runtime.ensure_sr(wf)

    from orchestration.sr_runtime import SrRuntime
    from orchestration.sr_tools import SrTools

    restarted = SrRuntime(app.store, app.client,
                          SrTools(app.store, app.dispatcher),
                          start_poll_interval=0.01)
    assert restarted.ensure_sr(wf) == cid
    assert len(fake.cloud.conversations) == 1


# ---------------------------------------------------------------------------
# 5. Acceptance: one workflow -> one Sr -> one Jr -> the result comes back.
# ---------------------------------------------------------------------------
def test_acceptance_one_sr_one_jr_and_the_answer_returns(app, fake):
    """The 17x24 acceptance chain, end to end, over real sockets and a real DB."""
    app.store.add_repository("acme/math", allowed_branches=["main"])
    out = app.start_workflow("Посчитай 17 × 24, затем назови результат одним предложением")
    wf = out["workflow_id"]

    # Exactly one workflow and one Sr.
    assert len(app.store.list_workflows()) == 1
    assert len(fake.cloud.conversations) == 1
    sr_cid = app.store.sr_for(wf)["sr_conversation_id"]
    assert sr_cid

    # Sr delegates the work to a Jr.
    fake.cloud.add_client_tool_action(
        sr_cid, "sr_create_task",
        {"goal": "Посчитай 17 × 24 и назови результат одним предложением",
         "repository": "acme/math", "branch": "main"},
    )
    app.runtime.tick()
    jr = app.store.list_tasks(workflow_id=wf)
    assert len(jr) == 1

    # The Jr dispatcher runs the child to completion with a real answer.
    app.dispatcher.tick()
    jr_cid = app.store.conversation_for(jr[0].id)["external_conversation_id"]
    fake.cloud.add_assistant_message(jr_cid, "17 × 24 = 408")
    fake.cloud.conversations[jr_cid]["execution_status"] = "finished"
    app.dispatcher.tick()

    task = app.store.get_task(jr[0].id)
    assert task.state.value == "completed"
    assert task.result == "17 × 24 = 408"

    # Sr reads the result back.
    fake.cloud.add_client_tool_action(sr_cid, "sr_get_task_report",
                                      {"task_id": jr[0].id})
    app.runtime.tick()
    assert "408" in fake.cloud.messages[sr_cid][-1]

    # Sr stops; the workflow completes on durable state alone.
    fake.cloud.conversations[sr_cid]["execution_status"] = "finished"
    report = app.verifier.tick()
    assert report.completed == [wf]
    assert app.store.sr_for(wf)["state"] == "completed"

