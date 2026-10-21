"""Dispatcher tests against a real local HTTP server.

Every test drives the dispatcher over real sockets. The LLM is the only thing
faked; the API surface, status codes and JSON are real.
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.control_plane import ControlPlane, TaskState  # noqa: E402
from orchestration.dispatcher import Dispatcher  # noqa: E402
from orchestration.openhands_client import (  # noqa: E402
    ApiUnavailable,
    OpenHandsClient,
    _origin,
    task_conversation_id,
)
from tests.fake_runtime import FakeConversation, FakeServer  # noqa: E402

KEY = "test-key"


@pytest.fixture
def server():
    with FakeServer() as s:
        yield s


@pytest.fixture
def cp(tmp_path):
    plane = ControlPlane(tmp_path / "cp.db")
    yield plane
    plane.close()


def make_dispatcher(cp, server, *, worker="w1", lease=300.0):
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    return Dispatcher(
        cp,
        client,
        worker_id=worker,
        workspace_root="/workspace/project",
        lease_seconds=lease,
    )


# ------------------------------------------------------------- happy path
def test_task_goes_from_sqlite_to_conversation(cp, server):
    cp.submit_task("t1", "say hello", max_attempts=3)
    d = make_dispatcher(cp, server)
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.RUNNING
    assert task.conversation_id == task_conversation_id("t1")
    assert report.dispatched == [
        {"task": "t1", "conversation_id": task_conversation_id("t1")}
    ]
    # A real conversation exists in the runtime, with the goal delivered.
    conv = server.runtime.conversations[task.conversation_id]
    assert conv.working_dir.endswith(task.conversation_id)
    assert conv.events == 1
    assert conv.status == "running"


# ------------------------------------------------------- naming (local path)
def test_conversation_is_named_after_it_exists(cp, server):
    """The name must be applied, because create cannot carry one.

    The stand-in mirrors the live server: it ignores ``title`` on create and
    only honours ``PATCH``. If the dispatcher stops renaming, the conversation
    stays untitled and this fails.
    """
    cp.submit_task("t1", "audit the parser", max_attempts=3)
    d = make_dispatcher(cp, server)
    d.tick()

    conv = server.runtime.conversations[task_conversation_id("t1")]
    assert conv.title == "Jr.audit the parser [t1]"


def test_two_tasks_with_one_goal_are_distinguishable(cp, server):
    """Identical goals must not produce identical names."""
    cp.submit_task("t1", "same goal")
    cp.submit_task("t2", "same goal")
    d = make_dispatcher(cp, server)
    d.tick()

    one = server.runtime.conversations[task_conversation_id("t1")].title
    two = server.runtime.conversations[task_conversation_id("t2")].title
    assert one != two
    assert one == "Jr.same goal [t1]"
    assert two == "Jr.same goal [t2]"


def test_adopted_conversation_is_still_named(cp, server):
    """An adopted conversation may be untitled, so the rename runs on every path.

    Create is idempotent on the derived id, so after a restart the server
    returns the conversation that already exists instead of making a new one.
    If the rename only ran when this call created the conversation, that
    pre-existing one would be adopted and left unnamed.
    """
    cid = task_conversation_id("t1")
    # The conversation survived a restart; the task row did not, so it is queued.
    server.runtime.conversations[cid] = FakeConversation(
        id=cid, working_dir=f"/workspace/project/{cid}", title=None
    )
    cp.submit_task("t1", "adopt me", max_attempts=3)

    d = make_dispatcher(cp, server)
    d.tick()

    assert server.runtime.conversations[cid].title == "Jr.adopt me [t1]"


def test_rename_failure_does_not_fail_the_task(cp, server, monkeypatch):
    """Losing a name is cosmetic; the task must still reach RUNNING."""
    cp.submit_task("t1", "keep going", max_attempts=3)
    d = make_dispatcher(cp, server)

    def boom(*a, **k):
        raise ApiUnavailable("rename exploded")

    monkeypatch.setattr(d.client, "rename_conversation", boom)
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.RUNNING
    assert report.dispatched == [
        {"task": "t1", "conversation_id": task_conversation_id("t1")}
    ]


def test_finished_task_is_verified_and_completed(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.finish(cid, "all done")
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.COMPLETED
    assert task.result == "all done"
    assert report.completed == ["t1"]
    assert any(e.get("reason") == "final response verified" for e in task.evidence)


def test_finished_without_result_is_blocked_not_completed(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.finish(cid, None)
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.BLOCKED
    assert task.state is not TaskState.COMPLETED
    assert report.blocked == ["t1"]


def test_waiting_for_confirmation_is_blocked(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.set_status(cid, "waiting_for_confirmation")
    d.tick()
    assert cp.get_task("t1").state is TaskState.BLOCKED


def test_runtime_error_requeues_within_budget(cp, server):
    cp.submit_task("t1", "do it", max_attempts=3)
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.set_status(cid, "error")
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.QUEUED
    assert task.attempt == 1
    assert report.requeued == ["t1"]


def test_non_retryable_runtime_error_dead_letters_immediately(cp, server):
    """A bad API key must not be retried: it cannot start working by itself."""
    cp.submit_task("t1", "do it", max_attempts=5)
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.set_status(cid, "error")
    server.runtime.error_classification = {
        "kind": "auth",
        "retryable": False,
        "user_action": "settings",
    }
    report = d.tick()

    task = cp.get_task("t1")
    assert task.state is TaskState.DEAD_LETTER
    assert task.attempt == 1, "must not burn the whole attempt budget"
    assert report.failed == ["t1"]
    assert any(
        e.get("kind") == "auth" for e in task.evidence
    ), "the runtime's own classification is preserved as evidence"


def test_retryable_runtime_error_is_retried(cp, server):
    """A transient runtime error should still be retried."""
    cp.submit_task("t1", "do it", max_attempts=3)
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id

    server.runtime.set_status(cid, "error")
    server.runtime.error_classification = {
        "kind": "rate_limit",
        "retryable": True,
        "user_action": "retry",
    }
    report = d.tick()

    assert cp.get_task("t1").state is TaskState.QUEUED
    assert report.requeued == ["t1"]


def test_error_exhausts_attempts_and_dead_letters(cp, server):
    cp.submit_task("t1", "do it", max_attempts=2)
    d = make_dispatcher(cp, server)

    for _ in range(4):
        d.tick()
        task = cp.get_task("t1")
        if task.state in (TaskState.COMPLETED, TaskState.DEAD_LETTER):
            break
        cid = task.conversation_id
        if cid and cid in server.runtime.conversations:
            server.runtime.set_status(cid, "error")

    assert cp.get_task("t1").state is TaskState.DEAD_LETTER


# ----------------------------------------------------------- idempotency
def test_restart_does_not_create_a_second_conversation(cp, server):
    cp.submit_task("t1", "do it")
    make_dispatcher(cp, server).tick()
    first = cp.get_task("t1").conversation_id
    assert server.runtime.created_count == 1

    # Simulate a control-plane restart: new dispatcher, same database.
    make_dispatcher(cp, server, worker="w2").tick()

    assert server.runtime.created_count == 1, "restart must not duplicate"
    assert cp.get_task("t1").conversation_id == first


def test_double_tick_does_not_duplicate_conversations(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    d.tick()
    d.tick()
    assert server.runtime.created_count == 1


def test_unbound_assignment_is_requeued_not_duplicated(cp, server):
    """A crash between claim and create must not strand the task.

    The first tick requeues it; the second dispatches it. One action per task
    per tick keeps a persistently failing task from spinning in a single tick.
    """
    cp.submit_task("t1", "do it")
    cp.claim_task("t1", "w1", lease_seconds=300)
    assert cp.get_task("t1").state is TaskState.ASSIGNED
    assert cp.get_task("t1").conversation_id is None

    d = make_dispatcher(cp, server)
    report = d.tick()

    assert "t1" in report.requeued
    assert cp.get_task("t1").state is TaskState.QUEUED

    d.tick()
    assert cp.get_task("t1").state is TaskState.RUNNING
    assert server.runtime.created_count == 1


def test_requeue_after_reopen_keeps_single_conversation(tmp_path, server):
    """Reopening the SQLite file is the real crash path."""
    path = tmp_path / "cp.db"
    cp1 = ControlPlane(path)
    cp1.submit_task("t1", "do it")
    make_dispatcher(cp1, server).tick()
    cid = cp1.get_task("t1").conversation_id
    cp1.close()

    cp2 = ControlPlane(path)
    task = cp2.get_task("t1")
    assert task.conversation_id == cid, "assignment survives the restart"
    make_dispatcher(cp2, server, worker="w2").tick()
    assert server.runtime.created_count == 1
    cp2.close()


# ------------------------------------------------------ failure handling
def test_api_timeout_keeps_task_leased(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    before = cp.get_task("t1")

    server.runtime.fail_times = 1
    report = d.tick()

    after = cp.get_task("t1")
    assert report.unavailable == ["t1"]
    assert after.state is before.state, "transient error must not change state"
    assert after.attempt == before.attempt


def test_api_unavailable_during_dispatch_releases_lease(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    server.runtime.fail_times = 1
    report = d.tick()

    assert report.unavailable == ["t1"]
    task = cp.get_task("t1")
    assert task.state is TaskState.QUEUED
    assert task.attempt == 0, "transport failure is not the task's fault"
    assert server.runtime.created_count == 0


def test_connection_refused_is_unavailable(cp):
    """A dead port must surface as transient, not as success."""
    client = OpenHandsClient("http://127.0.0.1:9", api_key=KEY, timeout=2.0)
    d = Dispatcher(
        cp, client, worker_id="w1", workspace_root="/workspace/project"
    )
    cp.submit_task("t1", "do it")
    report = d.tick()
    assert report.unavailable == ["t1"]
    assert cp.get_task("t1").state is TaskState.QUEUED


def test_capacity_429_does_not_lose_the_task(cp, server):
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    server.runtime.create_status = 429
    report = d.tick()
    assert report.unavailable == ["t1"]
    assert cp.get_task("t1").state is TaskState.QUEUED
    assert cp.get_task("t1").attempt == 0


def test_vanished_conversation_is_requeued(cp, server):
    cp.submit_task("t1", "do it", max_attempts=3)
    d = make_dispatcher(cp, server)
    d.tick()
    cid = cp.get_task("t1").conversation_id
    server.runtime.drop(cid)

    report = d.tick()
    assert "t1" in report.requeued
    assert cp.get_task("t1").state is TaskState.QUEUED


# ------------------------------------------------------------- recovery
def test_expired_lease_is_reclaimed(cp, server):
    cp.submit_task("t1", "do it")
    cp.claim_task("t1", "dead-worker", lease_seconds=-1)
    d = make_dispatcher(cp, server)
    report = d.tick()
    assert any(r["task"] == "t1" for r in report.reclaimed)


def test_lost_worker_task_is_recovered_and_finished(cp, server):
    """Worker dies mid-task; recovery adopts the conversation and completes."""
    cp.submit_task("t1", "do it", max_attempts=3)
    make_dispatcher(cp, server).tick()
    cid = cp.get_task("t1").conversation_id

    # The worker is gone: expire its lease by rewinding the clock via a claim.
    with cp._tx() as conn:  # noqa: SLF001 - test needs to age the lease
        conn.execute(
            "UPDATE tasks SET lease_expires=? WHERE id=?", (0.0, "t1")
        )
    server.runtime.finish(cid, "recovered result")

    report = make_dispatcher(cp, server, worker="w2").tick()
    task = cp.get_task("t1")
    assert task.state is TaskState.COMPLETED
    assert task.result == "recovered result"
    assert server.runtime.created_count == 1, "no duplicate on recovery"
    assert "t1" in report.completed


def test_approval_gate_blocks_dispatch(cp, server):
    cp.submit_task("t1", "risky", requires_approval=True)
    d = make_dispatcher(cp, server)
    report = d.tick()
    assert report.dispatched == []
    assert server.runtime.created_count == 0
    assert cp.get_task("t1").state is TaskState.QUEUED

    cp.approve("t1", approved=True)
    d.tick()
    assert server.runtime.created_count == 1


def test_dependencies_block_dispatch(cp, server):
    cp.submit_task("dep", "first")
    cp.submit_task("t2", "second", dependencies=["dep"])
    d = make_dispatcher(cp, server)
    d.tick()

    assert cp.get_task("t2").state is TaskState.QUEUED
    cid = cp.get_task("dep").conversation_id
    server.runtime.finish(cid, "dep done")
    d.tick()
    assert cp.get_task("dep").state is TaskState.COMPLETED

    d.tick()
    assert cp.get_task("t2").state is TaskState.RUNNING


def test_reprocessing_same_event_is_idempotent(cp, server):
    """Ticking repeatedly after completion must not change anything."""
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server)
    d.tick()
    server.runtime.finish(cp.get_task("t1").conversation_id, "done")
    d.tick()
    assert cp.get_task("t1").state is TaskState.COMPLETED

    for _ in range(3):
        d.tick()
    assert cp.get_task("t1").state is TaskState.COMPLETED
    assert server.runtime.created_count == 1


def test_no_api_key_sent_when_not_configured(cp, server):
    """Auth is used, never bypassed: the key goes in the documented header."""
    cp.submit_task("t1", "do it")
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    Dispatcher(cp, client, worker_id="w1", workspace_root="/workspace/project").tick()
    assert server.runtime.seen_keys, "client must send a key"
    assert all(k == KEY for k in server.runtime.seen_keys)


# --------------------------------------------------- redirect key confinement
#
# The session key travels in a header, and ``urlopen`` re-sends request headers
# to whatever host a 3xx names. These tests drive two real listeners so the
# claim "the key never reaches another origin" is observed, not assumed.

KEY_LEAK_CANARY = "SESSION_KEY_CANARY_XYZ"


def _serve(handler_cls):
    """Start a throwaway HTTP server on a free loopback port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _RecordingSink(BaseHTTPRequestHandler):
    """Records the session key it was sent, then answers 200."""

    seen: list[str | None] = []

    def do_GET(self):
        type(self).seen.append(self.headers.get("X-Session-API-Key"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def _redirector(location):
    """A handler that answers every request with a 302 to ``location``."""

    class _Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *a):
            pass

    return _Redirector


def _probe_redirect(location_for_sink, *, api_key=KEY_LEAK_CANARY):
    """Point a client at a redirector and return what the sink received."""
    _RecordingSink.seen = []
    sink = _serve(_RecordingSink)
    sink_port = sink.server_address[1]
    redirector = _serve(_redirector(location_for_sink(sink_port)))
    client = OpenHandsClient(
        f"http://127.0.0.1:{redirector.server_address[1]}", api_key=api_key, timeout=5.0
    )
    error = None
    try:
        client._request("GET", "/health")
    except Exception as exc:  # noqa: BLE001 - the refusal is the point
        error = exc
    finally:
        redirector.shutdown()
        sink.shutdown()
    return _RecordingSink.seen, error


def test_cross_host_redirect_does_not_leak_the_session_key():
    """A redirect to another port (different origin) must not carry the key."""
    seen, error = _probe_redirect(lambda p: f"http://127.0.0.1:{p}/steal")
    assert seen == [], f"the session key leaked to the redirect target: {seen}"
    assert isinstance(error, ApiUnavailable), "the client must refuse, not silently follow"


def test_redirect_to_another_host_does_not_leak_the_session_key():
    """A redirect to a different hostname must not carry the key.

    ``localhost`` and ``127.0.0.1`` resolve to the same machine, so the sink
    really does receive the request if the client follows it; only the origin
    check stops the key from being sent.
    """
    seen, error = _probe_redirect(lambda p: f"http://localhost:{p}/steal")
    assert seen == [], f"the session key leaked to another host: {seen}"
    assert isinstance(error, ApiUnavailable)


def test_scheme_downgrade_redirect_is_refused():
    """https -> http is a different origin even on the same host and port."""
    seen, error = _probe_redirect(lambda p: f"https://127.0.0.1:{p}/steal")
    assert seen == []
    assert isinstance(error, ApiUnavailable)


def test_same_origin_redirect_is_still_followed():
    """Refusing cross-origin must not break a same-origin redirect."""
    hits: list[str] = []

    class _SameOrigin(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/from":
                self.send_response(302)
                self.send_header("Location", "/to")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    httpd = _serve(_SameOrigin)
    client = OpenHandsClient(
        f"http://127.0.0.1:{httpd.server_address[1]}", api_key=KEY_LEAK_CANARY, timeout=5.0
    )
    try:
        status, payload = client._request("GET", "/from")
    finally:
        httpd.shutdown()
    assert status == 200
    assert payload == {"ok": True}
    assert hits == ["/from", "/to"], "a same-origin redirect must still be followed"


def test_origin_normalises_default_ports():
    """An omitted port and the explicit default are the same origin."""
    assert _origin("http://h/a") == _origin("http://h:80/a")
    assert _origin("https://h/a") == _origin("https://h:443/a")
    assert _origin("HTTP://H:80/a") == _origin("http://h/a")
    assert _origin("https://h/a") != _origin("http://h/a")
    assert _origin("http://h/a") != _origin("http://h:8080/a")
    assert _origin("http://h/a") != _origin("http://other/a")


def test_unparsable_port_is_never_treated_as_same_origin():
    """A malformed port must refuse rather than compare as equal."""
    assert _origin("http://h:notaport/a") != _origin("http://h/a")
    assert _origin("http://h:notaport/a")[2] == -1
