"""Transaction handling: a failed statement must not poison the connection.

Regression test for the reported ``InFailedSqlTransaction: current transaction
is aborted, commands ignored until end of transaction block``. That message is
never the real fault: it is the *second* error, raised when a later statement
runs on a connection whose previous transaction already aborted. The root cause
is the first failing statement (here ``UndefinedTable``), and it must surface to
its own caller instead of being masked.

Two defects produced the cascade and both are covered here:

1. Read paths opened a raw cursor and called ``commit()`` with no ``rollback``,
   so any SQL error left the shared connection in the aborted state.
2. The threaded web server shared one connection across request threads with no
   lock, so two threads interleaved on one transaction.

The database is taken from ``TEST_SUPABASE_DB_URL`` (falling back to
``SUPABASE_DB_URL``). When neither is set the module is skipped, so the suite
still runs on a machine without PostgreSQL.
"""

from __future__ import annotations

import os
import re
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run transaction tests"
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIG = os.path.join(_ROOT, "migrations")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sessions, login_attempts, sr_tool_calls, "
                "task_events, conversations, tasks, repositories, workflows CASCADE"
            )
            for name in ("0001_init.sql", "0002_sr_runtime.sql",
                         "0003_tool_call_recovery.sql", "0004_auth.sql"):
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


def test_root_cause_is_not_masked_by_the_cascade(store):
    """The first error must reach the caller, not InFailedSqlTransaction."""
    with pytest.raises(Exception) as excinfo:
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM table_that_does_not_exist")
    assert type(excinfo.value).__name__ == "UndefinedTable", (
        "the original SQL error must not be replaced by the cascade, got "
        f"{type(excinfo.value).__name__}"
    )


def test_connection_recovers_after_a_failed_statement(store):
    """A failed transaction is rolled back, so later calls still work."""
    with pytest.raises(Exception):
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM table_that_does_not_exist")

    # This is the exact operation that used to raise InFailedSqlTransaction.
    assert store.list_workflows() == []
    assert store.dashboard_counts()["workflows"] == 0


def test_swallowed_failure_does_not_poison_the_next_caller(store):
    """Even if a caller ignores its error, the connection is left usable."""
    try:
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM table_that_does_not_exist")
    except Exception:
        pass  # a buggy caller that ignores the failure

    assert store.list_workflows() == []


def test_read_paths_roll_back_instead_of_only_committing(store):
    """Every store transaction goes through the helper that rolls back.

    Guards against reintroducing the raw ``with self._conn.cursor()`` pattern,
    which had no rollback and left the connection aborted on any SQL error.
    """
    src = open(os.path.join(_ROOT, "orchestration", "cloud_store.py")).read()
    assert "self._conn.cursor()" not in src, (
        "use the _tx() helper so a failed statement is rolled back"
    )
    assert "self._conn.commit()" not in src, (
        "commit() outside _tx() has no matching rollback"
    )


def test_concurrent_readers_are_not_broken_by_a_failing_writer(store):
    """The threaded server shares one connection; readers must stay correct."""
    reader_errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                store.list_workflows()
                store.dashboard_counts()
            except Exception as e:  # noqa: BLE001
                reader_errors.append(f"{type(e).__name__}: {e}")

    def failing_writer():
        while not stop.is_set():
            try:
                with store._tx() as cur:
                    cur.execute("SELECT 1 FROM table_that_does_not_exist")
            except Exception:
                pass

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=failing_writer))
    for t in threads:
        t.start()
    stop.wait(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert reader_errors == [], (
        f"concurrent readers saw {len(reader_errors)} errors, first: "
        f"{reader_errors[0] if reader_errors else ''}"
    )


def test_aborted_transaction_is_recovered_on_entry(store):
    """A connection left aborted by an external writer is repaired by _tx()."""
    # Bypass the helper to simulate a connection already stuck in the aborted
    # state (as an interrupted transaction elsewhere would leave it).
    with store._conn.cursor() as cur:
        with pytest.raises(Exception):
            cur.execute("SELECT 1 FROM table_that_does_not_exist")

    # _tx() rolls the aborted transaction back on entry, so this call succeeds.
    assert store.list_workflows() == []


def test_reported_error_string_does_not_appear(store):
    """The exact reported message must not be raised on a normal call."""
    with pytest.raises(Exception):
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM table_that_does_not_exist")
    try:
        store.list_tasks_all()
    except Exception as e:  # noqa: BLE001
        assert "InFailedSqlTransaction" not in str(e), (
            "the cascade error is still reachable on a clean call"
        )


def test_no_migration_is_missing_for_the_tables_the_code_uses():
    """Every table the store writes to exists after all migrations run.

    A missing table is one of the real root causes that triggers the cascade,
    so this pins the schema to the code.
    """
    import psycopg

    src = open(os.path.join(_ROOT, "orchestration", "cloud_store.py")).read()
    used = set(re.findall(r"INSERT INTO\s+([a-z_]+)", src, re.I))
    assert used, "expected the store to contain INSERT statements"

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
            present = {r[0] for r in cur.fetchall()}

    missing = sorted(used - present)
    assert missing == [], f"tables used by the store are not migrated: {missing}"


def test_verify_schema_names_missing_tables(store):
    """A forgotten migration is reported by name, not as a 500 later."""
    assert store.verify_schema() == []

    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE sessions CASCADE")
    try:
        assert "sessions" in store.verify_schema()
    finally:
        _reset_schema()
