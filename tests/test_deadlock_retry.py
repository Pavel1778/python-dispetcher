"""A settle aborted by a PostgreSQL deadlock is retried exactly once.

Reproduction (2026-11-16, full-suite run ``/home/user/pytest_full5.log``):
``test_long_task_cases.py::test_after_the_run_has_ended_the_same_text_runs_again_from_the_same_plan``
failed once in ~40 runs with ``psycopg.errors.DeadlockDetected`` while the
member-facing cancel (``LabStore.set_run_state`` -> ``_apply_run_state``)
batch-updated ``nexus_run_tasks`` in a race with a worker settling a single
task. The aborted transaction commits nothing and every write inside it is
idempotent, so one retry is safe and removes the transient failure.

This test does not induce a real deadlock (it cannot be scheduled reliably);
it proves the retry contract deterministically: the first attempt raises the
real driver error, the second succeeds, the run settles, and no third attempt
is made.
"""
from __future__ import annotations

import os
import sys

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


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def lab():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "D")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    yield {"store": store, "svc": svc, "project": project, "goal": goal,
           "lab": LabStore(store)}
    store.close()


def test_cancel_survives_a_single_deadlock(lab, monkeypatch):
    from psycopg.errors import DeadlockDetected

    real = LabStore._apply_run_state
    calls = {"n": 0}

    def once_then_real(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeadlockDetected("deadlock detected (injected)")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LabStore, "_apply_run_state", once_then_real)
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    settled = lab["lab"].set_run_state("user:alice", lab["project"], run["id"],
                                       "cancelled")
    assert settled["state"] == "cancelled"
    assert calls["n"] == 2  # retried exactly once


def test_a_persistent_deadlock_still_surfaces(lab, monkeypatch):
    from psycopg.errors import DeadlockDetected

    def always(self, *args, **kwargs):
        raise DeadlockDetected("deadlock detected (injected)")

    monkeypatch.setattr(LabStore, "_apply_run_state", always)
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    with pytest.raises(DeadlockDetected):
        lab["lab"].set_run_state("user:alice", lab["project"], run["id"],
                                 "cancelled")
    assert lab["lab"].get_run("user:alice", lab["project"], run["id"])["state"] == "running"
