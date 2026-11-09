"""Backup/restore drill: a real pg_dump -> pg_restore into a fresh database.

This is the automated proxy for the operator procedure in DEPLOY.md. It proves
that a dump of a populated NEXUS database restores into an empty one with the
schema guard satisfied and the data intact — so a restore is not a leap of
faith.

It needs the PostgreSQL client tools (`pg_dump`, `pg_restore`, `createdb`) and a
disposable server, both available in CI. It is skipped when they are absent or
when ``TEST_SUPABASE_DB_URL`` is unset.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

_HAVE_TOOLS = all(shutil.which(t) for t in ("pg_dump", "pg_restore", "createdb", "dropdb"))
pytestmark = pytest.mark.skipif(
    not DSN or not _HAVE_TOOLS,
    reason="set TEST_SUPABASE_DB_URL and install the PostgreSQL client tools",
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _swap_db(dsn: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(dsn)
    return urllib.parse.urlunsplit(parts._replace(path="/" + dbname))


def _run(*cmd: str, env_extra: dict | None = None) -> None:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise AssertionError(
            f"{cmd[0]} failed ({result.returncode}): {result.stderr[:400]}"
        )


def test_dump_and_restore_into_a_fresh_database():
    _reset_schema()
    source = PostgresStore(DSN)
    restore_db = "nexus_restore_" + uuid.uuid4().hex[:8]
    dump_path = f"/tmp/nexus-drill-{uuid.uuid4().hex[:8]}.dump"
    parts = urllib.parse.urlsplit(DSN)
    env = {"PGPASSWORD": urllib.parse.unquote(parts.password or "")}
    try:
        # Populate the source with a project, a repository link and a task, so
        # the dump has real NEXUS data to carry.
        source.add_repository("acme/repo", allowed_branches=["main"])
        with source._tx() as cur:
            cur.execute("INSERT INTO nexus_projects(name) VALUES ('Drill') RETURNING id")
            pid = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                        "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
        task = source.submit_task("drill work", project_id=pid,
                                  selected_repository="acme/repo", selected_branch="main")

        # Dump the whole database in custom format.
        _run("pg_dump", "--format=custom", "--file", dump_path,
             "--no-owner", "--no-privileges", DSN, env_extra=env)

        # Create an empty database and restore into it.
        _run("createdb", "-h", parts.hostname or "127.0.0.1",
             "-p", str(parts.port or 5432), "-U", parts.username or "postgres",
             restore_db, env_extra=env)
        try:
            _run("pg_restore", "--dbname", _swap_db(DSN, restore_db),
                 "--no-owner", "--no-privileges", "--exit-on-error",
                 dump_path, env_extra=env)
            restored = PostgresStore(_swap_db(DSN, restore_db))
            try:
                # The schema guard is satisfied and the data survived.
                assert restored.verify_schema() == []
                assert restored.get_task(task.id).goal == "drill work"
                assert restored.project_repository_linked(pid, "acme/repo")
                with restored._tx() as cur:
                    cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
                    recorded = {r["filename"] for r in cur.fetchall()}
                on_disk = {f for f in os.listdir(_MIG) if f.endswith(".sql")}
                assert recorded == on_disk
            finally:
                restored.close()
        finally:
            _run("dropdb", "--if-exists", "-h", parts.hostname or "127.0.0.1",
                 "-p", str(parts.port or 5432), "-U", parts.username or "postgres",
                 restore_db, env_extra=env)
    finally:
        source.close()
        if os.path.exists(dump_path):
            os.unlink(dump_path)


def test_secret_tables_are_rls_protected():
    """conversation_routes/cloud_sandboxes hold a session key; they must be locked.

    RLS enabled with no client policy, and no PUBLIC grant, so a Supabase
    anon/authenticated role cannot read a sandbox session key through PostgREST.
    """
    _reset_schema()
    store = PostgresStore(DSN)
    try:
        with store._tx() as cur:
            for table in ("conversation_routes", "cloud_sandboxes"):
                cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid=%s::regclass",
                            (table,))
                assert cur.fetchone()["relrowsecurity"], table
                cur.execute(
                    "SELECT count(*) AS n FROM pg_class c, aclexplode(c.relacl) a "
                    "WHERE c.oid=%s::regclass AND a.grantee=0", (table,))
                assert cur.fetchone()["n"] == 0, table
    finally:
        store.close()
