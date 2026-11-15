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
    # ``conftest`` may have isolated this process already (a process-named copy
    # of a local disposable database); see ``tests/conftest._isolate_database``.
    # Defer to it rather than copy the copy.
    if os.environ.get("NEXUS_TEST_BASE_DB_URL"):
        return "deferred"
    # The shared escape hatch: an operator who asks for one named database gets
    # one, whichever mechanism would otherwise isolate.
    if os.environ.get("NEXUS_TEST_ISOLATE_DB", "").strip().lower() in (
            "0", "false", "no", "off"):
        return "unset"
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


setup()
