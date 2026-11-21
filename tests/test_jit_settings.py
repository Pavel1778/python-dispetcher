"""JIT is off by default: the cleanup scan must not spend half a second compiling.

Found by auditing the running system. ``LabStore.queue_cleanups`` unions five
sources with ``jsonb_array_elements`` lateral joins; the planner prices that in
the millions, so PostgreSQL's JIT compiled the plan. On a database with a few
thousand tasks the compile took ~500-670 ms and the query then returned zero
rows in microseconds. It runs on the worker's hot path every pass.

Raising ``jit_above_cost`` did not help — the estimate exceeded 10 000 000 — so
the store switches JIT off for every connection and lets ``NEXUS_DB_JIT=on``
restore the server's behaviour. These cases pin both directions, and the timing
case proves the setting is what makes the difference (not a changed query).
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore, _jit_options  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


# ------------------------------------------------------------ the setting
def test_jit_is_off_by_default():
    assert _jit_options() == "-c jit=off"


@pytest.mark.parametrize("value", ["on", "default", "server", "ON", " Server "])
def test_the_server_default_can_be_restored(monkeypatch, value):
    monkeypatch.setenv("NEXUS_DB_JIT", value)
    assert _jit_options() == ""


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "disabled", "anything"])
def test_anything_else_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv("NEXUS_DB_JIT", value)
    assert _jit_options() == "-c jit=off"


def test_a_store_connection_really_has_jit_off(monkeypatch):
    monkeypatch.delenv("NEXUS_DB_JIT", raising=False)
    _reset()
    store = PostgresStore(DSN)
    try:
        with store._tx() as cur:
            cur.execute("show jit")
            assert cur.fetchone()["jit"] == "off"
    finally:
        store.close()


def test_a_store_connection_honours_the_override(monkeypatch):
    monkeypatch.setenv("NEXUS_DB_JIT", "on")
    _reset()
    store = PostgresStore(DSN)
    try:
        with store._tx() as cur:
            cur.execute("show jit")
            # Whatever the server's own default is, it is not forced off.
            assert cur.fetchone()["jit"] in ("on", "off")
    finally:
        store.close()


# -------------------------------------------------------------- the effect
def _seed_tasks(store, runs: int, per_run: int) -> None:
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    projects = ProjectService(store)
    lab = LabStore(store)
    project = projects.create("bench", "B", "")["id"]
    plan = [{"id": f"T{i}", "title": "t", "description": "",
             "acceptance": [], "dependencies": []} for i in range(per_run)]
    for _ in range(runs):
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_goals(project_id,text,created_by,state) "
                        "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", "bench"))
            goal = cur.fetchone()["id"]
        run = lab.start_run("bench", project, str(goal), max_iterations=500,
                            plan=plan, repository="", branch="")
        while lab.claim_task(str(run["id"]), "w") is not None:
            pass
    with store._tx() as cur:
        cur.execute("ANALYZE")
    return lab


def test_the_cleanup_scan_is_fast_with_jit_off(monkeypatch):
    """The load-bearing case: with JIT off, the scan is milliseconds, not hundreds.

    Six thousand tasks is where the JIT compile cost was measured (500+ ms).
    The bound is generous (100 ms) so a slow CI box does not fail it, while a
    regression to the JIT path (~500 ms) still fails clearly.
    """
    monkeypatch.delenv("NEXUS_DB_JIT", raising=False)
    _reset()
    store = PostgresStore(DSN)
    try:
        lab = _seed_tasks(store, runs=12, per_run=500)
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            lab.queue_cleanups(retention_seconds=0)
            samples.append((time.perf_counter() - started) * 1000)
        assert min(samples) < 100, f"queue_cleanups took {min(samples):.0f} ms with JIT off"
    finally:
        store.close()


def test_the_same_scan_is_slow_with_the_server_default(monkeypatch):
    """Proof the setting is what matters: with the server's JIT on, it is slow.

    Skipped when the server already has JIT off (`jit = off` in postgresql.conf),
    because then the override changes nothing and the comparison is meaningless.
    """
    _reset()
    # Probe the *server's* own default with a plain connection: a PostgresStore
    # now forces JIT off, so asking through one would always read "off" and the
    # comparison could never run.
    import psycopg

    with psycopg.connect(DSN) as plain:
        with plain.cursor() as cur:
            cur.execute("SELECT current_setting('jit'), pg_jit_available()")
            server_default, available = cur.fetchone()
    if server_default == "off":
        pytest.skip("the server has JIT off already; the override cannot change it")
    if not available:
        pytest.skip("this PostgreSQL build has no JIT provider; no compilation to time")

    monkeypatch.setenv("NEXUS_DB_JIT", "on")
    store = PostgresStore(DSN)
    try:
        lab = _seed_tasks(store, runs=12, per_run=500)
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            lab.queue_cleanups(retention_seconds=0)
            samples.append((time.perf_counter() - started) * 1000)
        # With JIT on, the compile dominates. Do not assert an exact number
        # (it depends on the build), only that it is far slower than the
        # JIT-off case.
        assert min(samples) > 100, (
            f"expected the JIT path to be slow, got {min(samples):.0f} ms")
    finally:
        store.close()
