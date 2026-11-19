"""The self-hosting script: syntax, safety and a real backup/restore cycle.

``scripts/nexus.sh`` is the one command an operator runs on Debian. These cases
check the things that would hurt if wrong: the script parses, it never deletes a
live database, ``backup`` writes a dump and a checksum, and ``restore`` loads a
dump into a **scratch** database and drops it, leaving the live one untouched.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "nexus.sh")

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
requires_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")
requires_pg = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="pg_dump/pg_restore are required")


def test_the_script_exists_and_is_executable():
    assert os.path.isfile(_SCRIPT)
    assert os.access(_SCRIPT, os.X_OK), "scripts/nexus.sh must be executable"


def test_the_script_parses():
    out = subprocess.run(["bash", "-n", _SCRIPT], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_help_lists_every_documented_command():
    out = subprocess.run([_SCRIPT, "help"], capture_output=True, text=True)
    assert out.returncode == 0
    for command in ("setup", "start", "stop", "restart", "status", "logs",
                    "backup", "restore", "update", "tunnel"):
        assert command in out.stdout, f"{command} missing from help"


def test_an_unknown_command_is_refused():
    out = subprocess.run([_SCRIPT, "definitely-not-a-command"],
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert "unknown command" in (out.stderr + out.stdout)


def test_the_script_only_ever_drops_the_scratch_database():
    """`restore` must drop only the scratch database it created, never the live one."""
    src = open(_SCRIPT).read()
    drops = [ln.strip() for ln in src.splitlines()
             if "DROP DATABASE" in ln and not ln.lstrip().startswith("#")]
    assert drops, "expected the restore step to clean up its scratch database"
    for line in drops:
        assert "$testdb" in line, f"a DROP DATABASE that is not the scratch db: {line}"
        assert "db_url" not in line and "SUPABASE_DB_URL" not in line, (
            f"a DROP DATABASE that could target the live database: {line}")


def test_a_missing_env_file_is_refused(tmp_path):
    env = dict(os.environ)
    env["NEXUS_ENV_FILE"] = str(tmp_path / "does-not-exist.env")
    out = subprocess.run([_SCRIPT, "setup"], capture_output=True, text=True, env=env)
    assert out.returncode != 0
    assert "no " in (out.stderr + out.stdout).lower()


@requires_db
@requires_pg
def test_backup_then_restore_round_trip_leaves_the_live_database_alone(tmp_path):
    # The session may have rewritten the DSN to an isolated per-process database
    # (tests/_test_db), so read it now, not at import time.
    live_dsn = os.environ.get("TEST_SUPABASE_DB_URL") or DSN
    env_file = tmp_path / ".env"
    env_file.write_text(
        f'SUPABASE_DB_URL="{live_dsn}"\nWEB_AUTH_TOKEN=x\nAUTH_USERNAME=owner\n'
        'AUTH_PASSWORD_HASH=x\nAUTH_SESSION_SECRET=x\n')
    backups = tmp_path / "bk"
    backups.mkdir()
    env = dict(os.environ)
    env["NEXUS_ENV_FILE"] = str(env_file)
    env["NEXUS_BACKUP_DIR"] = str(backups)

    live_before = _table_count(live_dsn)

    backup = subprocess.run([_SCRIPT, "backup"], capture_output=True, text=True, env=env)
    assert backup.returncode == 0, backup.stderr
    dumps = list(backups.glob("nexus-*.dump"))
    assert len(dumps) == 1, dumps
    assert (backups / (dumps[0].name + ".sha256")).is_file(), "no checksum written"

    restore = subprocess.run([_SCRIPT, "restore", str(dumps[0])],
                             capture_output=True, text=True, env=env)
    assert restore.returncode == 0, restore.stderr
    assert "live database was not modified" in restore.stdout

    # The live database is exactly as it was: restore used a scratch copy.
    assert _table_count(live_dsn) == live_before


def _table_count(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = current_schema()")
            return cur.fetchone()[0]
