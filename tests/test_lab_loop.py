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

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import InvalidTransition, PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    LabScheduler,
    OpenHandsExecutor,
    TaskReport,
    Verification,
    _extract_sha,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

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


def _run_with_tasks(env, tasks, **run_options):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"], **run_options)
    for t in tasks:
        env["lab"].add_task("user:alice", env["project"], run["id"], **t)
    return run


def _run(env, run):
    return env["lab"].get_run("user:alice", env["project"], run["id"])


def _tasks(env, run):
    return {t["task_key"]: t
            for t in env["lab"].list_tasks("user:alice", env["project"], run["id"])}


def _states(env, run):
    return {key: task["state"] for key, task in _tasks(env, run).items()}


def _set_run(env, run, state):
    return env["lab"].set_run_state("user:alice", env["project"], run["id"], state)


def _sql(env, statement, *params):
    with env["store"]._tx() as cur:
        cur.execute(statement, params)


class HeldExecutor:
    """Conversations that stay running until the test lets them finish."""

    def __init__(self):
        self.dispatched: list[str] = []
        self.polled: list[str] = []
        self.done: set[str] = set()

    def dispatch(self, task):
        self.dispatched.append(task["task_key"])
        return f"conv-{task['task_key']}-{len(self.dispatched)}"

    def poll(self, task, conversation_id):
        self.polled.append(task["task_key"])
        state = "finished" if task["task_key"] in self.done else "running"
        return TaskReport(state=state, final_text="did work", conversation_id=conversation_id)


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


# ------------------------------------------------------ which repository may run
REPO = "acme/repo"
REPO_TASKS = [{"task_key": "a", "title": "A", "repository": REPO, "branch": "main"},
              {"task_key": "b", "title": "B", "repository": REPO, "branch": "main",
               "dependencies": ["a"]}]


def _allow(env, *, link=True, branches=("main",)):
    env["store"].add_repository(REPO, default_branch="main", allowed_branches=list(branches))
    if link:
        ProjectRepositoryService(env["store"]).link("user:alice", env["project"], REPO)


def _not_in_the_allowlist(env):
    return f"repository {REPO} is not in the allowlist"


def _not_linked(env):
    _allow(env, link=False)
    return f"repository {REPO} is not linked to project {env['project']}"


def _switched_off(env):
    _allow(env)
    _sql(env, "UPDATE repositories SET enabled=false WHERE full_name=%s", REPO)
    return f"repository {REPO} is disabled"


def _another_branch(env):
    _allow(env, branches=("release",))
    return "branch main is not allowed"


@pytest.mark.parametrize("arrange", [_not_in_the_allowlist, _not_linked, _switched_off,
                                     _another_branch])
def test_no_conversation_is_started_for_a_repository_the_project_may_not_use(env, arrange):
    """The gate stands immediately before the conversation is created.

    A run is checked when it is started, but its tasks wait: by the time one is
    claimed the link may be gone or the repository switched off. Before this
    gate existed the lab was the one path that started a conversation without
    asking, and a task for a repository in nobody's allowlist reached the Cloud.
    The executor here is the real one against the fake Cloud, so "nothing was
    started" is read off the wire.
    """
    reason = arrange(env)
    run = _run_with_tasks(env, REPO_TASKS)
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        sched = LabScheduler(env["lab"], OpenHandsExecutor(client, poll_interval=0.01),
                             FakeVerifier())
        result = sched.tick(run["id"])
        assert [call for call in cloud.cloud.calls if call[0] == "POST"] == []

    assert result.claimed == "a" and result.dispatched is False
    task = _tasks(env, run)["a"]
    assert task["state"] == "blocked" and task["conversation_id"] is None
    assert task["verification"] == {"verdict": "blocked", "reason": f"not authorized: {reason}"}
    stopped = _run(env, run)
    assert stopped["state"] == "blocked"
    assert stopped["last_error"] == f"task a was not started: {reason}"


def test_a_run_refused_at_the_gate_goes_on_once_the_repository_is_allowed(env):
    run = _run_with_tasks(env, REPO_TASKS)
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.run_until_done(run["id"])
    assert executor.dispatched == [] and _run(env, run)["state"] == "blocked"

    _allow(env)
    assert _set_run(env, run, "running")["last_error"] is None
    sched.run_until_done(run["id"])
    assert executor.dispatched == ["a", "b"]
    assert _run(env, run)["state"] == "completed"


def test_an_allowed_repository_is_started_on_the_cloud(env):
    """The other half: the gate lets the permitted case through, untouched."""
    _allow(env)
    run = _run_with_tasks(env, REPO_TASKS)
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        sched = LabScheduler(env["lab"], OpenHandsExecutor(client, poll_interval=0.01),
                             FakeVerifier())
        result = sched.tick(run["id"])
        starts = [call for call in cloud.cloud.calls if call[0] == "POST"]
    assert result.dispatched is True and len(starts) == 1
    assert _tasks(env, run)["a"]["conversation_id"]


