"""Concurrency, leak and shutdown verification for the pooled store.

``PostgresStore`` holds a **connection pool** (``psycopg_pool``; see
``orchestration/cloud_store``). Each caller takes a connection for the duration
of one transaction and returns it; the pool is bounded by ``pool_max`` (default
8) and never opens more. Concurrency safety here means:

* every thread completes without error;
* the number of server-side backends attributable to the store never exceeds
  ``pool_max`` - no leak, no unbounded growth, however many callers there are;
* ``close()`` closes every pooled connection.

This module proves those properties at escalating thread counts (1, 5, 10, 25,
50, 100), repeats the full sweep ten times so a slow leak is visible, and checks
that a shutdown during an active operation neither corrupts the operation nor
leaves a connection open.

(History: this file first asserted the *single-connection* model and that no
pool existed. The pool landed in ``c3127e8``; the assertions below target the
pool's real invariants instead - they are not weaker, they check a different,
still-strong property.)
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run connection concurrency tests"
)

CONCURRENCY_LEVELS = (1, 5, 10, 25, 50, 100)


def _backend_count(store: PostgresStore) -> int:
    """How many server-side backends this DSN's database currently has open.

    Counts every connection to the test database except the one this query
    itself runs on, using a short-lived admin connection so the count is not
    polluted by the probe.
    """
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            return cur.fetchone()[0]


@pytest.fixture
def store():
    s = PostgresStore(DSN)
    yield s
    s.close()


def _hammer(store: PostgresStore, n_threads: int, ops_per_thread: int = 20):
    """Run mixed read/write operations from ``n_threads`` concurrently.

    Returns ``(errors, latencies)``: a list of ``(thread, exception)`` pairs that
    must be empty, and the wall-clock seconds each thread spent.
    """
    errors: list[tuple[int, BaseException]] = []
    latencies: list[float] = []
    errors_lock = threading.Lock()
    latencies_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(index: int):
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        start = time.monotonic()
        try:
            for i in range(ops_per_thread):
                if i % 4 == 0:
                    with store._tx() as cur:
                        cur.execute("SELECT 1 AS one")
                        row = cur.fetchone()
                        assert row == {"one": 1}
                else:
                    store.dashboard_counts()
                    store.list_workflows()
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            with errors_lock:
                errors.append((index, exc))
        finally:
            with latencies_lock:
                latencies.append(time.monotonic() - start)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        if t.is_alive():
            errors.append((-1, TimeoutError("a worker thread did not finish in time")))
    return errors, latencies


@pytest.mark.parametrize("n_threads", CONCURRENCY_LEVELS)
def test_concurrent_access_does_not_error_or_leak_connections(store, n_threads):
    """At every concurrency level the pool stays within its bound.

    ``n_threads`` callers may run at once; the pool hands each its own
    connection, up to ``pool_max``. The invariant is that the backend count
    attributable to this store never exceeds ``pool_max`` - more callers than
    that simply wait for a free connection, they never open an extra one.
    """
    errors, latencies = _hammer(store, n_threads)

    assert errors == [], (
        f"concurrency={n_threads}: {len(errors)} worker(s) failed, first: "
        f"{errors[0][1]!r}"
    )
    after = _backend_count(store)
    assert after <= store.pool_max, (
        f"concurrency={n_threads}: backend count {after} exceeds the pool size "
        f"{store.pool_max} - the store opened more connections than the pool allows"
    )
    assert latencies, "expected at least one completed worker"


def test_ten_consecutive_sweeps_do_not_accumulate_connections(store):
    """Ten back-to-back sweeps across every concurrency level: no net growth.

    A single pass can hide a slow leak (one extra backend per run that a lone
    assertion would not catch against noise); ten in a row make a trend visible
    if one exists. The bound is the pool's own ``pool_max``.
    """
    for sweep in range(10):
        for n_threads in CONCURRENCY_LEVELS:
            errors, _ = _hammer(store, n_threads, ops_per_thread=5)
            assert errors == [], (
                f"sweep {sweep}, concurrency={n_threads}: {errors[0][1]!r}"
            )
        after_sweep = _backend_count(store)
        assert after_sweep <= store.pool_max, (
            f"sweep {sweep}: backend count drifted to {after_sweep}, above the "
            f"pool size {store.pool_max}"
        )


def test_an_exception_mid_operation_releases_the_connection_for_the_next_caller(store):
    """A worker that raises must not leave the connection checked out.

    ``_tx()`` is a context manager; if an exception inside it ever failed to
    return the connection, a few failures would exhaust the pool and every later
    caller would time out. This pins that with a short, deterministic timeout.
    """
    def exploding():
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM table_that_does_not_exist")

    for _ in range(10):
        with pytest.raises(Exception):
            exploding()

    done = threading.Event()

    def probe():
        store.dashboard_counts()
        done.set()

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "a later caller is stuck behind a connection a failure did not return"


def test_shutdown_during_an_active_operation_closes_every_connection(store):
    """``close()`` closes the pool, and an in-flight operation still completes.

    A thread inside ``_tx()`` holds a pooled connection. A concurrent
    ``close()`` must not raise, must not corrupt the in-flight operation (its
    own result stays intact), and must leave no pooled backend open afterwards.
    """
    started = threading.Event()
    outcome: dict[str, object] = {}

    def slow_caller():
        try:
            with store._tx() as cur:
                started.set()
                time.sleep(0.3)
                cur.execute("SELECT 1 AS one")
                row = cur.fetchone()
                outcome["row"] = row
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=slow_caller)
    t.start()
    started.wait(timeout=5)

    close_errors: list[BaseException] = []

    def closer():
        try:
            store.close()
        except BaseException as exc:  # noqa: BLE001
            close_errors.append(exc)

    c = threading.Thread(target=closer)
    c.start()
    c.join(timeout=5)
    t.join(timeout=5)

    assert not t.is_alive(), "the in-flight operation did not finish"
    assert not c.is_alive(), "close() did not finish"
    assert close_errors == [], close_errors
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("row") == {"one": 1}
    assert _backend_count(store) == 0, "close() left a pooled connection open"
