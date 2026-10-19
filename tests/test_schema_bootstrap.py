"""Schema bootstrap and transaction-pooler behaviour.

Two properties that only matter in production, so they are asserted directly
rather than assumed:

* the process can create its own schema (the free plan has no shell for psql),
* behind the transaction pooler no server-side prepared statement is left to
  be reused by another client.

Both run against a real PostgreSQL; the module is skipped without one.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run schema bootstrap tests"
)

TABLES = (
    "workflows", "repositories", "tasks", "conversations",
    "task_events", "sr_tool_calls", "sessions", "login_attempts",
)


def _drop_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS schema_migrations, sr_tool_calls, task_events, "
                "conversations, tasks, repositories, workflows, sessions, "
                "login_attempts CASCADE"
            )


def _table_names() -> set[str]:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return {row[0] for row in cur.fetchall()}


def test_connect_creates_the_schema() -> None:
    _drop_schema()
    assert not (_table_names() & set(TABLES)), "precondition: schema is absent"

    store = PostgresStore(DSN)
    try:
        present = _table_names()
    finally:
        store.close()

    assert set(TABLES) <= present


def test_migrations_are_recorded_and_not_reapplied() -> None:
    _drop_schema()
    store = PostgresStore(DSN, auto_migrate=False)
    try:
        first = store.apply_migrations()
        assert first, "a fresh database should apply at least one migration"
        # The second run must be a no-op: the checksums are already recorded.
        assert store.apply_migrations() == []
    finally:
        store.close()


def test_auto_migrate_can_be_disabled() -> None:
    _drop_schema()
    os.environ["AUTO_MIGRATE"] = "false"
    try:
        store = PostgresStore(DSN)
        store.close()
    finally:
        os.environ.pop("AUTO_MIGRATE", None)

    assert not (_table_names() & set(TABLES)), (
        "AUTO_MIGRATE=false must leave schema management to the operator"
    )


def test_pooler_mode_leaves_no_prepared_statements() -> None:
    _drop_schema()
    os.environ["DATABASE_TRANSACTION_POOLER"] = "true"
    try:
        store = PostgresStore(DSN)
    finally:
        os.environ.pop("DATABASE_TRANSACTION_POOLER", None)

    try:
        # None is what psycopg uses to mean "never prepare server-side".
        assert store._conn.prepare_threshold is None
        # Execute the same statement more than the default threshold (5); with
        # preparation disabled the server must not hold a prepared statement,
        # because a pooled connection may be handed to another client.
        with store._conn.cursor() as cur:
            for _ in range(8):
                cur.execute("SELECT 1 AS n")
                cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM pg_prepared_statements")
            assert cur.fetchone()["n"] == 0
    finally:
        store.close()
