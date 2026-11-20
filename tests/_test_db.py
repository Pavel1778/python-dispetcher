"""Per-process test-database isolation.

Roughly seventy test modules drop and re-create the ``public`` schema of the
database named by ``TEST_SUPABASE_DB_URL`` (or ``SUPABASE_DB_URL``). Two
pytest processes sharing one such database drop each other's schema mid-run:
the tables vanish underneath the other suite, which is how two tests failed
only when the suites ran in parallel and passed when run one at a time.

Imported by ``tests/conftest.py`` at import time, i.e. before any test module
reads the environment. Two strategies, in order of preference:

1. **A database of its own.** When the configured server allows it, this
   process creates ``nexus_test_<token>`` and points both environment
   variables at it for the rest of the session. Parallel pytest runs -
   including ``pytest -n`` workers, which are separate processes - can then
   never see each other's schema. The database is dropped at session end.

2. **A lock, one process at a time.** When creating a database is not
   allowed (a transaction-mode pooler in front of the server, or a role
   without ``CREATEDB``), this process takes an exclusive ``flock`` on a
   lock file derived from the DSN and holds it for the session, so test
   suites against the same DSN *run one after another* instead of
   corrupting each other. That is the "safe parallelism is impossible, so
   the parallelism is bounded" case: parallel invocation is still accepted,
   it just waits its turn.

If neither is possible (no DSN, or no ``fcntl`` on this platform) the tests
run against the shared database unchanged, exactly as they did before; the
mode in effect is exported as ``NEXUS_TEST_DB_ISOLATED`` (``1`` isolated,
``0`` shared/serialised).
"""

from __future__ import annotations

import atexit
import hashlib
import os
import secrets
import sys

ISOLATED = "NEXUS_TEST_DB_ISOLATED"


def _base_dsn() -> str | None:
    return os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")


def _drop(admin_dsn: str, name: str) -> None:
    try:
        import psycopg

        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception as e:  # noqa: BLE001 - never fail the run over cleanup
        print(f"[tests] could not drop test database {name}: {e}", file=sys.stderr)


def _create(base: str) -> tuple[str, str, str]:
    """Create ``nexus_test_<token>``; return ``(isolated DSN, admin DSN, name)``."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(base)
    token = secrets.token_hex(6)
    name = f"nexus_test_{token}"
    admin_params = dict(params)
    admin_params["dbname"] = "postgres"
    admin_dsn = make_conninfo(**admin_params)
    quoted = '"' + name.replace('"', '""') + '"'
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {quoted}")
    params["dbname"] = name
    return make_conninfo(**params), admin_dsn, name


def _serialise(base: str) -> str:
    """Hold an exclusive per-DSN lock for the whole session."""
    try:
        import fcntl
        import tempfile
        from pathlib import Path
    except ImportError:
        print(
            "[tests] this platform has no fcntl: test suites against one shared "
            "database are not serialised - parallel runs can corrupt each other",
            file=sys.stderr,
        )
        return "shared"
    key = hashlib.sha256(base.encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"nexus-test-dsn-{key}.lock"
    print(
        f"[tests] cannot hand out per-process databases (no CREATE DATABASE "
        f"right); waiting for the lock on {path}",
        file=sys.stderr,
    )
    fd = open(path, "a+")  # noqa: SIM115 - held for the process lifetime
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    os.environ[ISOLATED] = "0"

    def _release() -> None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()

    atexit.register(_release)
    return "serialised"


def setup() -> str:
    base = _base_dsn()
    if not base:
        return "unset"
    try:
        isolated, admin_dsn, name = _create(base)
    except Exception as e:  # noqa: BLE001 - any failure falls back to the lock
        print(
            f"[tests] no per-process database ({type(e).__name__}: {e}); "
            f"falling back to one-process-at-a-time",
            file=sys.stderr,
        )
        return _serialise(base)
    os.environ["TEST_SUPABASE_DB_URL"] = isolated
    os.environ["SUPABASE_DB_URL"] = isolated
    os.environ[ISOLATED] = "1"
    atexit.register(_drop, admin_dsn, name)
    print(f"[tests] using per-process test database {name}", file=sys.stderr)
    return "isolated"


def reset_schema(dsn: str | None = None) -> None:
    """Reset the test database to an empty, fully-migrated schema.

    When all migrations in ``migrations/`` are already applied and every
    required table/column is present, wipes all user rows and resets sequences
    in ~12 ms instead of dropping and recreating ~40 tables and ~80 indexes
    (~200 ms). When any table, column, or migration is missing or altered,
    drops ``public`` and re-applies all migrations from scratch.
    """
    from pathlib import Path
    import psycopg
    from orchestration.cloud_store import PostgresStore, _read_migration_file

    target_dsn = dsn or _base_dsn()
    if not target_dsn:
        return
    mig_dir = Path(__file__).resolve().parent.parent / "migrations"
    files = sorted(mig_dir.glob("*.sql"))
    expected = {path.name: _read_migration_file(path)[1] for path in files}
    req_tables = list(PostgresStore.REQUIRED_COLUMNS.keys())

    with psycopg.connect(target_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            intact = False
            cur.execute(
                "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
            )
            row = cur.fetchone()
            if row and row[0]:
                cur.execute("SELECT filename, checksum FROM schema_migrations")
                recorded = {r[0]: r[1] for r in cur.fetchall()}
                if recorded == expected:
                    cur.execute(
                        "SELECT c.relname, a.attname "
                        "FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "JOIN pg_attribute a ON a.attrelid = c.oid "
                        "WHERE n.nspname = 'public' AND c.relname = ANY(%s) "
                        "  AND a.attnum > 0 AND NOT a.attisdropped",
                        (req_tables,),
                    )
                    cols: dict[str, set[str]] = {}
                    for tname, cname in cur.fetchall():
                        cols.setdefault(tname, set()).add(cname)
                    intact = all(
                        t in cols and all(c in cols[t] for c in req_cols)
                        for t, req_cols in PostgresStore.REQUIRED_COLUMNS.items()
                    )
            if intact:
                cur.execute(
                    """
                    DO $$
                    DECLARE
                        r record;
                        has_row boolean;
                    BEGIN
                        SET LOCAL session_replication_role = replica;
                        FOR r IN SELECT tablename FROM pg_tables
                                 WHERE schemaname = 'public' AND tablename <> 'schema_migrations'
                        LOOP
                            EXECUTE 'SELECT EXISTS (SELECT 1 FROM ' || quote_ident(r.tablename) || ' LIMIT 1)'
                                INTO has_row;
                            IF has_row THEN
                                EXECUTE 'DELETE FROM ' || quote_ident(r.tablename);
                            END IF;
                        END LOOP;
                        FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
                            EXECUTE 'ALTER SEQUENCE ' || quote_ident(r.sequencename) || ' RESTART WITH 1';
                        END LOOP;
                    END $$;
                    """
                )
                return

            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename text PRIMARY KEY,"
                "  checksum text NOT NULL,"
                "  applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in files:
                sql, checksum = _read_migration_file(path)
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations(filename, checksum) VALUES (%s, %s) "
                    "ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = now()",
                    (path.name, checksum),
                )


setup()
