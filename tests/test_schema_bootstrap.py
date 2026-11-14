"""Schema bootstrap and transaction-pooler behaviour.

Two properties that only matter in production, so they are asserted directly
rather than assumed:

* the process can create its own schema (the free plan has no shell for psql),
* server-side prepared statements are off by default, so a pooled server session
  handed to the next process cannot collide on a prepared statement name.

Both run against a real PostgreSQL; the module is skipped without one. The
pooler-specific reproduction additionally needs ``TEST_POOLER_DSN``.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

# A DSN that goes through a transaction-mode pooler. Point this at a PgBouncer
# (or Supabase's pooler) to run the end-to-end reproduction; without it the
# pooler-specific test is skipped, because a direct connection cannot show the
# failure (the server session is fresh for every client).
POOLER_DSN = os.environ.get("TEST_POOLER_DSN")

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
                "conversation_routes, cloud_sandboxes, "
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


def test_an_edited_migration_is_refused_not_reapplied(tmp_path) -> None:
    """A migration already applied with one body must never be re-run with
    another. The old runner wrote the new checksum over the old and executed
    the changed SQL, so the recorded history claimed one thing and the schema
    was another. Editing an applied migration is now a refusal that names the
    file; a *new* file still applies normally."""
    from orchestration.cloud_store import MigrationError

    _drop_schema()
    store = PostgresStore(DSN, auto_migrate=False)
    try:
        first = store.apply_migrations()
        assert first, "a fresh database should apply at least one migration"
        name = first[0]

        # A directory with the same first migration, edited, plus nothing else.
        original = (Path(__file__).resolve().parent.parent / "migrations" / name)
        edited = tmp_path / name
        edited.write_text(original.read_text(encoding="utf-8")
                          + "\n-- an edit that must not be applied\n",
                          encoding="utf-8")
        with pytest.raises(MigrationError) as caught:
            store.apply_migrations(str(tmp_path))
        assert name in str(caught.value)
        assert "different body" in str(caught.value)

        # The recorded history still holds the original checksum (nothing ran).
        with store._tx() as cur:
            cur.execute("SELECT checksum FROM schema_migrations WHERE filename = %s",
                        (name,))
            recorded = cur.fetchone()["checksum"]
        assert recorded == hashlib.sha256(original.read_bytes()).hexdigest()

        # A genuinely new migration in the same directory still applies.
        (tmp_path / "9999_a_new_step.sql").write_text(
            "CREATE TABLE IF NOT EXISTS a_new_step (id int)", encoding="utf-8")
        # Remove the edited copy so the run sees only the new file.
        edited.unlink()
        assert "9999_a_new_step.sql" in store.apply_migrations(str(tmp_path))
        with store._tx() as cur:
            cur.execute("SELECT 1 FROM pg_tables WHERE tablename = 'a_new_step'")
            assert cur.fetchone() is not None
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


def test_prepared_statements_are_off_by_default() -> None:
    """Preparation is disabled unless the operator opts in with PSYCOPG_PREPARE.

    Behind a transaction-mode pooler a server session can be reused by the next
    process with a prepared statement still on it, so psycopg's default
    (prepare after the fifth execution) makes the *next* start fail with
    ``DuplicatePreparedStatement``. The first start is fine, which is why the
    default has to be the safe one.
    """
    _drop_schema()
    os.environ.pop("PSYCOPG_PREPARE", None)
    os.environ.pop("DATABASE_TRANSACTION_POOLER", None)
    store = PostgresStore(DSN)
    try:
        # None is what psycopg uses to mean "never prepare server-side".
        assert store.prepare_threshold is None
        # Every pooled connection must be opened with that policy, because a
        # pooled connection may be handed to another client.
        with store._connection() as conn:
            assert conn.prepare_threshold is None
            # Execute the same statement more than the default threshold (5);
            # with preparation disabled the server must not hold a prepared
            # statement, because a pooled connection may be handed to another
            # client.
            with conn.cursor() as cur:
                for _ in range(8):
                    cur.execute("SELECT 1 AS n")
                    cur.fetchone()
                cur.execute("SELECT count(*) AS n FROM pg_prepared_statements")
                assert cur.fetchone()["n"] == 0
            conn.rollback()
    finally:
        store.close()


def test_prepare_can_be_opted_in_for_a_direct_connection() -> None:
    """PSYCOPG_PREPARE=session restores psycopg's default for a direct DSN."""
    _drop_schema()
    os.environ["PSYCOPG_PREPARE"] = "session"
    try:
        store = PostgresStore(DSN)
    finally:
        os.environ.pop("PSYCOPG_PREPARE", None)
    try:
        assert store.prepare_threshold == 5
        with store._connection() as conn:
            assert conn.prepare_threshold == 5
    finally:
        store.close()


