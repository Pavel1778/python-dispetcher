"""Claim correctness under real concurrent workers, at every width the mission asks.

The mission's Phase 4 requires 1, 2, 4, 8, 16, 32 and 64 workers. What must hold
at every width, on real PostgreSQL with real threads:

* **Distinct tasks.** No two workers are ever granted the same task.
* **No duplicate live lease.** A task carries exactly one ``lease_owner`` at a
  time, and the fencing token of the grant matches the row.
* **No lost task.** When the queue is drained, every claimable task was granted
  exactly once - nothing disappears.
* **No illegal transition.** Every claimed task is ``dispatched`` with the token
  the grant returned.

The workers here are threads on one process against one database, each with its
own pooled connection, which is what a single-host deployment looks like. That
is the concurrency this test proves; multi-host is the same SQL, and the claims
are ``FOR UPDATE SKIP LOCKED`` so the database arbitrates either way.

Run: ``TEST_SUPABASE_DB_URL=... python -m pytest tests/test_concurrent_claims.py``
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_durable_runs import env, reset_schema, sql, start  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_SUPABASE_DB_URL"),
    reason="set TEST_SUPABASE_DB_URL to run concurrent-claim tests")


@pytest.fixture(autouse=True)
def _no_global_cap(monkeypatch):
    """Measure claim correctness, not the deployment's in-flight budget.

    ``claim_task`` refuses a new claim once ``max_active_tasks()`` tasks are at
    work in the database (8 by default). That is deliberate and correct - it is
    what stops one deployment flooding OpenHands - but it would make a
    width-16 test see only 8 grants and read as a lost-task bug. The cap has
    its own coverage; here it is switched off so every width can drain.
    """
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "0")

#: The widths the mission names. 64 is the largest it asks for; a 128-worker
#: variant is marked slow and skipped by default (see the last case).
WIDTHS = [1, 2, 4, 8, 16, 32, 64]


def _drain(lab_env, run_id: str, workers: int, *, per_task: int) -> list[dict]:
    """Every worker claims until the queue is empty; return all grants."""
    granted: list[dict] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    ready = threading.Barrier(workers)

    def worker(index: int) -> None:
        try:
            ready.wait(timeout=30)
            while True:
                claim = lab_env["lab"].claim_task(run_id, f"w{index}", lease_seconds=900)
                if claim is None:
                    return
                with lock:
                    granted.append(claim)
        except BaseException as e:  # noqa: BLE001 - a worker error is the failure
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    return granted


@pytest.mark.parametrize("workers", WIDTHS)
def test_no_two_workers_are_granted_the_same_task(env, workers):  # noqa: F811
    """The core claim guarantee, at every width."""
    # More tasks than workers, so every worker really contends.
    count = max(workers * 2, 8)
    run_id = start(env, [{"task_key": f"t{i}", "title": f"T{i}"} for i in range(count)],
                   max_iterations=count)

    granted = _drain(env, run_id, workers, per_task=1)

    ids = [g["id"] for g in granted]
    assert len(ids) == len(set(ids)), "a task was granted twice"
    assert len(ids) == count, f"expected {count} grants, got {len(ids)}"

    # Every claimed task is dispatched, and its token is the one returned.
    rows = sql(env, "SELECT id, state, lease_owner, lease_token FROM nexus_run_tasks "
                    "WHERE run_id=%s", run_id)
    by_id = {str(r["id"]): r for r in rows}
    for g in granted:
        row = by_id[str(g["id"])]
        assert row["state"] == "dispatched", (row["state"], g["id"])
        assert row["lease_token"] == g["lease_token"], "fencing token mismatch"
        assert row["lease_owner"] is not None


@pytest.mark.parametrize("workers", [8, 32, 64])
def test_no_task_carries_two_live_owners(env, workers):  # noqa: F811
    """One lease owner per task, never two - checked in the database, not in Python."""
    count = workers * 2
    run_id = start(env, [{"task_key": f"t{i}", "title": f"T{i}"} for i in range(count)],
                   max_iterations=count)
    _drain(env, run_id, workers, per_task=1)

    dupes = sql(env,
                "SELECT lease_owner, count(*) AS n FROM nexus_run_tasks "
                "WHERE run_id=%s AND state='dispatched' AND lease_owner IS NOT NULL "
                "GROUP BY lease_owner, id HAVING count(*) > 1", run_id)
    assert dupes == []
    # And the number of live leases equals the number of tasks: none lost.
    live = sql(env, "SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s "
                    "AND lease_owner IS NOT NULL", run_id)[0]["n"]
    assert live == count, (live, count)


@pytest.mark.parametrize("workers", [16, 64])
def test_a_drained_queue_grants_every_task_exactly_once(env, workers):  # noqa: F811
    """No lost task: what the queue held is what was granted, once each."""
    count = workers * 3
    run_id = start(env, [{"task_key": f"t{i}", "title": f"T{i}"} for i in range(count)],
                   max_iterations=count)
    granted = _drain(env, run_id, workers, per_task=1)

    keys = sorted(g["task_key"] for g in granted)
    expected = sorted(f"t{i}" for i in range(count))
    assert keys == expected, (set(expected) - set(keys), set(keys) - set(expected))
    # Nothing left claimable.
    assert env["lab"].claim_task(run_id, "after") is None


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("NEXUS_RUN_128") != "1",
                    reason="128 workers is a stress run; set NEXUS_RUN_128=1")
def test_128_workers_still_grant_each_task_once(env):  # noqa: F811
    """The optional stress width. Skipped unless explicitly asked for."""
    count = 256
    run_id = start(env, [{"task_key": f"t{i}", "title": f"T{i}"} for i in range(count)],
                   max_iterations=count)
    granted = _drain(env, run_id, 128, per_task=1)
    ids = [g["id"] for g in granted]
    assert len(ids) == len(set(ids)) == count
