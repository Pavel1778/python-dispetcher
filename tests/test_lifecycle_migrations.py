"""The lifecycle/pin migrations on a database that already holds something.

Three files are involved and two of them came from two independent lines of
work that both claimed the number ``0037``:

* ``0037_task_kind.sql`` - this branch's, for the ``completed_no_changes``
  task kind;
* ``0038_lifecycle.sql`` - this branch's, for archive/trash on projects and
  conversations;
* ``0039_conversation_pin.sql`` - the one column the other line
  (``arena/ab0d7665``) had and this one did not.

The runner keys on the *filename*, not the number, so distinct names coexist
and an already-migrated database re-runs nothing. These tests prove that on a
real PostgreSQL 17: a clean install has every column; an upgrade from a
schema without the three files gains them without losing a row; and applying
the files again changes nothing.
"""
from __future__ import annotations

import os
import sys

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
NEW = ("0037_task_kind.sql", "0038_lifecycle.sql", "0039_conversation_pin.sql")


def _apply(conn, names) -> None:
    for name in names:
        conn.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def before():
    """A database as it was before the lifecycle work: every migration except
    the three this test is about, plus one project and one conversation."""
    with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        _apply(conn, [n for n in sorted(os.listdir(_MIG))
                      if n.endswith(".sql") and n not in NEW])
        project = conn.execute("INSERT INTO nexus_projects(name) VALUES ('Shop') "
                               "RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                     "VALUES (%s, 'user:owner', 'owner')", (project,))
        chat = conn.execute(
            "INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
            "VALUES (%s, 'Чат', 'auto', 'user:owner') RETURNING id",
            (project,)).fetchone()["id"]
        yield conn, str(project), str(chat)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
    return {r["column_name"] for r in rows}


def test_the_three_files_are_distinct_and_the_numbers_do_not_collide():
    """Two lines of work both wrote a ``0037``; the files must have distinct
    names so the runner (which keys on the filename) keeps both."""
    names = sorted(n for n in os.listdir(_MIG) if n.startswith("0037"))
    assert names == ["0037_task_kind.sql"], names
    assert "0038_lifecycle.sql" in os.listdir(_MIG)
    assert "0039_conversation_pin.sql" in os.listdir(_MIG)


def test_an_upgrade_gains_the_columns_and_keeps_every_row(before):
    conn, project, chat = before
    # Before: none of the new columns exist.
    assert "deleted_at" not in _columns(conn, "nexus_projects")
    assert "pinned" not in _columns(conn, "nexus_conversations")
    assert "kind" not in _columns(conn, "nexus_run_tasks")

    _apply(conn, NEW)

    assert {"archived_at", "deleted_at"} <= _columns(conn, "nexus_projects")
    assert {"deleted_at", "pinned"} <= _columns(conn, "nexus_conversations")
    assert "kind" in _columns(conn, "nexus_run_tasks")
    # The existing rows are still there and readable, with the additive
    # defaults (NULL / false / 'implementation').
    row = conn.execute("SELECT name, archived_at, deleted_at FROM nexus_projects "
                       "WHERE id=%s", (project,)).fetchone()
    assert row["name"] == "Shop" and row["archived_at"] is None and row["deleted_at"] is None
    chat_row = conn.execute("SELECT title, pinned, deleted_at FROM nexus_conversations "
                            "WHERE id=%s", (chat,)).fetchone()
    assert chat_row["title"] == "Чат" and chat_row["pinned"] is False


def test_applying_the_three_files_again_changes_nothing(before):
    conn, project, chat = before
    _apply(conn, NEW)
    conn.execute("UPDATE nexus_conversations SET pinned=true WHERE id=%s", (chat,))
    conn.execute("UPDATE nexus_projects SET archived_at=now() WHERE id=%s", (project,))

    def snapshot():
        return (conn.execute("SELECT name, archived_at, deleted_at FROM nexus_projects "
                             "WHERE id=%s", (project,)).fetchone(),
                conn.execute("SELECT title, pinned, deleted_at FROM nexus_conversations "
                             "WHERE id=%s", (chat,)).fetchone())

    first = snapshot()
    _apply(conn, NEW)
    _apply(conn, NEW)
    assert snapshot() == first


def test_the_store_applies_all_of_them_on_a_fresh_schema():
    """The runner end to end: a clean database, every migration, and the schema
    guard is satisfied (the columns the code reads all exist)."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    store = PostgresStore(DSN)                     # applies migrations on connect
    try:
        assert store.verify_schema() == []
        with store._tx() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            applied = {r["filename"] for r in cur.fetchall()}
        assert set(NEW) <= applied
        # A second connect re-runs nothing.
        again = PostgresStore(DSN)
        try:
            assert again.apply_migrations() == []
        finally:
            again.close()
    finally:
        store.close()