# --------------------------------------------------------- pause, cancel, resume
def test_a_paused_run_starts_nothing_but_keeps_what_was_already_started(env):
    """Pausing used to change a label and nothing else: the scheduler never
    read the run's state, and a "paused" run went on to complete."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}, {"task_key": "b", "title": "B"}])
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.tick(run["id"])
    assert executor.dispatched == ["a"]

    _set_run(env, run, "paused")
    for _ in range(3):
        result = sched.tick(run["id"])
        assert result.claimed is None and result.finished is False and result.idle is False
    assert executor.dispatched == ["a"], "a paused run started a task"
    assert _states(env, run) == {"a": "running", "b": "pending"}

    # The conversation that was already under way is still read: its answer was
    # paid for, and it must not be lost or asked for a second time on resume.
    executor.done.add("a")
    result = sched.tick(run["id"])
    assert result.verdict == "completed_verified" and result.idle is True
    assert _states(env, run) == {"a": "completed_verified", "b": "pending"}
    assert _run(env, run)["state"] == "paused"

    _set_run(env, run, "running")
    executor.done.add("b")
    sched.run_until_done(run["id"])
    assert executor.dispatched == ["a", "b"]
    assert _run(env, run)["state"] == "completed"


@pytest.mark.parametrize("state", ["paused", "cancelled"])
def test_the_claim_itself_refuses_a_run_that_is_not_running(env, state):
    """Checked in the claim's own transaction, so there is no moment between
    "the run looked fine" and "the task was taken"."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    _set_run(env, run, state)
    assert env["lab"].claim_task(run["id"], "w1") is None
    assert _run(env, run)["iteration"] == 0
    assert _tasks(env, run)["a"]["attempt"] == 0


def test_a_cancelled_run_is_over_for_good(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}, {"task_key": "b", "title": "B"},
                                {"task_key": "c", "title": "C"}])
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.tick(run["id"])                      # a is running
    claimed = env["lab"].claim_task(run["id"], "w2")   # b is claimed by a worker mid-start
    assert claimed["task_key"] == "b"
    polls_before = len(executor.polled)

    assert _set_run(env, run, "cancelled")["state"] == "cancelled"
    assert _set_run(env, run, "cancelled")["state"] == "cancelled"       # idempotent
    tasks = _tasks(env, run)
    assert {key: (t["state"], t["last_error"]) for key, t in tasks.items()} == {
        key: ("blocked", "run cancelled") for key in "abc"}
    with env["store"]._tx() as cur:
        cur.execute("SELECT state FROM nexus_goals WHERE id=%s", (env["goal"],))
        assert cur.fetchone()["state"] == "cancelled"

    # Nothing is polled or started again...
    for _ in range(3):
        result = sched.tick(run["id"])
        assert result.finished is True and result.note == "run is cancelled"
    assert len(executor.polled) == polls_before and executor.dispatched == ["a"]

    # ...a worker that was mid-way cannot bring a task back...
    late = env["lab"].mark_running(tasks["b"]["id"], conversation_id="conv-late")
    assert (late["state"], late["conversation_id"]) == ("blocked", "conv-late")
    settled = env["lab"].record_verification(tasks["a"]["id"], verdict="completed_verified",
                                             evidence={"late": True})
    assert settled["state"] == "blocked" and settled["evidence"] == []

    # ...and neither can a person.
    for state in ("running", "paused"):
        with pytest.raises(InvalidTransition, match="a cancelled run cannot become"):
            _set_run(env, run, state)
    assert _run(env, run)["state"] == "cancelled"


def test_a_decision_made_a_moment_too_late_does_not_overrule_a_person(env):
    """The scheduler decides from what it read; a pause or cancel since stands."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    _set_run(env, run, "paused")
    assert env["lab"].set_run_state_internal(run["id"], "blocked", last_error="stale") is None
    assert env["lab"].set_run_state_internal(run["id"], "completed") is None
    paused = _run(env, run)
    assert (paused["state"], paused["last_error"]) == ("paused", None)

    _set_run(env, run, "cancelled")
    assert env["lab"].set_run_state_internal(run["id"], "completed") is None
    assert _run(env, run)["state"] == "cancelled"


def test_resuming_a_blocked_run_retries_what_did_not_verify(env):
    """"Resume" on a blocked run flipped the label and the next tick blocked it
    again: nothing could ever be retried."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"},
                                {"task_key": "b", "title": "B", "dependencies": ["a"]}])
    executor = FakeExecutor()
    verifier = FakeVerifier({"a": "completed_unverified"})
    sched = LabScheduler(env["lab"], executor, verifier)
    sched.run_until_done(run["id"])
    blocked = _run(env, run)
    assert blocked["state"] == "blocked" and blocked["last_error"]
    assert _states(env, run) == {"a": "completed_unverified", "b": "pending"}

    verifier.verdicts.clear()                       # the cause was fixed
    resumed = _set_run(env, run, "running")
    assert (resumed["state"], resumed["last_error"]) == ("running", None)
    task = _tasks(env, run)["a"]
    assert (task["state"], task["attempt"], task["conversation_id"]) == ("pending", 1, None)
    # The attempt that is over stays on record, with its conversation.
    assert task["evidence"][-1] == {"requeued": "resumed", "attempt": 1,
                                    "was": "completed_unverified", "conversation_id": "conv-a"}

    sched.run_until_done(run["id"])
    assert executor.dispatched == ["a", "a", "b"]
    assert _run(env, run)["state"] == "completed"
    assert _tasks(env, run)["a"]["attempt"] == 2


