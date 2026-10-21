"""A slow Sr start must not spawn a second Sr conversation.

The incident: task ``67a31b4b-28ee-4075-b999-5b9888b61fba`` produced several
``sr_bound`` events with different ``start_task_id`` values, one of the bound
conversations (``4a2e18211bd24eaba12d4663ba7be66e``) died immediately, and no
Jr child task was ever created.

Root cause (proven below, not assumed):

``SrRuntime.ensure_sr`` records ``sr_start_task_id`` and a ``starting`` status
via ``bind_sr`` *before* it polls. ``bind_sr`` clears ``sr_start_lease_expires``
unconditionally. So the moment the start task exists the workflow looks
unclaimed *and* still has no ``sr_conversation_id`` -- which is exactly the
predicate ``pending_sr_starts`` / ``claim_sr_start`` use to decide "this
workflow needs an Sr". A start that is slow (the real two-phase window: the id
is only meaningful at ``READY``) therefore:

1. leaves the lease clear, so the next tick claims the workflow again;
2. finds no ``sr_conversation_id`` and no orphan (the conversation is not in the
   search index until READY), so it calls ``start_conversation`` a second time;
3. writes a second ``sr_bound`` with a different ``start_task_id``.

Each retry creates another Sr conversation for one workflow. Whichever one the
user looked at first can be the one that dies, and the workflow's
``sr_conversation_id`` may end up pointing at a conversation that never
received the delegating instruction -- so no Jr is ever created.

Unlike the Jr path (``cloud_dispatcher._start_task``), which resumes a recorded
``external_start_task_id``, the Sr path never resumed a recorded
``sr_start_task_id``: ``SrRuntime`` did not read the column at all. That
asymmetry is the bug.

These tests run the real web server, the real ``SrRuntime`` and a real
PostgreSQL against the fake Cloud over real sockets.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run slow-start tests"
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
    yield a
    a.store.close()


@pytest.fixture
def server(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield ws
    ws.stop()


def _sr_bound_events(app, wf: str) -> list[dict]:
    return [e for e in app.store.events(workflow_id=wf, limit=200)
            if e["kind"] == "sr_bound"]


def _distinct_start_tasks(app, wf: str) -> set[str]:
    return {
        (e["detail"] or {}).get("start_task_id")
        for e in _sr_bound_events(app, wf)
        if (e["detail"] or {}).get("start_task_id")
    }


# ---------------------------------------------------------------------------
# The incident itself.
# ---------------------------------------------------------------------------
def test_slow_start_creates_exactly_one_sr(app, fake):
    """A start that outlives one poll budget must not create a second Sr."""
    # The start task needs 5 polls before the conversation id is meaningful;
    # the runtime's poll budget is 2, so the first inline attempt cannot finish.
    fake.cloud.start_ready_after_polls = 5
    app.runtime.start_poll_attempts = 2
    app.runtime.start_poll_interval = 0.01

    app.start_workflow("count 17 x 24")

    wf = app.store.list_workflows()[0]["id"]
    # The inline attempt could not resolve the conversation; it must be recorded
    # as *starting*, not as a failure, and it must keep the start task id.
    row = app.store.sr_for(wf)
    assert row["sr_start_task_id"], "the start task id must be recorded"
    assert row["sr_conversation_id"] is None

    # The background loop now runs repeatedly. It must resume the recorded start
    # task instead of starting a second conversation.
    for _ in range(8):
        app.runtime.tick()

    sr = app.store.sr_for(wf)
    assert sr["sr_conversation_id"], "Sr must eventually be bound"

    # The incident signature: several sr_bound events with different start ids.
    assert len(_distinct_start_tasks(app, wf)) == 1, (
        "one workflow must have exactly one Sr start task; "
        f"got {sorted(_distinct_start_tasks(app, wf))}"
    )
    assert len(fake.cloud.conversations) == 1, (
        "exactly one Sr conversation must exist; "
        f"got {len(fake.cloud.conversations)}"
    )
    assert fake.cloud.calls.count(("POST", "/api/v1/app-conversations")) == 1


def test_slow_start_then_restart_still_one_sr(app, fake):
    """A worker restart mid-start must resume, not duplicate (recovery)."""
    fake.cloud.start_ready_after_polls = 5
    app.runtime.start_poll_attempts = 2
    app.runtime.start_poll_interval = 0.01

    app.start_workflow("count 17 x 24")
    wf = app.store.list_workflows()[0]["id"]
    assert app.store.sr_for(wf)["sr_start_task_id"]

    # Simulate a restart: a brand-new runtime instance over the same store.
    from orchestration.sr_runtime import SrRuntime  # noqa: E402

    restarted = SrRuntime(
        app.store, app.client, app.runtime.tools,
        start_poll_attempts=10, start_poll_interval=0.01,
    )
    for _ in range(4):
        restarted.tick()

    assert app.store.sr_for(wf)["sr_conversation_id"]
    assert len(_distinct_start_tasks(app, wf)) == 1
    assert len(fake.cloud.conversations) == 1


def test_slow_start_does_not_double_start_concurrent_ticks(app, fake):
    """Two ticks racing on one slow start must not both start a conversation."""
    fake.cloud.start_ready_after_polls = 5
    app.runtime.start_poll_attempts = 2
    app.runtime.start_poll_interval = 0.01

    app.start_workflow("count 17 x 24")
    wf = app.store.list_workflows()[0]["id"]

    import threading

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(4):
                app.runtime.tick()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(fake.cloud.conversations) == 1
    assert len(_distinct_start_tasks(app, wf)) == 1


def test_a_failed_start_task_is_reported_and_not_retried_forever(app, fake):
    """An ERROR start task must surface its reason, not silently re-start."""
    fake.cloud.start_fail = True

    with pytest.raises(Exception):
        app.start_workflow("count 17 x 24")

    wf = app.store.list_workflows()[0]["id"]
    for _ in range(6):
        app.runtime.tick()

    row = app.store.sr_for(wf)
    assert row["sr_last_error"], "the Cloud error must be recorded for diagnosis"
    # It must not have started a fresh conversation per tick.
    assert fake.cloud.calls.count(("POST", "/api/v1/app-conversations")) <= 1


# ---------------------------------------------------------------------------
# The same class of bug on the Jr side.
# ---------------------------------------------------------------------------
def test_slow_jr_start_creates_exactly_one_conversation(app, fake):
    """A slow Jr start must be resumed, not restarted, and must settle.

    ``CloudDispatcher._await_start`` had the same early-return as the Sr path:
    it accepted ``app_conversation_id`` before the start task was READY. The task
    was then marked ``running`` while its conversation was still provisioning,
    the next tick could not address that conversation, requeued the task, and
    the cycle repeated -- a live-lock between ``running`` and ``queued`` that
    never settled. The fix waits for READY, so the task starts once and stays
    running.
    """
    repo = "acme/repo"
    app.store.add_repository(repo, allowed_branches=["main"])
    fake.cloud.start_ready_after_polls = 5
    app.dispatcher.start_poll_attempts = 2
    app.dispatcher.start_poll_interval = 0.01

    task = app.store.submit_task("do the work", selected_repository=repo)

    states = []
    for _ in range(8):
        app.dispatcher.tick()
        states.append(app.store.get_task(task.id).state.value)

    convs = [c for c in fake.cloud.conversations.values()
             if c.get("tags", {}).get("orchestratortaskid")
             == task.id.replace("-", "")]
    assert len(convs) == 1, (
        f"one Jr task must own exactly one conversation, got {len(convs)}"
    )
    # The recorded conversation is the one that actually came up.
    bound = app.store.conversation_for(task.id)
    assert bound and bound["external_conversation_id"] == convs[0]["id"]
    # It must not have started a fresh conversation per tick.
    starts = [c for c in fake.cloud.calls
              if c == ("POST", "/api/v1/app-conversations")]
    assert len(starts) == 1, f"expected one start POST, got {len(starts)}"
    # And it must settle: a task that oscillates running<->queued every tick is
    # the live-lock the early return caused.
    assert states.count("running") >= 5, (
        f"task never settled into running; states were {states}"
    )