@pytest.mark.skipif(
    not POOLER_DSN,
    reason="set TEST_POOLER_DSN to a transaction-mode pooler to run this",
)
def test_restart_on_a_migrated_schema_behind_a_pooler() -> None:
    """End-to-end reproduction of the production startup failure.

    The schema must already be migrated: a fresh-schema start runs DDL, and
    psycopg reacts to ``ALTER``/``DROP`` by sending ``DEALLOCATE ALL``, which
    wipes the server-side prepared state and hides the bug. On a warm schema a
    start runs only SELECTs, so psycopg prepares the checksum query on the sixth
    execution and the pooler keeps it on the server session. The next start then
    tries to prepare the same generated name, ``_pg3_0``, and PostgreSQL rejects
    it -- which is the error seen in production, inside ``apply_migrations``.
    """
    _drop_schema()
    PostgresStore(DSN).close()  # migrate once; DDL clears any prepared state

    first = PostgresStore(POOLER_DSN)
    first.close()

    # This is the restart. With server-side preparation on, its apply_migrations
    # raises DuplicatePreparedStatement: prepared statement "_pg3_0" already exists.
    second = PostgresStore(POOLER_DSN)
    second.close()


def test_every_migration_applies_when_the_supabase_roles_exist() -> None:
    """Every migration must apply on a server that *has* the Supabase roles.

    ``0037`` declared its revoke loop variable as ``record`` instead of ``text``.
    A record loop variable renders as ``(authenticated)`` in ``format('%I', r)``,
    so the statement became ``REVOKE ... FROM "(authenticated)"`` and the whole
    migration failed with ``role "(authenticated)" does not exist`` on any server
    that has that role - i.e. on the Supabase production database, which is the
    one place the migration had to run. The defect was invisible locally because
    a plain PostgreSQL has neither ``anon`` nor ``authenticated``, so the loop
    body never executed.

    This test creates the two roles (dropping them again afterwards, so a plain
    PostgreSQL is left exactly as it was), applies every migration on a fresh
    schema, and asserts none of them raises. It fails on the ``record`` bug and
    passes once the variable is ``text``.
    """
    import psycopg

    _drop_schema()
    # Create the roles only if they do not already exist, and only for the
    # duration of this test. A database user without CREATEROLE cannot do it;
    # that is not the code under test, so the case is skipped, not failed.
    created: list[str] = []
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                for role in ("anon", "authenticated"):
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                    if cur.fetchone() is None:
                        cur.execute(f'CREATE ROLE "{role}" NOLOGIN')
                        created.append(role)
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("this database user cannot create the Supabase roles")
    try:
        store = PostgresStore(DSN, auto_migrate=False)
        try:
            applied = store.apply_migrations()
            assert applied, "a fresh schema should apply the migrations"
        finally:
            store.close()
    finally:
        # Drop only the roles this test created, and release anything the
        # migrations attached to them first (a policy or grant makes the role
        # own objects, and DROP ROLE refuses while they exist). A role that was
        # already there is left untouched.
        if created:
            with psycopg.connect(DSN, autocommit=True) as conn:
                with conn.cursor() as cur:
                    for role in created:
                        cur.execute(f'DROP OWNED BY "{role}"')
                        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