def test_a_blocked_run_with_nothing_left_to_retry_cannot_be_resumed(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}])
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier({"a": "failed"}))
    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "blocked"
    with pytest.raises(InvalidTransition, match="nothing to retry"):
        _set_run(env, run, "running")
    assert _run(env, run)["state"] == "blocked"
    assert _states(env, run) == {"a": "failed"}


# ------------------------------------------------------------------------ budgets
def test_a_run_starts_no_more_tasks_than_its_iteration_budget(env):
    """``max_iterations`` was stored and never read; the counter stayed at 0."""
    run = _run_with_tasks(env, [{"task_key": key, "title": key.upper()} for key in "abc"],
                          max_iterations=2)
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.run_until_done(run["id"])

    assert executor.dispatched == ["a", "b"]
    over = _run(env, run)
    assert (over["state"], over["iteration"]) == ("failed", 2)
    assert over["last_error"] == "iteration budget used: 2 of 2 task starts"
    tasks = _tasks(env, run)
    assert _states(env, run) == {"a": "completed_verified", "b": "completed_verified",
                                 "c": "blocked"}
    assert tasks["c"]["last_error"] == "run failed: iteration budget used: 2 of 2 task starts"


def test_a_run_inside_its_budget_is_not_failed_by_it(env):
    run = _run_with_tasks(env, [{"task_key": key, "title": key.upper()} for key in "ab"],
                          max_iterations=2)
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier())
    sched.run_until_done(run["id"])
    done = _run(env, run)
    assert (done["state"], done["iteration"]) == ("completed", 2)


def test_nothing_is_started_after_the_deadline(env):
    """``deadline_at`` was stored and never read."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}, {"task_key": "b", "title": "B"}],
                          deadline_seconds=3600)
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.tick(run["id"])
    assert executor.dispatched == ["a"]

    _sql(env, "UPDATE nexus_runs SET deadline_at = now() - interval '1 second' WHERE id=%s",
         run["id"])
    result = sched.tick(run["id"])
    assert result.claimed is None and result.finished is False     # a is still under way
    executor.done.add("a")
    sched.run_until_done(run["id"])

    assert executor.dispatched == ["a"], "a task was started after the deadline"
    late = _run(env, run)
    assert (late["state"], late["last_error"]) == ("failed", "deadline passed")
    assert _states(env, run) == {"a": "completed_verified", "b": "blocked"}


def test_a_conversation_that_outlives_its_lease_is_not_started_again(env):
    """The lease is 15 minutes by default and nothing renewed it, so any task
    that ran longer was requeued and started a second time while the first
    conversation was still running - then a third - and finally marked failed.
    A lease now only reclaims a task that never got a conversation."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier(), lease_seconds=0)
    for _ in range(5):
        result = sched.tick(run["id"])
        assert result.reclaimed == 0
    assert executor.dispatched == ["a"]
    task = _tasks(env, run)["a"]
    assert (task["state"], task["attempt"], task["conversation_id"]) == ("running", 1, "conv-a-1")

    executor.done.add("a")
    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "completed"


