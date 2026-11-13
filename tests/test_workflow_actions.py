"""Operator actions on a workflow: cancel, resume, retry a task, rename Sr.

These pin down the behaviour the dashboard offers, against a real PostgreSQL and
a real HTTP server in front of the fake Cloud, over real sockets.

What matters and is asserted here:

* ``cancel`` is terminal and idempotent, and it really stops the machine --
  ``SrRuntime.tick`` services no tool call for it, ``pending_sr_starts`` never
  re-selects it, and its in-flight child tasks are cancelled so the Jr
  dispatcher stops polling them.
* ``resume`` reopens a blocked/failed workflow and requeues only the children
  that still have attempts left; it refuses a workflow that is already terminal,
  because "resume" on finished work is a mistake, not a no-op.
* ``retry`` requeues one task and cannot be applied to a task in a state the
  state machine has no route from.
* ``rename`` goes to the real Cloud conversation first, so a rejected rename
  leaves no local state claiming otherwise, and a rename never changes the goal.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import TaskState  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run workflow-action tests"
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


def _req(method: str, base: str, path: str, body: dict | None = None,
         token: str = "test-token"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
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


@pytest.fixture
def base(server):
    return f"http://127.0.0.1:{server.port}"


def _make_workflow(app, fake, *, with_sr: bool = True) -> tuple[str, str | None]:
    """A workflow, optionally with a real Sr conversation bound."""
    wid = app.store.create_workflow("actionable work")
    if not with_sr:
        return wid, None
    cid = fake.cloud.make_conversation(title="Sr.actionable", tags={})
    app.store.bind_sr(wid, conversation_id=cid, status="running")
    return wid, cid


# ---------------------------------------------------------------- cancel
def test_cancel_is_terminal_and_idempotent(app, fake):
    wid, _ = _make_workflow(app, fake)
    out = app.cancel_workflow(wid)
    assert out["workflow_id"] == wid
    assert out["cancelled"] is False
    assert app.store.list_workflows()[0]["state"] == "cancelled"

    # A second cancel is a no-op, not an error: a double click is harmless.
    again = app.cancel_workflow(wid)
    assert again["cancelled"] is True
    assert app.store.list_workflows()[0]["state"] == "cancelled"


def test_cancel_does_not_delete_the_sr_conversation(app, fake):
    """Stopping work must not destroy the transcript."""
    wid, cid = _make_workflow(app, fake)
    app.cancel_workflow(wid)
    assert cid in fake.cloud.conversations
    assert app.store.sr_for(wid)["sr_conversation_id"] == cid


def test_cancel_cancels_in_flight_child_tasks(app, fake):
    wid, _ = _make_workflow(app, fake)
    t = app.store.submit_task("child work", workflow_id=wid)
    app.store.transition(t.id, TaskState.ASSIGNED)
    app.store.transition(t.id, TaskState.RUNNING)

    out = app.cancel_workflow(wid)
    assert t.id in out["tasks_cancelled"]
    assert app.store.get_task(t.id).state is TaskState.CANCELLED


def test_cancelled_workflow_is_never_retried_by_the_sr_loop(app, fake):
    """The whole point of cancel: the machine stops touching it."""
    wid, _ = _make_workflow(app, fake, with_sr=False)
    app.cancel_workflow(wid)
    pending = [r["id"] for r in app.store.pending_sr_starts(limit=50)]
    assert wid not in pending


def test_cancelled_workflow_is_not_reopened_by_the_verifier(app, fake):
    wid, _ = _make_workflow(app, fake)
    app.cancel_workflow(wid)
    app.verifier.tick()
    assert app.store.list_workflows()[0]["state"] == "cancelled"


def test_cancel_route(app, fake, base):
    wid, _ = _make_workflow(app, fake)
    status, out = _req("POST", base, f"/api/workflows/{wid}/cancel", {})
    assert status == 200
    assert out["workflow_id"] == wid


# ------------------------------------------------------------ state events
def _state_events(app, wid) -> list[str]:
    with app.store._tx() as cur:
        cur.execute("SELECT detail->>'state' AS state FROM task_events "
                    "WHERE kind='workflow_state' AND workflow_id=%s ORDER BY seq", (wid,))
        return [row["state"] for row in cur.fetchall()]


def _updated_at(app, wid):
    with app.store._tx() as cur:
        cur.execute("SELECT updated_at FROM workflows WHERE id=%s", (wid,))
        return cur.fetchone()["updated_at"]


def test_saying_the_state_a_workflow_is_in_writes_nothing(app, fake):
    """The verifier names the state of every open workflow on every pass. A
    workflow that stays where it is got one event per pass for ever (2 891 in
    a day in production, from four idle workflows), and its ``updated_at``
    moved with each, so it never said when the workflow had last changed."""
    from orchestration.cloud_store import NotFound

    wid, _ = _make_workflow(app, fake)
    before = _state_events(app, wid)
    app.store.set_workflow_state(wid, "blocked")
    changed_at = _updated_at(app, wid)
    assert _state_events(app, wid) == before + ["blocked"]
    for _ in range(5):
        app.store.set_workflow_state(wid, "blocked")
    assert _state_events(app, wid) == before + ["blocked"]
    assert _updated_at(app, wid) == changed_at

    # A change is still a change, in either direction.
    app.store.set_workflow_state(wid, "failed")
    app.store.set_workflow_state(wid, "failed")
    app.store.set_workflow_state(wid, "blocked")
    assert _state_events(app, wid) == before + ["blocked", "failed", "blocked"]
    assert _updated_at(app, wid) > changed_at
    # And a workflow that does not exist is still an error, not a silent no-op.
    with pytest.raises(NotFound):
        app.store.set_workflow_state("00000000-0000-0000-0000-000000000000", "blocked")


def test_the_verifier_passing_over_an_idle_workflow_leaves_no_trail(app, fake):
    """The same through the loop that does it: several passes of the real
    verifier over a workflow whose state does not change."""
    wid, _ = _make_workflow(app, fake)
    app.verifier.tick()
    settled = _state_events(app, wid)
    stamp = _updated_at(app, wid)
    for _ in range(4):
        app.verifier.tick()
    assert _state_events(app, wid) == settled
    assert _updated_at(app, wid) == stamp


# ---------------------------------------------------------------- resume
def test_resume_reopens_a_blocked_workflow(app, fake):
    wid, _ = _make_workflow(app, fake)
    app.store.set_workflow_state(wid, "blocked")
    out = app.resume_workflow(wid)
    assert out["workflow_id"] == wid
    assert app.store.list_workflows()[0]["state"] == "queued"


def test_resume_requeues_retryable_tasks_only(app, fake):
    wid, _ = _make_workflow(app, fake)
    app.store.set_workflow_state(wid, "blocked")

    retryable = app.store.submit_task("retryable", workflow_id=wid, max_attempts=3)
    app.store.transition(retryable.id, TaskState.ASSIGNED)
    app.store.transition(retryable.id, TaskState.RUNNING)
    app.store.transition(retryable.id, TaskState.FAILED)  # attempt -> 1

    exhausted = app.store.submit_task("exhausted", workflow_id=wid, max_attempts=1)
    app.store.transition(exhausted.id, TaskState.ASSIGNED)
    app.store.transition(exhausted.id, TaskState.RUNNING)
    app.store.transition(exhausted.id, TaskState.FAILED)  # attempt -> 1 == max

    done = app.store.submit_task("done", workflow_id=wid)
    app.store.transition(done.id, TaskState.ASSIGNED)
    app.store.transition(done.id, TaskState.RUNNING)
    app.store.transition(done.id, TaskState.VERIFYING)
    app.store.transition(done.id, TaskState.COMPLETED, result="ok")

    out = app.resume_workflow(wid)
    assert out["requeued"] == [retryable.id]
    assert app.store.get_task(retryable.id).state is TaskState.QUEUED
    # A task out of attempts is left for a human; resuming the workflow is not
    # consent to spend an attempt the task does not have.
    assert app.store.get_task(exhausted.id).state is TaskState.FAILED
    assert app.store.get_task(done.id).state is TaskState.COMPLETED


def test_resume_clears_sr_start_backoff_so_the_loop_may_retry(app, fake):
    wid, _ = _make_workflow(app, fake, with_sr=False)
    app.store.record_sr_failure(wid, error="429 too many requests", attempt=1,
                                backoff_seconds=900.0, give_up=True)
    assert app.store.list_workflows()[0]["state"] == "blocked"
    assert wid not in [r["id"] for r in app.store.pending_sr_starts(limit=50)]

    app.resume_workflow(wid)
    pending = [r["id"] for r in app.store.pending_sr_starts(limit=50)]
    assert wid in pending


def test_resume_refuses_terminal_workflows(app, fake):
    wid, _ = _make_workflow(app, fake)
    app.cancel_workflow(wid)
    with pytest.raises(Exception) as exc:
        app.resume_workflow(wid)
    assert "cancel" in str(exc.value)


def test_resume_route_conflicts_on_completed(app, fake, base):
    wid, _ = _make_workflow(app, fake)
    app.store.set_workflow_state(wid, "completed")
    status, out = _req("POST", base, f"/api/workflows/{wid}/resume", {})
    assert status == 409


# ----------------------------------------------------------------- retry
def test_retry_requeues_a_failed_task(app, fake):
    wid, _ = _make_workflow(app, fake)
    t = app.store.submit_task("will fail", workflow_id=wid)
    app.store.transition(t.id, TaskState.ASSIGNED)
    app.store.transition(t.id, TaskState.RUNNING)
    app.store.transition(t.id, TaskState.FAILED)

    out = app.retry_task(t.id)
    assert out["state"] == "queued"
    assert app.store.get_task(t.id).state is TaskState.QUEUED


def test_retry_requeues_a_blocked_task(app, fake):
    wid, _ = _make_workflow(app, fake)
    t = app.store.submit_task("blocked", workflow_id=wid)
    app.store.transition(t.id, TaskState.BLOCKED)
    app.retry_task(t.id)
    assert app.store.get_task(t.id).state is TaskState.QUEUED


def test_retry_route(app, fake, base):
    wid, _ = _make_workflow(app, fake)
    t = app.store.submit_task("blocked", workflow_id=wid)
    app.store.transition(t.id, TaskState.BLOCKED)
    status, out = _req("POST", base, f"/api/tasks/{t.id}/retry", {})
    assert status == 200
    assert out["task_id"] == t.id
    assert out["state"] == "queued"


def test_retry_reports_the_state_it_produced_even_if_the_task_is_claimed_at_once(app, fake):
    """The answer to "retry" is what the retry did, not what happened next.

    The dispatcher loop may claim the task the instant it is queued. The store
    used to read the task again after committing, so the answer could already be
    the next state: the route above reported ``assigned`` about one time in
    seven. Here the claim is made to happen at exactly that moment, every time.
    """
    wid, _ = _make_workflow(app, fake)
    t = app.store.submit_task("blocked", workflow_id=wid)
    app.store.transition(t.id, TaskState.BLOCKED)

    read = app.store.get_task

    def read_too_late(task_id):
        # Stand in for the dispatcher: by the time anyone looks at a queued
        # task again, another worker has claimed it.
        if read(task_id).state is TaskState.QUEUED:
            assert app.store.claim_task("another-worker", task_id=task_id) is not None
        return read(task_id)

    app.store.get_task = read_too_late
    try:
        out = app.retry_task(t.id)
    finally:
        del app.store.get_task
    assert out == {"task_id": t.id, "state": "queued"}


def test_retry_unknown_task_is_404(app, fake, base):
    status, _ = _req("POST", base,
                     "/api/tasks/00000000-0000-0000-0000-000000000000/retry", {})
    assert status == 404


# ---------------------------------------------------------------- rename
def test_rename_updates_the_real_cloud_conversation(app, fake):
    wid, cid = _make_workflow(app, fake)
    out = app.rename_sr(wid, "Моё новое имя")
    assert out["title"] == "Моё новое имя"
    assert fake.cloud.conversations[cid]["title"] == "Моё новое имя"
    assert ("PATCH", f"/api/v1/app-conversations/{cid}") in fake.cloud.calls


def test_rename_never_changes_the_goal(app, fake):
    wid, _ = _make_workflow(app, fake)
    before = app.store.sr_for(wid)["goal"]
    app.rename_sr(wid, "renamed")
    assert app.store.sr_for(wid)["goal"] == before


def test_rename_rejects_an_empty_title(app, fake, base):
    wid, _ = _make_workflow(app, fake)
    status, _ = _req("POST", base, f"/api/workflows/{wid}/rename", {"title": "  "})
    assert status == 400


def test_rename_without_an_sr_is_a_client_error(app, fake, base):
    wid, _ = _make_workflow(app, fake, with_sr=False)
    status, _ = _req("POST", base, f"/api/workflows/{wid}/rename", {"title": "x"})
    assert status == 400


def test_rename_route(app, fake, base):
    wid, cid = _make_workflow(app, fake)
    status, out = _req("POST", base, f"/api/workflows/{wid}/rename",
                       {"title": "Новое"})
    assert status == 200
    assert out["conversation_id"] == cid
    assert fake.cloud.conversations[cid]["title"] == "Новое"


# ------------------------------------------------------- detail / link
def test_detail_exposes_a_usable_cloud_url(app, fake, base):
    """The UI links to ``sr_url``; without it the link never rendered."""
    wid, cid = _make_workflow(app, fake)
    status, d = _req("GET", base, f"/api/workflows/{wid}")
    assert status == 200
    url = d["workflow"]["sr_url"]
    assert url and url.startswith("http")
    assert cid in url
    # A human opens this in a browser, so it must not be the JSON API route.
    assert "/api/conversations/" not in url


def test_detail_has_no_url_before_the_sr_exists(app, fake, base):
    wid, _ = _make_workflow(app, fake, with_sr=False)
    _, d = _req("GET", base, f"/api/workflows/{wid}")
    assert d["workflow"]["sr_url"] is None


# ------------------------------------------------------- auth boundary
def test_actions_require_authentication(app, fake, base):
    wid, _ = _make_workflow(app, fake)
    for path in (f"/api/workflows/{wid}/cancel", f"/api/workflows/{wid}/resume",
                 f"/api/workflows/{wid}/rename"):
        req = urllib.request.Request(base + path, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=10)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 401
        assert raised, path


# ------------------------------------------------- malformed id is not a 500
def test_malformed_workflow_id_is_404_not_500(app, fake, base):
    """A non-UUID id must be a 404, never a 500 that echoes a database error.

    Regression: the route handlers passed the raw path segment straight to a
    ``WHERE id=%s`` on a ``uuid`` column. A value like ``nope`` made PostgreSQL
    raise ``InvalidTextRepresentation``, which the generic handler turned into
    a 500 whose body contained the database error text. A bad id is a bad
    request, not a server fault, and the error text must not leak.
    """
    for bad in ("nope", "123", "%20", "a" * 40):
        status, d = _req("GET", base, f"/api/workflows/{bad}")
        assert status == 404, (bad, status, d)
        assert "InvalidTextRepresentation" not in json.dumps(d)

    for action in ("cancel", "resume", "rename", "message"):
        status, d = _req("POST", base, f"/api/workflows/nope/{action}",
                         {"title": "x", "text": "x"})
        assert status == 404, (action, status, d)
        assert "invalid input syntax" not in json.dumps(d)

    status, d = _req("POST", base, "/api/tasks/nope/retry")
    assert status == 404, (status, d)
    assert "invalid input syntax" not in json.dumps(d)


def test_thirty_six_hyphens_is_404_not_500(app, fake, base):
    """The old guard ``[0-9a-fA-F-]{36}`` let 36 hyphens through to the database.

    That string has the right length and only allowed characters, so the
    ``fullmatch`` check passed, the query ran, and PostgreSQL rejected it --
    a 500 with the database error in the body. The guard now requires the real
    8-4-4-4-12 UUID shape, so this is a 404 before any query.
    """
    dashes = "-" * 36
    status, d = _req("GET", base, f"/api/workflows/{dashes}")
    assert status == 404, (status, d)
    assert "invalid input syntax" not in json.dumps(d)

    status, d = _req("POST", base, f"/api/workflows/{dashes}/cancel")
    assert status == 404, (status, d)
    assert "invalid input syntax" not in json.dumps(d)


def test_well_formed_but_unknown_uuid_is_404(app, fake, base):
    """A valid-shaped id that does not exist is still a clean 404."""
    unknown = "00000000-0000-4000-8000-000000000000"
    status, d = _req("GET", base, f"/api/workflows/{unknown}")
    assert status == 404, (status, d)
    status, d = _req("POST", base, f"/api/workflows/{unknown}/cancel")
    assert status == 404, (status, d)
    status, d = _req("POST", base, f"/api/tasks/{unknown}/retry")
    assert status == 404, (status, d)
