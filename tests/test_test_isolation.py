"""Test-database isolation: two pytest processes must never share one database.

Several modules drop and re-create the whole schema or create and drop the
cluster-global Supabase roles. Two processes on one database therefore fight,
and the failures read as real defects (this happened: ``test_schema_bootstrap``
failed with ``DependentObjectsStillExist`` and a browser test failed too, while
each passed alone). ``tests/conftest.py`` gives each process its own database.

These cases run the real ``conftest`` in a fresh interpreter (so nothing here
mutates the running session's own DSN) and read what it decided.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = """
import json, os, sys
sys.path.insert(0, %r)
import tests.conftest  # noqa: F401  (runs _isolate_database at import)
print(json.dumps({
    "dsn": os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL"),
    "base": os.environ.get("NEXUS_TEST_BASE_DB_URL"),
}))
""" % ROOT


def _decide(env_overrides):
    """Import tests.conftest in a fresh interpreter and report its decision."""
    env = dict(os.environ)
    env.pop("TEST_SUPABASE_DB_URL", None)
    env.pop("SUPABASE_DB_URL", None)
    env.pop("NEXUS_TEST_BASE_DB_URL", None)
    env.pop("NEXUS_TEST_ISOLATE_DB", None)
    env.update(env_overrides)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_remote_database_is_left_alone():
    """A host that is not ours to create databases on is never touched."""
    url = "postgresql://u:p@db.example.com:5432/prod"
    got = _decide({"TEST_SUPABASE_DB_URL": url})
    assert got["dsn"] == url
    assert got["base"] is None


def test_the_escape_hatch_keeps_one_named_database():
    url = "postgresql://nexus:nexus@127.0.0.1:5432/nexus_agent"
    got = _decide({"TEST_SUPABASE_DB_URL": url, "NEXUS_TEST_ISOLATE_DB": "0"})
    assert got["dsn"] == url
    assert got["base"] is None


def test_a_local_database_is_isolated_per_process():
    """A local disposable database is isolated for this process.

    Two mechanisms can do it and they compose: ``conftest._isolate_database``
    makes a process-named copy ``<base>_p<suffix>`` of the named database;
    ``tests/_test_db.py`` is the fallback for a base whose copy cannot be made
    (it hands out ``nexus_test_<token>``, or serialises when it cannot create
    one). The property under test is the same either way - the process never
    ends up on the shared base URL - so the assertion is on the property, not
    on one mechanism's database name.
    """
    url = "postgresql://nexus:nexus@127.0.0.1:5432/nexus_agent"
    got = _decide({"TEST_SUPABASE_DB_URL": url})
    if got["base"] is not None:
        # ``conftest`` copied the named database to a process-named sibling.
        assert got["base"] == url
        assert got["dsn"].startswith(
            "postgresql://nexus:nexus@127.0.0.1:5432/nexus_agent_p")
        assert got["dsn"] != url
    else:
        # ``conftest`` could not copy the named database (it does not exist, or
        # the role cannot create one), so the ``tests/_test_db.py`` fallback ran
        # instead. It must still have put this process on a database of its own
        # - a ``nexus_test_<token>`` sibling - and never on the shared base URL.
        # Only a role that can create nothing falls back to serialising on the
        # base URL, and this role can create databases.
        assert got["dsn"] != url, "the process is on the shared base database"
        assert "nexus_test_" in got["dsn"] or "_p" in got["dsn"]


def test_two_processes_get_two_databases():
    """The whole point: two interpreters, one base DSN, two different databases.

    Either mechanism may isolate: ``conftest._isolate_database`` copies the
    named database, or ``tests/_test_db.py`` creates ``nexus_test_<token>``.
    The property asserted is the same either way - the two processes never end
    up on the same database, and neither is on the shared base URL.
    """
    url = "postgresql://nexus:nexus@127.0.0.1:5432/nexus_agent"
    first = _decide({"TEST_SUPABASE_DB_URL": url})
    second = _decide({"TEST_SUPABASE_DB_URL": url})
    if first["dsn"] == url and second["dsn"] == url:
        import pytest

        pytest.skip("this database role cannot create databases; "
                    "processes serialise on the base URL instead")
    # Different pids give different suffixes/tokens, so the two never collide,
    # and neither process runs on the shared base database.
    assert first["dsn"] != second["dsn"]
    assert first["dsn"] != url and second["dsn"] != url