def test_a_task_past_its_own_time_limit_is_over_and_is_not_started_again(env):
    """``timeout_seconds`` was stored and never read."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "timeout_seconds": 60}])
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.tick(run["id"])
    sched.tick(run["id"])
    assert _states(env, run) == {"a": "running"}                 # inside the limit

    _sql(env, "UPDATE nexus_run_tasks SET updated_at = now() - interval '61 seconds' "
              "WHERE run_id=%s", run["id"])
    result = sched.tick(run["id"])
    assert result.verdict == "failed"
    task = _tasks(env, run)["a"]
    assert task["state"] == "failed"
    assert task["verification"]["reason"] == (
        "timed out after 60 s; the conversation was left running")
    assert executor.dispatched == ["a"]
    assert _run(env, run)["state"] == "blocked"


def test_a_run_with_no_tasks_has_not_completed(env):
    """Zero tasks left unverified used to read as "everything is verified"."""
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    result = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier()).tick(run["id"])
    assert result.finished is True
    empty = _run(env, run)
    assert (empty["state"], empty["last_error"]) == ("failed", "the run has no tasks")


# ------------------------------------------------- no run is open-ended in time
def test_a_run_that_names_no_deadline_gets_a_day(env):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    allowed = (run["deadline_at"] - run["created_at"]).total_seconds()
    assert abs(allowed - 24 * 3600) < 5
    # ...and one that names a deadline keeps its own.
    env["lab"].set_run_state("user:alice", env["project"], run["id"], "cancelled")
    own = env["lab"].start_run("user:alice", env["project"], env["goal"], deadline_seconds=90)
    assert abs((own["deadline_at"] - own["created_at"]).total_seconds() - 90) < 5


def _made_before_deadlines_existed(env, run, age):
    """A run as the code before this change stored it: no deadline at all."""
    _sql(env, "UPDATE nexus_runs SET deadline_at=NULL, "
              "created_at = now() - %s::interval, updated_at = now() - %s::interval "
              "WHERE id=%s", age, age, run["id"])


def test_a_run_nobody_served_does_not_spring_to_life_days_later(env):
    """What production looked like: runs "running" for days with every task
    pending, because no executor was deployed. Switching an executor on must
    not start that old work; the runs are over, and say why."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}, {"task_key": "b", "title": "B"}])
    _made_before_deadlines_existed(env, run, "25 hours")
    executor = FakeExecutor()
    result = LabScheduler(env["lab"], executor, FakeVerifier()).tick(run["id"])

    assert executor.dispatched == [] and result.claimed is None
    assert result.finished is True
    stale = _run(env, run)
    assert (stale["state"], stale["last_error"], stale["iteration"]) == (
        "failed", "deadline passed", 0)
    assert _states(env, run) == {"a": "blocked", "b": "blocked"}
    # The deadline it was held to is shown, not an empty field.
    assert abs((stale["deadline_at"] - stale["created_at"]).total_seconds() - 24 * 3600) < 1


