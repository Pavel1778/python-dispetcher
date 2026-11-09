"""Durable lab store: run lifecycle, atomic claim, dependency gating, recovery.

Real PostgreSQL. These tests exercise the invariants the autonomous loop depends
on, so a regression here would silently corrupt a run.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
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
def store():
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def lab(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Lab")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    return {"store": store, "lab": LabStore(store), "project": project, "goal": goal,
            "svc": svc}


# ------------------------------------------------------------------------ runs
def test_start_run_creates_one_active_run(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    assert run["state"] == "running" and run["iteration"] == 0
    with pytest.raises(ValueError):
        lab["lab"].start_run("user:alice", lab["project"], lab["goal"])  # second refused


def test_run_is_project_scoped(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    other = str(lab["svc"].create("user:bob", "Other")["id"])
    with pytest.raises(NotFound):
        lab["lab"].get_run("user:bob", other, run["id"])


def test_terminal_run_state_mirrors_onto_the_goal(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].set_run_state("user:alice", lab["project"], run["id"], "completed")
    with lab["store"]._tx() as cur:
        cur.execute("SELECT state FROM nexus_goals WHERE id=%s", (lab["goal"],))
        assert cur.fetchone()["state"] == "completed"


# ----------------------------------------------------------------------- tasks
def test_claim_requires_verified_dependencies(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A")
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="b", title="B",
                        dependencies=["a"])
    first = lab["lab"].claim_task(run["id"], "w1")
    assert first["task_key"] == "a"  # b is gated on a
    # b is not claimable while a is unverified.
    assert lab["lab"].claim_task(run["id"], "w1") is None
    lab["lab"].record_verification(first["id"], verdict="completed_verified",
                                   evidence={"by": "test"})
    second = lab["lab"].claim_task(run["id"], "w1")
    assert second["task_key"] == "b"


def test_unverified_dependency_does_not_unblock(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A")
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="b", title="B",
                        dependencies=["a"])
    a = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].record_verification(a["id"], verdict="completed_unverified",
                                   evidence={"by": "agent"})
    assert lab["lab"].claim_task(run["id"], "w1") is None  # b still gated


def test_concurrent_claims_never_take_the_same_task(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    for i in range(6):
        lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key=f"t{i}",
                            title=f"T{i}")

    def claim(_):
        other = PostgresStore(DSN, auto_migrate=False)
        try:
            t = LabStore(other).claim_task(run["id"], "w")
            return t["id"] if t else None
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = [i for i in pool.map(claim, range(12)) if i]
    assert len(ids) == len(set(ids)) == 6  # six tasks, six distinct claims


def test_claim_respects_attempt_budget(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A",
                        max_attempts=1)
    first = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].release_task(first["id"], error="boom", requeue=True)
    assert lab["lab"].claim_task(run["id"], "w1") is None  # attempt budget spent


def test_expired_lease_is_reclaimed(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A")
    claimed = lab["lab"].claim_task(run["id"], "w1", lease_seconds=0)
    assert claimed["state"] == "dispatched"
    reclaimed = lab["lab"].reclaim_expired(run["id"])
    assert reclaimed and reclaimed[0]["state"] == "pending"
    assert lab["lab"].claim_task(run["id"], "w2")["task_key"] == "a"


def test_expired_lease_after_max_attempts_fails_the_task(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A",
                        max_attempts=1)
    lab["lab"].claim_task(run["id"], "w1", lease_seconds=0)
    reclaimed = lab["lab"].reclaim_expired(run["id"])
    assert reclaimed[0]["state"] == "failed"


def test_verification_verdicts_are_recorded(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A")
    t = lab["lab"].claim_task(run["id"], "w1")
    task = lab["lab"].record_verification(t["id"], verdict="partial",
                                          evidence={"reason": "half"}, detail={"x": 1})
    assert task["state"] == "partial" and task["verification"] == {"x": 1}
    with pytest.raises(ValueError):
        lab["lab"].record_verification(t["id"], verdict="nope", evidence={})


def test_all_verified_and_has_pending(lab):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="a", title="A")
    assert lab["lab"].has_pending(run["id"]) is True
    assert lab["lab"].all_verified(run["id"]) is False
    t = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].record_verification(t["id"], verdict="completed_verified", evidence={})
    assert lab["lab"].has_pending(run["id"]) is False
    assert lab["lab"].all_verified(run["id"]) is True


def test_notifications_are_recorded_and_scoped(lab):
    lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    notes = lab["lab"].notifications("user:alice", lab["project"])
    assert any(n["kind"] == "goal_started" for n in notes)
    lab["svc"].create("user:bob", "O")  # bob is a real principal with no access
    with pytest.raises(NotFound):
        lab["lab"].notifications("user:bob", lab["project"])
