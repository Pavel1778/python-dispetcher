"""One user request must create exactly one workflow (and one Sr).

The incident these tests pin down, reproduced from the live account: a user sent
"Привет, создай новую сессию и назови ее: Тест1" once, and the dispatcher created
**three** workflows with byte-identical goals, three Sr conversations and three Jr
tasks, ~63 seconds apart. Each Sr called ``sr_create_task`` exactly once, so the
Sr/Jr semantics were right -- the duplication was at the workflow level.

Root cause: ``App.start_workflow`` minted a fresh workflow id on every call and
started a new Sr for it. A retried request (client timeout followed by a
resubmit, a double submit, a proxy retry) therefore produced another workflow,
another Sr and another Jr. ``tasks`` had an ``idempotency_key``; the workflow
path had nothing equivalent.

The fix is a UNIQUE ``workflows.idempotency_key`` plus an atomic Sr-start claim,
so a retry adopts what the first attempt created. These tests use a real
PostgreSQL and a real HTTP server against the fake Cloud, over real sockets.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run duplicate-request tests"
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


def _post(base: str, path: str, body: dict, token: str = "test-token",
          headers: dict | None = None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


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


def _cloud_conversation_count(fake) -> int:
    return len(fake.cloud.conversations)


# ---------------------------------------------------------------------------
# 1. One request, sent once, creates one task.
# ---------------------------------------------------------------------------
def test_one_request_creates_exactly_one_workflow(server, app, fake):
    status, out = _post(f"http://127.0.0.1:{server.port}", "/api/workflows",
                        {"goal": "Привет, создай новую сессию и назови ее: Тест1"})
    assert status == 200
    assert out["workflow_id"]
    assert len(app.store.list_workflows()) == 1
    assert _cloud_conversation_count(fake) == 1


# ---------------------------------------------------------------------------
# 2. Two identical requests with one idempotency key create one workflow.
# ---------------------------------------------------------------------------
def test_same_idempotency_key_creates_one_workflow(server, app, fake):
    base = f"http://127.0.0.1:{server.port}"
    body = {"goal": "Привет, создай новую сессию и назови ее: Тест1"}
    s1, out1 = _post(base, "/api/workflows", body, headers={"Idempotency-Key": "req-1"})
    s2, out2 = _post(base, "/api/workflows", body, headers={"Idempotency-Key": "req-1"})

    assert (s1, s2) == (200, 200)
    assert out1["workflow_id"] == out2["workflow_id"]
    assert out2["reused"] is True
    assert len(app.store.list_workflows()) == 1
    assert _cloud_conversation_count(fake) == 1


def test_request_id_in_body_is_honoured(server, app, fake):
    base = f"http://127.0.0.1:{server.port}"
    body = {"goal": "g", "request_id": "body-key"}
    _, out1 = _post(base, "/api/workflows", body)
    _, out2 = _post(base, "/api/workflows", body)
    assert out1["workflow_id"] == out2["workflow_id"]
    assert len(app.store.list_workflows()) == 1


# ---------------------------------------------------------------------------
# 3. A retry after a timeout does not create a second workflow.
#    Simulated at the layer that actually matters: the first attempt created the
#    workflow and the Sr, then the client never saw the response.
# ---------------------------------------------------------------------------
def test_retry_after_lost_response_does_not_duplicate(app, fake):
    first = app.start_workflow("Тест1", request_id="retry-key")
    assert first["workflow_id"]
    sr_before = app.store.sr_for(first["workflow_id"])["sr_conversation_id"]
    convs_before = _cloud_conversation_count(fake)

    # The client retries the same logical request.
    second = app.start_workflow("Тест1", request_id="retry-key")

    assert second["workflow_id"] == first["workflow_id"]
    assert second["sr_conversation_id"] == sr_before
    assert second["reused"] is True
    assert _cloud_conversation_count(fake) == convs_before
    assert len(app.store.list_workflows()) == 1


def test_retry_while_first_start_is_still_in_flight_does_not_duplicate(app, fake):
    """The response is lost *and* the first start has not finished yet.

    The retry must not start a second Sr: the claim taken by the first attempt
    makes the second a no-op that returns the in-progress workflow.
    """
    wf, created = app.store.create_workflow_idempotent("Тест1", "inflight-key")
    assert created is True
    # First attempt takes the start lease but has not bound an Sr yet.
    assert app.store.claim_sr_start(wf, "worker-a", lease_seconds=120) is True

    second = app.start_workflow("Тест1", request_id="inflight-key")
    assert second["workflow_id"] == wf
    assert second["reused"] is True
    assert second.get("starting") is True
    assert _cloud_conversation_count(fake) == 0

    # The first attempt died holding the lease. Once it is released the loop
    # completes the start exactly once -- it does not start two.
    app.store.release_sr_start(wf)
    report = app.runtime.tick()
    assert [r["workflow"] for r in report.started] == [wf]
    assert _cloud_conversation_count(fake) == 1


# ---------------------------------------------------------------------------
# 4. Concurrent requests cannot bypass the server-side guard.
# ---------------------------------------------------------------------------
def test_concurrent_requests_with_one_key_create_one_workflow(server, app, fake):
    base = f"http://127.0.0.1:{server.port}"
    body = {"goal": "concurrent Тест1", "request_id": "concurrent-key"}
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def fire():
        r = _post(base, "/api/workflows", body)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {out.get("workflow_id") for _, out in results if out}
    assert len(ids) == 1, f"expected one workflow, got {ids}"
    assert len(app.store.list_workflows()) == 1
    # At most one Sr conversation, however the race resolved.
    assert _cloud_conversation_count(fake) <= 1


def test_concurrent_ticks_start_sr_only_once(app, fake):
    """Two workers racing on the same pending workflow start one Sr, not two."""
    wf = app.store.create_workflow("race")

    outcomes: list[bool] = []
    lock = threading.Lock()

    def claim(worker):
        got = app.store.claim_sr_start(wf, worker, lease_seconds=120)
        with lock:
            outcomes.append(got)

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for o in outcomes if o) == 1, outcomes


# ---------------------------------------------------------------------------
# 5. Two independent tasks with the same text but different keys are allowed.
# ---------------------------------------------------------------------------
def test_two_distinct_requests_with_the_same_text_are_both_created(server, app, fake):
    base = f"http://127.0.0.1:{server.port}"
    _, a = _post(base, "/api/workflows", {"goal": "same text"},
                 headers={"Idempotency-Key": "key-a"})
    _, b = _post(base, "/api/workflows", {"goal": "same text"},
                 headers={"Idempotency-Key": "key-b"})

    assert a["workflow_id"] != b["workflow_id"]
    assert len(app.store.list_workflows()) == 2
    assert _cloud_conversation_count(fake) == 2


def test_no_key_still_creates_distinct_workflows(app, fake):
    """Backwards compatible: without a key each call is a new request."""
    a = app.start_workflow("same")
    b = app.start_workflow("same")
    assert a["workflow_id"] != b["workflow_id"]
    assert len(app.store.list_workflows()) == 2


def test_permanently_failed_start_is_not_retried_forever(app, fake):
    """A blocked workflow must leave the retry scan.

    ``record_sr_failure(give_up=True)`` blocks the workflow and clears the
    backoff, so without excluding ``blocked`` the scan would keep starting
    conversations for a workflow that can never start.
    """
    wf = app.store.create_workflow("doomed")
    app.store.record_sr_failure(
        wf, error="CloudAuthError: bad key", attempt=1,
        backoff_seconds=0.0, give_up=True,
    )
    assert app.store.sr_for(wf)["state"] == "blocked"

    report = app.runtime.tick()
    assert report.started == []
    assert report.retried == []
    assert _cloud_conversation_count(fake) == 0
    assert app.store.pending_sr_starts() == []


# ---------------------------------------------------------------------------
# 6. A repeated tool call must not repeat the external side effect.
# ---------------------------------------------------------------------------
def test_repeated_sr_create_task_with_one_event_id_creates_one_task(app, fake):
    wf = app.store.create_workflow("wf")
    app.runtime.ensure_sr(wf)
    cid = app.store.sr_for(wf)["sr_conversation_id"]

    event_id = fake.cloud.add_client_tool_action(
        cid, "sr_create_task", {"goal": "make Тест1"}
    )
    app.runtime.tick()
    # The same Cloud event is seen again by a later poll.
    app.runtime.tick()
    app.runtime.tick()

    tasks = app.store.list_tasks(workflow_id=wf)
    assert len(tasks) == 1, [t.goal for t in tasks]
    # The key is derived from the Cloud event id, so a re-poll adopts the task.
    with app.store._tx() as cur:
        cur.execute("SELECT idempotency_key FROM tasks WHERE id=%s", (tasks[0].id,))
        assert cur.fetchone()["idempotency_key"] == f"sr:{event_id}"


# ---------------------------------------------------------------------------
# 7. Polling status and reconnecting the UI must not create workflows.
# ---------------------------------------------------------------------------
def test_read_endpoints_never_create_workflows(server, app, fake):
    base = f"http://127.0.0.1:{server.port}"
    for _ in range(3):
        for path in ("/api/dashboard", "/api/workflows", "/api/tasks",
                     "/api/system", "/api/health", "/api/session"):
            req = urllib.request.Request(base + path, method="GET")
            req.add_header("Authorization", "Bearer test-token")
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
    assert app.store.list_workflows() == []
    assert _cloud_conversation_count(fake) == 0


def test_tick_is_idempotent_with_no_queued_work(app, fake):
    app.start_workflow("Тест1", request_id="once")
    before = _cloud_conversation_count(fake)
    for _ in range(3):
        app.runtime.tick()
        app.dispatcher.tick()
    assert _cloud_conversation_count(fake) == before
    assert len(app.store.list_workflows()) == 1


# ---------------------------------------------------------------------------
# 8. A stuck task recovers without duplicating its conversation.
# ---------------------------------------------------------------------------
def test_restart_does_not_recreate_sr_or_conversation(app, fake, monkeypatch):
    first = app.start_workflow("Тест1", request_id="restart-key")
    cid = app.store.sr_for(first["workflow_id"])["sr_conversation_id"]
    convs = _cloud_conversation_count(fake)

    # A fresh App stands in for a process restart against the same database.
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    app2 = App(DSN, poll_interval=0.2, token="test-token")
    app2.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    app2.dispatcher.client = app2.client
    app2.runtime.client = app2.client
    try:
        again = app2.start_workflow("Тест1", request_id="restart-key")
        assert again["workflow_id"] == first["workflow_id"]
        assert again["sr_conversation_id"] == cid
        assert _cloud_conversation_count(fake) == convs
    finally:
        app2.store.close()


# ---------------------------------------------------------------------------
# 9. Regression: the three duplicate workflows from the incident are handled
#    safely -- left alone, not merged, not deleted, and never re-dispatched into
#    new conversations.
# ---------------------------------------------------------------------------
def test_existing_duplicate_workflows_are_not_touched(app, fake):
    """Three pre-existing workflows (as found live) stay three; no new Sr."""
    wfs = [app.store.create_workflow("Привет, создай новую сессию и назови ее: Тест1")
           for _ in range(3)]
    # Give each its Sr, as the incident rows had.
    for wf in wfs:
        app.runtime.ensure_sr(wf)
    convs = _cloud_conversation_count(fake)
    assert convs == 3

    # A tick must not start anything new for them, and must not delete them.
    app.runtime.tick()
    app.dispatcher.tick()

    assert len(app.store.list_workflows()) == 3
    assert _cloud_conversation_count(fake) == convs
    for wf in wfs:
        assert app.store.sr_for(wf)["sr_conversation_id"]


def test_duplicate_workflows_have_distinct_ids_and_one_sr_each(app, fake):
    a = app.start_workflow("dup")
    b = app.start_workflow("dup")
    assert a["workflow_id"] != b["workflow_id"]
    assert a["sr_conversation_id"] != b["sr_conversation_id"]
    for wf in (a["workflow_id"], b["workflow_id"]):
        assert app.store.sr_for(wf)["sr_conversation_id"]
    assert _cloud_conversation_count(fake) == 2
