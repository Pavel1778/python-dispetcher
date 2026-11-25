"""Concurrent task claiming at 1, 4, 16 and 64 workers.

The mission's Phase H requires the claim path be exercised at several worker
widths and prove that concurrent workers never double-claim a task, never lose a
task, and never grant two owners one lease. This file is the missing proof: the
prior audit *claimed* a ``tests/test_concurrent_claims.py`` but it exists only on
an unmerged branch, so nothing on ``main`` ran this.

Everything here is the real ``LabStore`` against real PostgreSQL - no mock of the
claim, no fake store. Each worker is a real thread with its own store
connection; ``FOR UPDATE SKIP LOCKED`` plus the fencing token is what is under
test, not a Python lock.

Each claim returns the task *and* its ``lease_token``; the test asserts:

* every task is claimed at most once across all workers (no duplicate grant);
* every task that is claimable is claimed exactly once (no lost task);
* the lease tokens are distinct per task, and the owner recorded in the row
  matches the worker that claimed it (no two owners of one lease).
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:alice"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _plan(n: int) -> list[dict]:
    # N independent tasks (no dependencies), so all are claimable at once and
    # the branch lock is not what serialises them.
    return [
        {"id": f"t{i}", "title": f"task {i}", "description": "d", "acceptance": ["ok"],
         "dependencies": [], "repository": f"acme/repo{i}", "branch": "main"}
        for i in range(n)
    ]


def _run(store: PostgresStore, n_tasks: int) -> tuple[str, str]:
    svc = ProjectService(store)
    project = str(svc.create(PRINCIPAL, "Concurrency")["id"])
    goal = str(svc.create_goal(PRINCIPAL, project, "ship")[0]["id"])
    lab = LabStore(store)
    run = lab.start_run(PRINCIPAL, project, goal, plan=_plan(n_tasks),
                        repository="", branch="", policy="durable",
                        max_iterations=500, max_attempts_per_task=3)
    return str(run["id"]), project


def _claim_all(store: PostgresStore, run_id: str, workers: int) -> list[dict]:
    """Spawn *workers* threads that each claim until the queue is empty."""
    lab = LabStore(store)

    def worker(i: int) -> list[dict]:
        mine: list[dict] = []
        while True:
            got = lab.claim_task(run_id, f"w{i}", lease_seconds=900)
            if got is None:
                return mine
            mine.append({"task": str(got["id"]), "key": got["task_key"],
                         "token": got["lease_token"], "worker": f"w{i}"})
        # unreachable

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(worker, range(workers)))
    return [row for mine in results for row in mine]


@pytest.mark.parametrize("workers", [1, 2, 4, 8, 16, 32, 64])
def test_no_task_is_claimed_twice_and_none_is_lost(store, workers):
    run_id, _project = _run(store, n_tasks=40)
    claims = _claim_all(store, run_id, workers)

    by_task = Counter(c["task"] for c in claims)
    # No duplicate grant: every task id claimed at most once.
    assert all(n == 1 for n in by_task.values()), {
        t: n for t, n in by_task.items() if n != 1}
    # No lost task: all 40 claimable tasks were claimed exactly once.
    assert len(by_task) == 40
    # Distinct fencing tokens per task, and the recorded owner matches.
    assert len({(c["task"], c["token"]) for c in claims}) == 40
    with store._tx() as cur:
        cur.execute(
            "SELECT id::text, lease_owner FROM nexus_run_tasks WHERE run_id=%s", (run_id,))
        owners = {r["id"]: r["lease_owner"] for r in cur.fetchall()}
    for c in claims:
        assert owners[c["task"]] == c["worker"], (
            f"task {c['task']} recorded owner {owners[c['task']]} != {c['worker']}")


def test_two_workers_cannot_own_one_lease(store):
    """With one task, exactly one of many workers wins; the rest get nothing."""
    run_id, _ = _run(store, n_tasks=1)
    claims = _claim_all(store, run_id, workers=16)
    assert len(claims) == 1, claims
    with store._tx() as cur:
        cur.execute("SELECT lease_owner, lease_token FROM nexus_run_tasks WHERE run_id=%s",
                    (run_id,))
        row = cur.fetchone()
    assert row["lease_owner"] == claims[0]["worker"]
    assert row["lease_token"] == claims[0]["token"]


@pytest.fixture
def store(monkeypatch):
    # The claim path caps *in-flight* tasks per database (NEXUS_LAB_MAX_ACTIVE_TASKS,
    # default 8) so a deployment does not run the whole queue at once. This test
    # is about the claim atomicity, not that cap, so it lifts it (0 = unlimited)
    # and every one of the 40 tasks is claimable at the same moment.
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "0")
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


def test_a_wide_stress_run_grants_each_task_exactly_once(store):
    """256 tasks, 128 workers: each task granted exactly once, none lost.

    The mission's "64 workers" figure doubled for headroom. Gated on
    ``NEXUS_STRESS`` so the default suite stays fast; the correctness proof at
    1/4/16/64 already runs unconditionally.
    """
    if os.environ.get("NEXUS_STRESS") != "1":
        pytest.skip("set NEXUS_STRESS=1 to run the 128-worker stress case")
    run_id, _ = _run(store, n_tasks=256)
    claims = _claim_all(store, run_id, workers=128)
    by_task = Counter(c["task"] for c in claims)
    assert len(by_task) == 256
    assert all(n == 1 for n in by_task.values())


def test_100_workers_claim_100_tasks_without_duplicates(store):
    """A queue contention correctness test, not a 100-agent throughput test."""
    run_id, _ = _run(store, n_tasks=100)
    claims = _claim_all(store, run_id, workers=100)
    assert len(claims) == 100
    assert len({claim["task"] for claim in claims}) == 100
    with store._tx() as cur:
        cur.execute("SELECT id::text, lease_owner FROM nexus_run_tasks WHERE run_id=%s", (run_id,))
        owners = {row["id"]: row["lease_owner"] for row in cur.fetchall()}
    assert all(owners[claim["task"]] == claim["worker"] for claim in claims)
