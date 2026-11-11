"""A dropped database connection is replaced, not kept for the rest of the process.

The store holds one connection for the life of the process, and the driver never
reconnects by itself. When the server end went away (a database restart, a pooler
recycling its clients, a network reset) every later call failed with
``the connection is lost`` until the process was restarted, while ``/healthz`` -
the check a platform uses to decide the instance is fine - kept answering 200.

These tests drop the connection the way a server does (``pg_terminate_backend``
from a second connection) against a real PostgreSQL and a real HTTP server.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import PostgresStore, StoreUnavailable  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the reconnect tests"
)


def _drop(store: PostgresStore) -> int:
    """End the store's connection from the server side. Returns its backend pid."""
    import psycopg

    pid = store._conn.info.backend_pid
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    # The goodbye has to cross the socket; a drop in real life is found long
    # after it happened, not in the same millisecond.
    time.sleep(0.3)
    return pid


def _dead_dsn() -> str:
    """The same DSN pointed at a port nothing listens on."""
    parts = urllib.parse.urlsplit(DSN)
    host = parts.hostname or "127.0.0.1"
    auth = ""
    if parts.username:
        auth = urllib.parse.quote(parts.username)
        if parts.password:
            auth += ":" + urllib.parse.quote(parts.password)
        auth += "@"
    return urllib.parse.urlunsplit(parts._replace(netloc=f"{auth}{host}:1"))


@pytest.fixture
def store():
    s = PostgresStore(DSN)
    yield s
    s.close()


def test_a_connection_dropped_while_idle_costs_the_caller_nothing(store):
    assert isinstance(store.dashboard_counts(), dict)
    old_pid = _drop(store)

    # The very next call works: the drop was noticed before the transaction
    # began, and a new connection took its place.
    assert isinstance(store.dashboard_counts(), dict)
    assert store._conn.info.backend_pid != old_pid
    # ...and it is an ordinary connection again, not a one-off.
    for _ in range(3):
        assert isinstance(store.list_repositories(enabled_only=False), list)


def test_the_replacement_is_opened_the_same_way(store):
    """Row shape and prepared-statement policy must survive a reconnect.

    The pooler-safe default (no server-side prepared statements) is what keeps
    a restart behind PgBouncer working; a reconnect that forgot it would bring
    the original failure back at the worst moment.
    """
    _drop(store)
    with store._tx() as cur:
        cur.execute("SELECT 1 AS one")
        row = cur.fetchone()
    assert row == {"one": 1}, "rows are still dicts"
    assert store._conn.prepare_threshold == store.prepare_threshold
    assert store._conn.autocommit is False


def test_a_connection_lost_mid_transaction_fails_that_call_and_only_that_call(store):
    import psycopg

    with pytest.raises(psycopg.OperationalError) as caught:
        with store._tx() as cur:
            cur.execute("SELECT 1")
            pid = store._conn.info.backend_pid
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            time.sleep(0.3)
            cur.execute("SELECT 2")
    # The caller sees why its statement failed, not a secondary rollback error.
    assert "rollback" not in str(caught.value).lower()

    assert isinstance(store.dashboard_counts(), dict)


def test_an_unreachable_database_is_reported_as_unavailable_and_not_hammered(store):
    real_dsn = store.dsn
    store.dsn = _dead_dsn()
    store.RECONNECT_BACKOFF_SECONDS = 0.6
    _drop(store)

    with pytest.raises(StoreUnavailable) as caught:
        store.dashboard_counts()
    # No host, port or driver text reaches the caller of the store.
    assert str(caught.value) == "database unavailable"

    # Inside the back-off window nothing tries to connect again: every caller
    # waits on one lock, so retrying each time would queue them all behind the
    # connect timeout.
    attempts = []
    real_connect = __import__("psycopg").connect

    def counting_connect(*args, **kwargs):
        attempts.append(1)
        return real_connect(*args, **kwargs)

    import psycopg

    psycopg.connect, saved = counting_connect, psycopg.connect
    try:
        with pytest.raises(StoreUnavailable):
            store.dashboard_counts()
        assert attempts == []

        # The database comes back; after the window the store recovers by itself.
        store.dsn = real_dsn
        time.sleep(0.7)
        assert isinstance(store.dashboard_counts(), dict)
        assert attempts == [1]
    finally:
        psycopg.connect = saved