def test_an_old_run_still_inside_its_day_is_served(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    _made_before_deadlines_existed(env, run, "23 hours")
    executor = FakeExecutor()
    LabScheduler(env["lab"], executor, FakeVerifier()).run_until_done(run["id"])
    assert executor.dispatched == ["a"]
    assert _run(env, run)["state"] == "completed"


def test_a_start_that_fails_after_a_cancel_does_not_requeue_the_task(env):
    """The Cloud refused the start; meanwhile the run was cancelled. Releasing
    the claim must not turn the settled task back into "pending"."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    task = env["lab"].claim_task(run["id"], "w1")
    _set_run(env, run, "cancelled")
    released = env["lab"].release_task(task["id"], error="CloudError: 503", requeue=True)
    assert (released["state"], released["last_error"]) == ("blocked", "run cancelled")
    # The ordinary case is unchanged: a failed start goes back to the queue.
    other = env["lab"].start_run("user:alice", env["project"], env["goal"])
    env["lab"].add_task("user:alice", env["project"], other["id"], task_key="b", title="B")
    claimed = env["lab"].claim_task(other["id"], "w1")
    back = env["lab"].release_task(claimed["id"], error="CloudError: 503", requeue=True)
    assert (back["state"], back["last_error"]) == ("pending", "CloudError: 503")



# ------------------------------------------------- a start the service refused
class RefusingExecutor(FakeExecutor):
    """The service answers 429 until the test lets it through."""

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.refusals = 0

    def dispatch(self, task):
        if self.error is not None:
            self.refusals += 1
            raise self.error
        return super().dispatch(task)


def _waits(env, run, key="a"):
    """Seconds until *key* may be claimed again (negative once it may)."""
    with env["store"]._tx() as cur:
        cur.execute("SELECT EXTRACT(EPOCH FROM (lease_expires - now())) AS s "
                    "FROM nexus_run_tasks WHERE run_id=%s AND task_key=%s", (run["id"], key))
        return cur.fetchone()["s"]


def test_a_start_the_service_refuses_is_not_an_attempt(env):
    """A daily limit lasts for hours. It used to cost a task its three attempts
    within seconds, and the run it stopped could not be resumed."""
    from orchestration.cloud_client import CloudRateLimited
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 3}])
    limit = CloudRateLimited(
        'POST /api/v1/app-conversations -> 429: {"detail": {"code": '
        '"daily_conversation_limit_reached"}}', code="daily_conversation_limit_reached")
    executor = RefusingExecutor(limit)
    sched = LabScheduler(env["lab"], executor, FakeVerifier())

    # Ten passes of the loop: one refusal, then the task waits.
    for _ in range(10):
        result = sched.tick(run["id"])
        assert not result.finished
    assert executor.refusals == 1
    task = _tasks(env, run)["a"]
    assert (task["state"], task["attempt"], task["conversation_id"]) == ("pending", 0, None)
    assert "not counted as an attempt" in task["last_error"]
    assert "trying again in 15 min" in task["last_error"]
    assert "daily_conversation_limit_reached" in task["last_error"]
    assert 890 < _waits(env, run) <= 900
    waiting = _run(env, run)
    # Still running, nothing spent: neither "blocked" nor out of iterations.
    assert (waiting["state"], waiting["iteration"], waiting["last_error"]) == ("running", 0, None)

    # The wait is over and the limit is still there: one more refusal, no more.
    _sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
              "WHERE run_id=%s", run["id"])
    for _ in range(5):
        sched.tick(run["id"])
    assert executor.refusals == 2 and _tasks(env, run)["a"]["attempt"] == 0

    # The limit is lifted: the task starts, on its first attempt.
    executor.error = None
    _sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
              "WHERE run_id=%s", run["id"])
    done = sched.run_until_done(run["id"])
    assert done.finished and executor.dispatched == ["a"]
    assert _tasks(env, run)["a"]["attempt"] == 1
    finished = _run(env, run)
    assert (finished["state"], finished["iteration"]) == ("completed", 1)


def test_a_refused_start_does_not_bring_back_a_cancelled_task(env):
    from orchestration.cloud_client import CloudRateLimited
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    task = env["lab"].claim_task(run["id"], "w1")
    _set_run(env, run, "cancelled")
    back = env["lab"].postpone_task(task["id"], error=str(CloudRateLimited("429")), seconds=60)
    assert (back["state"], back["last_error"], back["attempt"]) == ("blocked", "run cancelled", 1)
    assert _run(env, run)["iteration"] == 1          # nothing was given back either


@pytest.mark.parametrize("retry_after, code, expected", [
    (None, "daily_conversation_limit_reached", 900),   # nothing changes for hours
    (None, None, 60),
    (120.0, None, 120),                                # what the service asked for
    (1.0, None, 30),                                   # ...but not every second
    (86_400.0, "daily_conversation_limit_reached", 3600),   # ...and not once a day
])
def test_how_long_a_refused_start_waits(retry_after, code, expected):
    from orchestration.cloud_client import CloudRateLimited
    from orchestration.nexus_lab import refused_start_wait
    error = CloudRateLimited("POST -> 429", code=code, retry_after=retry_after)
    assert refused_start_wait(error) == expected


def test_the_cloud_client_reports_a_429_as_a_refusal_with_its_reason(env):
    from orchestration.cloud_client import CloudRateLimited, CloudUnavailable
    with FakeCloudServer() as cloud:
        cloud.cloud.start_status, cloud.cloud.start_status_times = 429, 1
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        with pytest.raises(CloudRateLimited) as refused:
            client.start_conversation(initial_message="hi")
    # Still the transient kind the older callers handle.
    assert isinstance(refused.value, CloudUnavailable)
    assert "429" in str(refused.value) and "capacity full" in str(refused.value)


# ------------------------------------------------------- what the agent is told
def test_the_agent_is_told_where_the_work_has_to_end_up(env):
    """The verifier accepts a task only when the head of its branch on GitHub
    is a commit the report names. The agent has to be told that in so many
    words: "if configured to push" left it free to commit and stop."""
    told = OpenHandsExecutor.instructions(
        {"task_key": "a", "title": "A", "repository": REPO, "branch": "release"})
    assert f"push it to branch release of {REPO}" in told
    assert "committed and not pushed does not count" in told
    assert "never force-push" in told
    assert "If configured to push" not in told
    assert "push it to the default branch of" in OpenHandsExecutor.instructions(
        {"task_key": "a", "title": "A", "repository": REPO, "branch": ""})
    # No repository, nowhere to push.
    bare = OpenHandsExecutor.instructions({"task_key": "a", "title": "A"})
    assert "6. Commit your work.\n" in bare and "push it to" not in bare


# ------------------------------------- a run planned before specifications were
def test_a_task_with_no_specification_is_given_its_runs_request(env):
    """Runs planned before specifications existed are still waiting on the
    live service. Their tasks hold a title and a line of description; the
    request itself is in the goal, and the agent is now given it whole."""
    import hashlib

    from orchestration.nexus_specs import SpecService
    request = ("  MASTER TASK: собрать сервис.\r\n\r\n1. Система должна принимать заказ.\n"
               "2. Проверь, что 👩🏽\u200d💻 и e\u0301 дошли как есть.\n")
    goal = str(env["svc"].create_goal("user:alice", env["project"], request)[0]["id"])
    stored = request.strip()                      # a goal of that time was stored trimmed
    _allow(env)
    run = env["lab"].start_run("user:alice", env["project"], goal)
    env["lab"].add_task("user:alice", env["project"], run["id"], task_key="a",
                        title="Принять заказ", description="Одна строка.",
                        repository=REPO, branch="main")
    assert _tasks(env, run)["a"]["spec_ref"] is None

    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        executor = OpenHandsExecutor(client, poll_interval=0.01, specs=SpecService(env["store"]))
        LabScheduler(env["lab"], executor, FakeVerifier()).tick(run["id"])
        (body,) = cloud.cloud.start_bodies.values()
    message = body["initial_message"]["content"][0]["text"]
    digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    opening, closing = f"<<<NEXUS-TASK-TEXT {digest[:12]}\n", f"\nNEXUS-TASK-TEXT {digest[:12]}>>>"
    assert message.count(opening) == 1 and message.count(closing) == 1
    assert message[message.index(opening) + len(opening):message.index(closing)] == stored
    assert "Task: Принять заказ" in message and "do this step, not the whole request" in message
    task = _tasks(env, run)["a"]
    assert task["state"] == "running"
    proof = task["spec_ref"]["delivery"]
    assert {k: proof[k] for k in ("mode", "source", "sha256", "chars")} == {
        "mode": "inline", "source": "goal", "sha256": digest, "chars": len(stored)}


def test_a_task_with_no_specification_and_no_request_still_starts(env):
    """Nothing to hand over is not a reason to refuse a start that was allowed
    before: the executor has no access to specifications, or the goal is gone."""
    _allow(env)
    run = _run_with_tasks(env, REPO_TASKS)
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        LabScheduler(env["lab"], OpenHandsExecutor(client, poll_interval=0.01),
                     FakeVerifier()).tick(run["id"])
        (body,) = cloud.cloud.start_bodies.values()
    assert "NEXUS-TASK-TEXT" not in body["initial_message"]["content"][0]["text"]
    assert _tasks(env, run)["a"]["state"] == "running"


def test_a_conversation_with_no_status_yet_is_waited_for(env):
    """The service leaves ``execution_status`` empty while it cannot ask the
    sandbox. That is not "did not finish": the task is not settled on it."""
    _allow(env)
    run = _run_with_tasks(env, REPO_TASKS[:1])
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        sched = LabScheduler(env["lab"], OpenHandsExecutor(client, poll_interval=0.01),
                             FakeVerifier())
        sched.tick(run["id"])
        (cid,) = cloud.cloud.conversations
        cloud.cloud.conversations[cid]["execution_status"] = None
        for _ in range(3):
            assert sched.tick(run["id"]).verified is None
        assert _tasks(env, run)["a"]["state"] == "running"
        assert _run(env, run)["state"] == "running"

        cloud.cloud.add_assistant_message(cid, "done")
        cloud.cloud.conversations[cid]["execution_status"] = "finished"
        assert sched.tick(run["id"]).verdict == "completed_verified"
    assert _run(env, run)["state"] == "completed"


# --------------------------------------------------- the end of a conversation
def _conversation(cloud, events):
    cid = cloud.cloud.make_conversation(title="t")
    cloud.cloud.events[cid] = list(events)
    return cid


def _says(role, text):
    return {"kind": "MessageEvent", "source": "agent" if role == "assistant" else role,
            "llm_message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _finish_tool(message):
    return {"kind": "ActionEvent", "source": "agent", "tool_name": "finish",
            "action": {"kind": "FinishAction", "message": message}}


_NOISE = {"kind": "ObservationEvent", "source": "environment", "tool_name": "terminal"}


@pytest.mark.parametrize("server", ["honoured", "ignored", "refused"])
def test_the_final_answer_is_found_at_the_end_of_a_long_conversation(env, server):
    """Read oldest-first and bounded, the end of a long conversation is the
    part that was never reached: beyond a thousand events the report was lost
    and the task recorded as finished with no answer. Whatever the server does
    with the request for the newest page, the answer is the last one."""
    with FakeCloudServer() as cloud:
        cloud.cloud.events_sort = server
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        events = [_says("user", "task"), _says("assistant", "an early remark")]
        events += [_NOISE] * 1930 + [_says("assistant", "Готово. Коммит 0123abc.")] + [_NOISE] * 20
        cid = _conversation(cloud, events)
        assert client.final_response(cid) == "Готово. Коммит 0123abc."
        asked = [(order, page) for c, order, page in cloud.cloud.event_searches if c == cid]
    if server == "honoured":
        assert asked == [("TIMESTAMP_DESC", "")], "one page from the end is enough"
    else:
        # Asked for the newest page, did not get it, read everything in order:
        # 1 953 events are twenty pages.
        assert asked[0] == ("TIMESTAMP_DESC", "") and len(asked) == 21
        assert all(order == "" for order, _ in asked[1:])


def test_what_counts_as_the_final_answer(env):
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)

        def answer(*events):
            return client.final_response(_conversation(cloud, events))

        # The finish tool's message is the answer, not the remark before it.
        assert answer(_says("user", "task"), _says("assistant", "Смотрю."),
                      _finish_tool("Сделано: abc1234")) == "Сделано: abc1234"
        # A message written after a finish (a later turn) is the later answer.
        assert answer(_says("user", "task"), _finish_tool("Первый ответ"),
                      _says("user", "ещё"), _says("assistant", "Второй ответ")) == "Второй ответ"
        # An empty finish is answered by the text of the same turn...
        assert answer(_says("user", "task"), _says("assistant", "Отчёт: def5678"),
                      _finish_tool("")) == "Отчёт: def5678"
        # ...and never by an earlier turn's.
        assert answer(_says("user", "one"), _says("assistant", "Старый ответ"),
                      _says("user", "two"), _finish_tool("  ")) is None
        # Nothing said at all.
        assert answer(_says("user", "task"), _NOISE) is None
        assert answer() is None


# ------------------------------------------------ one task at a time per branch
def _second_run(env, tasks):
    goal = str(env["svc"].create_goal("user:alice", env["project"], "Another goal")[0]["id"])
    run = env["lab"].start_run("user:alice", env["project"], goal)
    for t in tasks:
        env["lab"].add_task("user:alice", env["project"], run["id"], **t)
    return run


def test_two_runs_take_turns_on_one_branch(env):
    """A task is accepted when the head of its branch is the commit its agent
    reports. Two agents pushing to one branch move the head under each other,
    so only one task works on a branch at a time - across runs."""
    _allow(env)
    on_main = {"repository": REPO, "branch": "main"}
    first = _run_with_tasks(env, [{"task_key": "a", "title": "A", **on_main},
                                  {"task_key": "b", "title": "B", "dependencies": ["a"], **on_main}])
    second = _second_run(env, [{"task_key": "x", "title": "X", **on_main}])
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())

    for _ in range(3):
        sched.tick(first["id"])
        waiting = sched.tick(second["id"])
    assert executor.dispatched == ["a"]
    # Waiting for its turn is not a deadlock: the second run is still running.
    assert not waiting.finished and _run(env, second)["state"] == "running"
    assert _states(env, second) == {"x": "pending"}

    # A pass verifies what finished and then claims: the first run goes on to
    # its next task in the same pass, and the second run goes on waiting.
    executor.done.add("a")
    sched.tick(first["id"])
    assert _states(env, first) == {"a": "completed_verified", "b": "running"}
    sched.tick(second["id"])
    assert executor.dispatched == ["a", "b"] and _states(env, second) == {"x": "pending"}

    # The first run is done with the branch: now the second takes it.
    executor.done.add("b")
    sched.tick(first["id"])
    assert _run(env, first)["state"] == "completed"
    sched.tick(second["id"])
    assert executor.dispatched == ["a", "b", "x"]
    executor.done.add("x")
    sched.tick(second["id"])
    assert _run(env, second)["state"] == "completed"


def test_tasks_of_one_run_do_not_share_a_branch_either(env):
    _allow(env, branches=("main", "next"))
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A", "repository": REPO, "branch": "main"},
        {"task_key": "b", "title": "B", "repository": REPO, "branch": "main"},
        {"task_key": "c", "title": "C", "repository": REPO, "branch": "next"},
        {"task_key": "d", "title": "D"}])                       # no repository at all
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    for _ in range(5):
        sched.tick(run["id"])
    # Another branch and "no repository" are not held back; the same branch is.
    assert executor.dispatched == ["a", "c", "d"]
    executor.done.add("a")
    sched.tick(run["id"])
    sched.tick(run["id"])
    assert executor.dispatched == ["a", "c", "d", "b"]


def test_two_workers_reaching_for_one_branch_at_once_get_one_task(env):
    """Each worker's transaction sees the branch free. The lock on the branch
    puts them in line, and the second looks again."""
    import threading
    _allow(env)
    runs = [_run_with_tasks(env, [{"task_key": "a", "title": "A", "repository": REPO,
                                   "branch": "main"}])]
    runs += [_second_run(env, [{"task_key": f"x{n}", "title": "X", "repository": REPO,
                                "branch": "main"}]) for n in range(5)]
    stores = [PostgresStore(DSN) for _ in runs]
    claimed: list[dict | None] = [None] * len(runs)
    start = threading.Barrier(len(runs))

    def claim(n):
        start.wait()
        claimed[n] = LabStore(stores[n]).claim_task(runs[n]["id"], f"w{n}")

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(len(runs))]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        for store in stores:
            store.close()
    assert len([task for task in claimed if task is not None]) == 1


# -------------------------------------------------------- a task's time limit
def test_a_new_task_is_given_two_hours_not_thirty_minutes(env, monkeypatch):
    """Thirty minutes was nobody's choice for any task, and not enough: on the
    live service agents still at work were recorded as failed, and their runs
    stopped."""
    monkeypatch.delenv("NEXUS_TASK_TIMEOUT_SECONDS", raising=False)
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    assert _tasks(env, run)["a"]["timeout_seconds"] == 7200
    # A plan stored with its run gets the same.
    planned = env["lab"].start_run(
        "user:alice", env["project"],
        str(env["svc"].create_goal("user:alice", env["project"], "Another")[0]["id"]),
        plan=[{"id": "p", "title": "P", "acceptance": [], "dependencies": []}])
    assert _tasks(env, planned)["p"]["timeout_seconds"] == 7200
    # A row written by anything else gets it from the column.
    _sql(env, "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title) "
              "VALUES (%s, %s, 'raw', 'Raw')", run["id"], env["project"])
    assert _tasks(env, run)["raw"]["timeout_seconds"] == 7200

    monkeypatch.setenv("NEXUS_TASK_TIMEOUT_SECONDS", "600")
    env["lab"].add_task("user:alice", env["project"], run["id"], task_key="b", title="B")
    assert _tasks(env, run)["b"]["timeout_seconds"] == 600
    for raw, expected in (("5", 60), ("999999", 86_400), ("soon", 7200)):
        monkeypatch.setenv("NEXUS_TASK_TIMEOUT_SECONDS", raw)
        from orchestration.nexus_lab_store import task_timeout_seconds
        assert task_timeout_seconds() == expected


def test_the_migration_lifts_the_limit_of_tasks_that_are_still_to_run(env):
    """Runs were waiting, and at work, with the old thirty minutes when the
    limit was raised. They are not cut short; what was verified stays as it
    was, and a limit somebody chose is not touched."""
    run = _run_with_tasks(env, [
        {"task_key": "waiting", "title": "W"}, {"task_key": "working", "title": "R"},
        {"task_key": "done", "title": "D"}, {"task_key": "chosen", "title": "C"},
        {"task_key": "timed-out", "title": "T"}])
    _sql(env, "UPDATE nexus_run_tasks SET timeout_seconds = 1800 WHERE run_id=%s", run["id"])
    _sql(env, "UPDATE nexus_run_tasks SET timeout_seconds = 900 WHERE task_key='chosen'")
    _sql(env, "UPDATE nexus_run_tasks SET state='completed_verified' WHERE task_key='done'")
    _sql(env, "UPDATE nexus_run_tasks SET state='failed' WHERE task_key='timed-out'")
    _sql(env, "UPDATE nexus_run_tasks SET state='running', conversation_id='c1', "
              "updated_at = now() - interval '40 minutes' WHERE task_key='working'")
    before = _tasks(env, run)["working"]["updated_at"]

    _sql(env, open(os.path.join(_MIG, "0025_task_time_limit.sql")).read())

    tasks = _tasks(env, run)
    # "timed-out" already failed on the old limit; resumed, it gets the new one.
    assert {key: task["timeout_seconds"] for key, task in tasks.items()} == {
        "waiting": 7200, "working": 7200, "timed-out": 7200, "done": 1800, "chosen": 900}
    # The limit is counted from the start of the conversation: that moment is kept.
    assert tasks["working"]["updated_at"] == before
    # Forty minutes in, the task that would have been given up goes on.
    executor = HeldExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    sched.tick(run["id"])
    assert _tasks(env, run)["working"]["state"] == "running"


# ------------------------------------------------- why a run stopped, in a line
def test_a_stopped_run_says_which_task_stopped_it_and_why(env):
    """"A dependency is not verified" was all a blocked run said. The task and
    the verifier's reason are known, and are now part of the run's error."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"},
                                {"task_key": "b", "title": "B", "dependencies": ["a"]}])
    sched = LabScheduler(env["lab"], FakeExecutor(),
                         FakeVerifier({"a": "completed_unverified"}))
    sched.run_until_done(run["id"])
    stopped = _run(env, run)
    assert stopped["state"] == "blocked"
    assert stopped["last_error"] == (
        "dependency graph deadlocked: a dependency is not verified. "
        "Task a ended completed_unverified: fake verdict for a")

    # A task that ran out of time is named the same way.
    late = _second_run(env, [{"task_key": "x", "title": "X", "timeout_seconds": 60},
                             {"task_key": "y", "title": "Y", "dependencies": ["x"]}])
    held = LabScheduler(env["lab"], HeldExecutor(), FakeVerifier())
    held.tick(late["id"])
    _sql(env, "UPDATE nexus_run_tasks SET updated_at = now() - interval '2 minutes' "
              "WHERE run_id=%s AND task_key='x'", late["id"])
    held.tick(late["id"])
    assert _run(env, late)["last_error"] == (
        "dependency graph deadlocked: a dependency is not verified. Task x ended failed: "
        "timed out after 60 s; the conversation was left running")
