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

from orchestration.cloud_store import InvalidTransition, NotFound, PostgresStore  # noqa: E402
from orchestration.nexus_lab_store import _RUN_ENTERED_FROM, LabStore  # noqa: E402
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


# ------------------------------------------------------------- run state machine
RUN_STATES = sorted(_RUN_ENTERED_FROM)


@pytest.mark.parametrize("source", RUN_STATES)
def test_a_run_moves_only_along_the_table(lab, source):
    """Every (from, to) pair: either the table allows it, or nothing changes.

    Before the table existed any state could follow any other. A cancelled run
    could be "resumed" - and with another run of the same goal alive that broke
    the one-active-run index instead of being refused.
    """
    for target in RUN_STATES:
        with lab["store"]._tx() as cur:
            cur.execute("DELETE FROM nexus_runs")
        run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
        task = lab["lab"].add_task("user:alice", lab["project"], run["id"],
                                   task_key="a", title="A")
        with lab["store"]._tx() as cur:
            # A blocked run needs something a resume could retry.
            cur.execute("UPDATE nexus_run_tasks SET state='failed', attempt=1 WHERE id=%s",
                        (task["id"],))
            cur.execute("UPDATE nexus_runs SET state=%s WHERE id=%s", (source, run["id"]))
        allowed = target == source or source in _RUN_ENTERED_FROM[target]
        if allowed:
            moved = lab["lab"].set_run_state("user:alice", lab["project"], run["id"], target)
            assert moved["state"] == target, (source, target)
        else:
            with pytest.raises(InvalidTransition):
                lab["lab"].set_run_state("user:alice", lab["project"], run["id"], target)
            assert lab["lab"].set_run_state_internal(run["id"], target) is None
            assert lab["lab"].get_run("user:alice", lab["project"], run["id"])["state"] == source


def test_the_table_has_no_way_out_of_a_finished_run():
    for target, sources in _RUN_ENTERED_FROM.items():
        assert not sources & {"completed", "failed", "cancelled"}, target


def test_a_run_and_its_plan_are_stored_together_or_not_at_all(lab):
    plan = [{"id": "a", "title": "A", "acceptance": ["x"], "dependencies": []},
            {"id": "b", "title": "B" * 201, "acceptance": [], "dependencies": ["a"]}]   # too long
    with pytest.raises(Exception, match="nexus_run_tasks_title_check"):
        lab["lab"].start_run("user:alice", lab["project"], lab["goal"], plan=plan)
    assert lab["lab"].list_runs("user:alice", lab["project"]) == []
    with lab["store"]._tx() as cur:
        cur.execute("SELECT state, active_run_id FROM nexus_goals WHERE id=%s", (lab["goal"],))
        goal = cur.fetchone()
    assert goal["state"] == "draft" and goal["active_run_id"] is None


def test_a_plan_stored_in_one_go_keeps_its_order(lab):
    """One transaction means one ``now()`` for every row; the order must not
    fall back to the random task id."""
    keys = [f"t{i:02d}" for i in range(15)]
    plan = [{"id": key, "title": key, "acceptance": [], "dependencies": []} for key in keys]
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"], plan=plan,
                               repository="acme/repo", branch="main", max_attempts_per_task=2)
    tasks = lab["lab"].list_tasks("user:alice", lab["project"], run["id"])
    assert [t["task_key"] for t in tasks] == keys
    assert {(t["repository"], t["branch"], t["max_attempts"]) for t in tasks} == {
        ("acme/repo", "main", 2)}
    # Claimed in that order too. One task at a time works on a branch, so each
    # is settled before the next is asked for.
    order = []
    for _ in range(3):
        task = lab["lab"].claim_task(run["id"], "w")
        assert lab["lab"].claim_task(run["id"], "w") is None, "two tasks on one branch"
        order.append(task["task_key"])
        lab["lab"].record_verification(task["id"], verdict="completed_verified", evidence={})
    assert order == keys[:3]


@pytest.mark.parametrize("options", [{"max_iterations": 0}, {"max_iterations": 501},
                                     {"max_attempts_per_task": 0}, {"max_attempts_per_task": 11},
                                     {"deadline_seconds": -1}, {"deadline_seconds": 10 ** 9}])
def test_budgets_outside_their_bounds_are_refused(lab, options):
    with pytest.raises(ValueError):
        lab["lab"].start_run("user:alice", lab["project"], lab["goal"], **options)
    assert lab["lab"].list_runs("user:alice", lab["project"]) == []


# ------------------------------------------------------------- worker heartbeat
def test_a_registered_worker_reads_as_live_and_a_stopped_one_does_not(lab):
    """A lease cannot tell an idle worker from a dead one; the heartbeat can."""
    lab["lab"].register_worker("w1")
    live = lab["lab"].live_workers(max_age_seconds=60)
    assert [w["worker_id"] for w in live] == ["w1"]
    assert live[0]["kind"] == "worker"
    # A heartbeat with an error still keeps the worker live, with the reason.
    lab["lab"].heartbeat("w1", error="run X failed")
    live = lab["lab"].live_workers(max_age_seconds=60)
    assert live[0]["last_error"] == "run X failed"
    # A graceful stop is no longer live.
    lab["lab"].stop_worker("w1")
    assert lab["lab"].live_workers(max_age_seconds=60) == []


def test_an_old_heartbeat_is_not_live(lab):
    lab["lab"].register_worker("w1")
    with lab["store"]._tx() as cur:
        cur.execute("UPDATE nexus_lab_workers SET heartbeat_at=now() - interval '10 minutes' "
                    "WHERE worker_id='w1'")
    assert lab["lab"].live_workers(max_age_seconds=60) == []


def test_registering_the_same_worker_twice_is_an_upsert(lab):
    lab["lab"].register_worker("w1", kind="worker")
    lab["lab"].register_worker("w1", kind="inline")
    live = lab["lab"].live_workers(max_age_seconds=60)
    assert len(live) == 1 and live[0]["kind"] == "inline"


def test_register_worker_rejects_an_unknown_kind(lab):
    with pytest.raises(ValueError):
        lab["lab"].register_worker("w1", kind="bogus")

