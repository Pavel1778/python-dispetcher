"""Autonomous loop: dispatch -> verify -> record -> decide, over real PostgreSQL.

The executor and verifier are small in-process fakes so the *loop* is the thing
under test (ordering, dependency gating, verification gating, completion,
blocking, recovery). The store, leases and SQL are real.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    LabScheduler,
    TaskReport,
    Verification,
    _extract_sha,
)
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


class FakeExecutor:
    """Dispatches instantly and reports a scripted outcome per task key."""

    def __init__(self, outcomes: dict[str, str] | None = None):
        self.outcomes = outcomes or {}
        self.dispatched: list[str] = []

    def dispatch(self, task):
        self.dispatched.append(task["task_key"])
        return f"conv-{task['task_key']}"

    def poll(self, task, conversation_id):
        state = self.outcomes.get(task["task_key"], "finished")
        return TaskReport(state=state, final_text="did work", conversation_id=conversation_id)


class FakeVerifier:
    """Verdicts by task key, defaulting to verified."""

    def __init__(self, verdicts: dict[str, str] | None = None):
        self.verdicts = verdicts or {}

    def verify(self, task, report):
        if report.state != "finished":
            return Verification("failed", "not finished", {"state": report.state})
        v = self.verdicts.get(task["task_key"], "completed_verified")
        return Verification(v, f"fake verdict for {task['task_key']}", {"k": task["task_key"]})


@pytest.fixture
def env():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Lab")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    lab = LabStore(store)
    yield {"store": store, "svc": svc, "project": project, "goal": goal, "lab": lab}
    store.close()


def _run_with_tasks(env, tasks):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    for t in tasks:
        env["lab"].add_task("user:alice", env["project"], run["id"], **t)
    return run


def test_linear_chain_completes_in_dependency_order(env):
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B", "dependencies": ["a"]},
        {"task_key": "c", "title": "C", "dependencies": ["b"]},
    ])
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    result = sched.run_until_done(run["id"])
    assert result.finished
    assert executor.dispatched == ["a", "b", "c"]  # strict order
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"


def test_unverified_task_blocks_its_child_and_the_run(env):
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B", "dependencies": ["a"]},
    ])
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier({"a": "completed_unverified"}))
    result = sched.run_until_done(run["id"])
    assert result.finished
    # b never dispatched; the run is blocked, not completed.
    assert executor.dispatched == ["a"]
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "blocked"


def test_parallel_independent_tasks_both_run(env):
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B"},
    ])
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.run_until_done(run["id"])
    assert sorted(executor.dispatched) == ["a", "b"]
    assert env["lab"].all_verified(run["id"])


def test_dispatch_failure_requeues_and_then_succeeds(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 3}])

    class FlakyExecutor(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def dispatch(self, task):
            self.calls += 1
            if self.calls == 1:
                from orchestration.cloud_client import CloudUnavailable
                raise CloudUnavailable("boom")
            return super().dispatch(task)

    ex = FlakyExecutor()
    sched = LabScheduler(env["lab"], ex, FakeVerifier())
    sched.tick(run["id"])            # first dispatch fails, task requeued
    assert env["lab"].list_tasks("user:alice", env["project"], run["id"])[0]["state"] == "pending"
    result = sched.run_until_done(run["id"])
    assert result.finished and ex.calls == 2


def test_recovery_resumes_an_interrupted_run(env):
    """A worker that dies mid-task leaves a lease; a new scheduler recovers it."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    # Simulate a crash: claim with a zero-second lease, then abandon.
    claimed = env["lab"].claim_task(run["id"], "dead-worker", lease_seconds=0)
    assert claimed["state"] == "dispatched"
    # A fresh scheduler reclaims the expired lease and finishes the task.
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier())
    result = sched.run_until_done(run["id"])
    assert result.finished
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"


def test_no_task_is_dispatched_twice(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.run_until_done(run["id"])
    sched.run_until_done(run["id"])  # a second pass must not re-dispatch
    assert executor.dispatched == ["a"]


def test_extract_sha_finds_a_commit_but_ignores_numbers():
    assert _extract_sha("done at 3f2a1b9c") == "3f2a1b9c"
    assert _extract_sha("changed 12345 files") is None
    assert _extract_sha("no commit here") is None


def test_scheduler_tick_reports_what_it_did(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier())
    first = sched.tick(run["id"])
    assert first.claimed == "a" and first.dispatched is True
    second = sched.tick(run["id"])
    assert second.verdict == "completed_verified" and second.finished is True
