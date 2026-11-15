"""Concurrency and leak verification for the shared single-connection store.

``PostgresStore`` holds exactly **one** physical connection for the life of
the process (see the module docstring of ``orchestration.cloud_store``) and
serialises every caller through ``self._lock``. There is no connection pool
anywhere in this codebase (no ``psycopg_pool``, no ``getconn``/``putconn``):
concurrency safety here means every thread converges on the one connection
without corrupting a transaction, without starving, and without the store
ever opening a second physical connection to the database by accident.

This module proves that property at escalating thread counts (1, 5, 10, 25,
50, 100) and checks, after every run, that the database server reports
exactly the connection count the store is expected to hold - i.e. a run does
not leak a server-side backend. It repeats the full sweep ten times in a row
(``test_ten_consecutive_sweeps_do_not_accumulate_connections``) because a
one-shot pass cannot tell a stable baseline from a slow leak.
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

    Returns ``(errors, latencies)``: a list of ``(thread, exception)`` pairs
    that must be empty, and the wall-clock seconds each thread spent (used to
    report the lock's contention, not asserted on - contention is expected
    and correct under one shared connection).
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
def test_concurrent_access_does_not_error_or_open_a_second_connection(store, n_threads):
    """At every concurrency level the shared connection survives intact.

    There being no pool, "no leak" here means the backend count attributable
    to this store never exceeds one: ``n_threads`` callers must converge on
    the single connection, never open their own.
    """
    before = _backend_count(store)

    errors, latencies = _hammer(store, n_threads)

    assert errors == [], (
        f"concurrency={n_threads}: {len(errors)} worker(s) failed, first: "
        f"{errors[0][1]!r}"
    )
    after = _backend_count(store)
    assert after == before, (
        f"concurrency={n_threads}: backend count changed from {before} to {after} "
        "- the store opened an extra connection instead of serialising on one"
    )
    assert store._conn.closed == 0, "the shared connection must still be open"
    # Report (not asserted on): the lock serialises access, so wall time grows
    # with concurrency - that is the expected cost of one physical connection,
    # not a defect.
    assert latencies, "expected at least one completed worker"


def test_ten_consecutive_sweeps_do_not_accumulate_connections(store):
    """Ten back-to-back sweeps across every concurrency level: no net growth.

    A single pass can hide a slow leak (one extra backend per run that a lone
    assertion would not catch against noise); ten in a row make a trend
    visible if one exists.
    """
    baseline = _backend_count(store)
    history = [baseline]

    for sweep in range(10):
        for n_threads in CONCURRENCY_LEVELS:
            errors, _ = _hammer(store, n_threads, ops_per_thread=5)
            assert errors == [], (
                f"sweep {sweep}, concurrency={n_threads}: {errors[0][1]!r}"
            )
        after_sweep = _backend_count(store)
        history.append(after_sweep)
        assert after_sweep == baseline, (
            f"sweep {sweep}: backend count drifted to {after_sweep} "
            f"(baseline {baseline}); history={history}"
        )

    assert store._conn.closed == 0


def test_an_exception_mid_operation_releases_the_lock_for_the_next_caller(store):
    """A worker that raises must not leave the lock held or the connection unusable.

    ``_tx()`` is a context manager; if an exception inside it ever failed to
    unwind the lock, every later caller - on any thread - would hang forever.
    This pins that with a short, deterministic timeout.
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
    assert done.is_set(), "a later caller is stuck behind a lock the failure did not release"


def test_shutdown_during_an_active_operation_waits_then_closes_cleanly(store):
    """``close()`` takes the same lock as every operation: it waits, not races.

    The store has one lock guarding the one connection, and ``close()`` goes
    through it like any other caller (see ``PostgresStore.close``). This pins
    that ordering directly: a thread holding the lock inside ``_tx()`` must
    finish - successfully, with its own result intact - *before* a concurrent
    ``close()`` call (run from this test, standing in for a shutdown request)
    is allowed to proceed, and ``close()`` itself must not raise.
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
    close_start = time.monotonic()
    c.start()
    c.join(timeout=5)
    close_elapsed = time.monotonic() - close_start
    t.join(timeout=5)

    assert not t.is_alive(), "the in-flight operation did not finish"
    assert not c.is_alive(), "close() did not finish"
    assert close_errors == [], close_errors
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("row") == {"one": 1}
    # close() had to wait out the ~0.3s held operation, not return instantly.
    assert close_elapsed >= 0.2, (
        f"close() returned after {close_elapsed:.3f}s - it did not wait for "
        "the lock the in-flight operation was holding"
    )
    assert store._conn.closed != 0, "the connection must be closed afterward"
