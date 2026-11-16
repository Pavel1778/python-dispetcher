"""A dropped database connection is replaced, not kept for the rest of the process.

The store holds a pool of connections, and the driver never reconnects a
connection by itself. When the server end of a pooled connection goes away (a
database restart, a pooler recycling its clients, a network reset) that
connection must be discarded and a fresh one handed to the next caller; before
this, every later call failed with ``the connection is lost`` until the process
was restarted, while ``/healthz`` - the check a platform uses to decide the
instance is fine - kept answering 200.

These tests drop a connection the way a server does (``pg_terminate_backend``
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
    """End the connection a pooled slot is using, from the server side.

    Returns its backend pid. Every pooled connection is dropped, so whichever
    slot the next call takes is one whose server end has gone.
    """
    import psycopg

    pids: list[int] = []
    with store._connection() as conn:
        pids.append(conn.info.backend_pid)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                      "AND application_name <> 'nexus-admin'")
    # The goodbye has to cross the socket; a drop in real life is found long
    # after it happened, not in the same millisecond.
    time.sleep(0.3)
    return pids[0]


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

    # The very next calls work: a dropped connection is discarded by the pool's
    # check and a fresh one takes its place, so the caller sees nothing.
    assert isinstance(store.dashboard_counts(), dict)
    assert isinstance(store.dashboard_counts(), dict)
    # ...and it is an ordinary connection again, not a one-off.
    for _ in range(3):
        assert isinstance(store.list_repositories(enabled_only=False), list)
    with store._connection() as conn:
        assert conn.info.backend_pid != old_pid, "the dropped connection is gone"


def test_the_replacement_is_opened_the_same_way(store):
    """Row shape and prepared-statement policy must survive a reconnect.

    The pooler-safe default (no server-side prepared statements) is what keeps
    a restart behind PgBouncer working; a replacement that forgot it would bring
    the original failure back at the worst moment.
    """
    _drop(store)
    with store._tx() as cur:
        cur.execute("SELECT 1 AS one")
        row = cur.fetchone()
    assert row == {"one": 1}, "rows are still dicts"
    with store._connection() as conn:
        assert conn.prepare_threshold == store.prepare_threshold
        assert conn.autocommit is False


def test_a_connection_lost_mid_transaction_fails_that_call_and_only_that_call(store):
    import psycopg

    with pytest.raises(psycopg.OperationalError) as caught:
        with store._tx() as cur:
            cur.execute("SELECT 1")
            # The pid of the connection this very transaction is using (the
            # cursor carries it), so the drop hits the right backend.
            pid = cur.connection.info.backend_pid
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            time.sleep(0.3)
            cur.execute("SELECT 2")
    # The caller sees why its statement failed, not a secondary rollback error.
    assert "rollback" not in str(caught.value).lower()

    assert isinstance(store.dashboard_counts(), dict)


def test_an_unreachable_database_is_reported_as_unavailable_and_not_hammered(store):
    real_dsn = store.dsn
    store._pool.close()
    store.dsn = _dead_dsn()
    store._pool = __import__("psycopg_pool").ConnectionPool(
        store.dsn, kwargs=store._connect_kwargs, min_size=1, max_size=1,
        open=True, timeout=0.6, reconnect_timeout=0.6,
        check=__import__("orchestration.cloud_store", fromlist=["x"])._pool_check_connection,
    )

    with pytest.raises(StoreUnavailable) as caught:
        store.dashboard_counts()
    # No host, port or driver text reaches the caller of the store.
    assert str(caught.value) == "database unavailable"

    # The pool does not hammer a database that is down: a second call inside the
    # window fails the same way without a fresh connect storm, and once the
    # database is back the store recovers by itself.
    with pytest.raises(StoreUnavailable):
        store.dashboard_counts()

    store._pool.close()
    store.dsn = real_dsn
    store._pool = __import__("psycopg_pool").ConnectionPool(
        store.dsn, kwargs=store._connect_kwargs, min_size=1, max_size=1,
        open=True, timeout=5.0, reconnect_timeout=5.0,
        check=__import__("orchestration.cloud_store", fromlist=["x"])._pool_check_connection,
    )
    assert isinstance(store.dashboard_counts(), dict)


def test_input_waiting_on_a_healthy_connection_is_not_a_drop(store):
    """Something to read is a reason to ask the connection, not to replace it.

    A notification is the plain case of a server speaking first on a connection
    that is perfectly well.
    """
    import psycopg

    with store._tx() as cur:
        cur.execute("LISTEN store_reconnect_probe")
    with psycopg.connect(DSN, autocommit=True) as other:
        other.execute("NOTIFY store_reconnect_probe")
    time.sleep(0.3)

    # The check on a healthy connection with pending input must not throw it
    # away: it is asked, finds itself well, and is reused.
    assert isinstance(store.dashboard_counts(), dict)


@pytest.mark.parametrize("failure", [OSError("bad descriptor"),
                                     ValueError("filedescriptor out of range in select()")])
def test_a_check_that_cannot_be_made_leaves_a_working_connection_alone(
        store, monkeypatch, failure):
    """The check may add a replacement; it must never take a good connection away."""
    from orchestration import cloud_store

    def cannot_tell(_fd):
        raise failure

    monkeypatch.setattr(cloud_store, "_has_pending_input", cannot_tell)
    for _ in range(3):
        assert isinstance(store.dashboard_counts(), dict)


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
    import psycopg_pool
    from orchestration import cloud_store

    app, base = served
    real_dsn = app.store.dsn
    app.store._pool.close()
    app.store.dsn = _dead_dsn()
    app.store._pool = psycopg_pool.ConnectionPool(
        app.store.dsn, kwargs=app.store._connect_kwargs, min_size=1, max_size=1,
        open=True, timeout=0.5, reconnect_timeout=0.5,
        check=cloud_store._pool_check_connection)

    for path in ("/api/v1/projects", "/api/tasks"):
        status, body = _get(base, path)
        assert status == 503, (path, body)
        assert body == {"error": "database unavailable"}
    # Liveness still answers (the process is up); readiness says the truth.
    assert _get(base, "/healthz", token=None)[0] == 200
    assert _get(base, "/readyz", token=None)[0] == 503

    app.store._pool.close()
    app.store.dsn = real_dsn
    app.store._pool = psycopg_pool.ConnectionPool(
        app.store.dsn, kwargs=app.store._connect_kwargs, min_size=1, max_size=1,
        open=True, timeout=5.0, reconnect_timeout=5.0,
        check=cloud_store._pool_check_connection)
    assert _get(base, "/api/v1/projects")[0] == 200
    assert _get(base, "/readyz", token=None)[0] == 200


# --------------------------------------------------------------- pool hygiene
def test_a_failed_transaction_returns_its_connection_to_the_pool(store):
    """An exception inside a transaction must not leak the connection.

    If the pool handed out a connection and never got it back, a few failed
    calls would exhaust it and every later call would time out. The idle count
    is the same before and after a failure, so the connection came back.
    """
    import psycopg

    idle_before = store._pool.get_stats()["pool_available"]
    for _ in range(5):
        with pytest.raises(psycopg.errors.UndefinedTable):
            with store._tx() as cur:
                cur.execute("SELECT 1 FROM a_table_that_does_not_exist")
    idle_after = store._pool.get_stats()["pool_available"]
    assert idle_after == idle_before, "a connection was lost after a failure"
    # The pool is still usable.
    assert isinstance(store.dashboard_counts(), dict)


def test_pool_exhaustion_is_reported_as_unavailable_not_a_hang(store, monkeypatch):
    """When every connection is out and the wait expires, the caller is told.

    Held connections are released afterwards, so this is the *bounded* wait,
    not a deadlock: the caller gets StoreUnavailable and the pool recovers.
    """
    monkeypatch.setattr(store, "pool_timeout", 0.3)
    store._pool.close()
    store._open_pool()
    held = [store._pool.getconn() for _ in range(store.pool_max)]
    try:
        with pytest.raises(StoreUnavailable):
            store.dashboard_counts()
    finally:
        for conn in held:
            store._pool.putconn(conn)
    assert isinstance(store.dashboard_counts(), dict)
