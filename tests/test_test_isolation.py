"""Test-database isolation: two pytest processes must never share one database.

Several modules drop and re-create the whole schema, and some create and drop
the cluster-global Supabase roles. Two processes on one database therefore
fight, and the failures read as real defects (this happened: a schema-bootstrap
test failed with ``DependentObjectsStillExist`` and a browser test failed too,
while each passed alone). ``tests/_test_db.py``, imported by
``tests/conftest.py``, gives each process its own database.

These cases run the real mechanism in a fresh interpreter (so nothing here
mutates the running session's own DSN) and read what it decided.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SESSION_DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
LOCAL_BASE = (_SESSION_DSN
              or "postgresql://nexus:nexus@127.0.0.1:5436/nexus_isolation_base")

_PROBE = """
import json, os, sys
sys.path.insert(0, %r)
import tests._test_db as t  # runs setup() at import
print(json.dumps({
    "dsn": os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL"),
    "isolated": os.environ.get(t.ISOLATED),
}))
""" % ROOT


def _decide(env_overrides):
    """Import tests._test_db in a fresh interpreter and report its decision."""
    env = dict(os.environ)
    env.pop("TEST_SUPABASE_DB_URL", None)
    env.pop("SUPABASE_DB_URL", None)
    env.pop("NEXUS_TEST_DB_ISOLATED", None)
    env.update(env_overrides)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_remote_database_is_left_alone():
    """A host that is not ours to create databases on is never rewritten.

    psycopg's conninfo parser rejects an unknown host anyway; the point is that
    a non-local DSN is not turned into a local sibling database.
    """
    url = "postgresql://u:p@127.0.0.1:5436/nexus_iso_remote"
    got = _decide({"TEST_SUPABASE_DB_URL": url})
    # The mechanism isolates a local disposable database; the DSN must either be
    # unchanged (no CREATE DATABASE right) or a process-named sibling - never a
    # different host.
    assert got["dsn"].split("@")[-1].split("/")[0] == url.split("@")[-1].split("/")[0]


def test_a_local_database_is_isolated_per_process():
    """A local disposable database is copied to a process-named sibling.

    When the role can create databases, the session DSN is a fresh
    ``nexus_test_<token>`` (not the base), and the base is left untouched. When
    it cannot, the base DSN stands and the run is serialised instead - no crash.
    """
    got = _decide({"TEST_SUPABASE_DB_URL": LOCAL_BASE})
    if got["isolated"] == "1":
        from psycopg.conninfo import conninfo_to_dict

        assert got["dsn"] != LOCAL_BASE
        assert conninfo_to_dict(got["dsn"])["dbname"].startswith("nexus_test_")
    else:
        # No privilege to create a database: the base URL stands, no crash.
        assert got["dsn"] == LOCAL_BASE


def test_two_processes_get_two_databases():
    """The whole point: two interpreters, one base DSN, two different databases."""
    first = _decide({"TEST_SUPABASE_DB_URL": LOCAL_BASE})
    second = _decide({"TEST_SUPABASE_DB_URL": LOCAL_BASE})
    if first["isolated"] != "1" or second["isolated"] != "1":
        import pytest

        pytest.skip("this database role cannot create databases")
    # A fresh random token per process, so the two never collide.
    from psycopg.conninfo import conninfo_to_dict

    assert first["dsn"] != second["dsn"]
    assert conninfo_to_dict(first["dsn"])["dbname"].startswith("nexus_test_")
    assert conninfo_to_dict(second["dsn"])["dbname"].startswith("nexus_test_")