def test_input_waiting_on_a_healthy_connection_is_not_a_drop(store):
    """Something to read is a reason to ask the connection, not to replace it.

    A notification is the plain case of a server speaking first on a connection
    that is perfectly well.
    """
    import psycopg

    with store._tx() as cur:
        cur.execute("LISTEN store_reconnect_probe")
    pid = store._conn.info.backend_pid
    with psycopg.connect(DSN, autocommit=True) as other:
        other.execute("NOTIFY store_reconnect_probe")
    time.sleep(0.3)

    assert isinstance(store.dashboard_counts(), dict)
    assert store._conn.info.backend_pid == pid, "the working connection was kept"


@pytest.mark.parametrize("failure", [OSError("bad descriptor"),
                                     ValueError("filedescriptor out of range in select()")])
def test_a_check_that_cannot_be_made_leaves_a_working_connection_alone(
        store, monkeypatch, failure):
    """The check may add a reconnect; it must never take a good connection away."""
    from orchestration import cloud_store

    def cannot_tell(_fd):
        raise failure

    monkeypatch.setattr(cloud_store, "_has_pending_input", cannot_tell)
    pid = store._conn.info.backend_pid
    for _ in range(3):
        assert isinstance(store.dashboard_counts(), dict)
    assert store._conn.info.backend_pid == pid


def test_a_descriptor_too_high_for_select_can_still_be_watched():
    """A threaded server holds many sockets; ``select`` stops at number 1024."""
    import select
    import socket

    from orchestration.cloud_store import _has_pending_input

    if not hasattr(select, "poll"):
        pytest.skip("this platform has no poll()")
    import resource

    # Descriptor 1500 has to be allowed at all; raise the soft limit for the
    # length of this test when the hard limit permits it.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1600
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip("this process may not open a descriptor above 1024")
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    left, right = socket.socketpair()
    high = None
    try:
        high = os.dup2(left.fileno(), 1500)
        assert _has_pending_input(high) is False
        right.sendall(b"x")
        assert _has_pending_input(high) is True
        right.close()                       # end-of-file counts as input too
        left.recv(1)
        assert _has_pending_input(high) is True
    finally:
        if high is not None:
            os.close(high)
        left.close()
        right.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ------------------------------------------------------------------ over HTTP
def _get(base: str, path: str, token: str | None = "test-token"):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    with FakeCloudServer() as fake:
        # A long poll interval: the background loop must not be the one that
        # finds the dead connection, the request has to.
        app = App(DSN, poll_interval=3600.0, token="test-token")
        app.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        server = WebServer(app, "127.0.0.1", 0)
        server.start()
        time.sleep(0.3)          # let the loop's first tick finish
        try:
            yield app, f"http://127.0.0.1:{server.port}"
        finally:
            server.stop()


def test_the_server_keeps_answering_after_the_database_dropped_it(served):
    app, base = served
    assert _get(base, "/api/v1/projects")[0] == 200
    _drop(app.store)

    status, body = _get(base, "/api/v1/projects")
    assert status == 200, body
    assert _get(base, "/readyz", token=None) == (200, {"status": "ready", "database": True})


def test_an_outage_is_a_503_without_the_driver_text_and_ends_by_itself(served):
    app, base = served
    real_dsn = app.store.dsn
    app.store.dsn = _dead_dsn()
    app.store.RECONNECT_BACKOFF_SECONDS = 0.5
    _drop(app.store)

    for path in ("/api/v1/projects", "/api/tasks"):
        status, body = _get(base, path)
        assert status == 503, (path, body)
        assert body == {"error": "database unavailable"}
    # Liveness still answers (the process is up); readiness says the truth.
    assert _get(base, "/healthz", token=None)[0] == 200
    assert _get(base, "/readyz", token=None)[0] == 503

    app.store.dsn = real_dsn
    time.sleep(0.6)
    assert _get(base, "/api/v1/projects")[0] == 200
    assert _get(base, "/readyz", token=None)[0] == 200
