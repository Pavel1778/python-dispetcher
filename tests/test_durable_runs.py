"""A durable run does not give a task up, and stays governable while it goes on.

The scheduler, the store and the SQL are real (PostgreSQL). The executor and
the verifier are scripted in-process, so that a test decides what a
conversation does: fail three times, be refused with a 429, hang, vanish. Time
is not waited for: a wait is read from the row (``retry_at``) and then moved
into the past, the way a clock would have.

What is shown here, by acceptance criterion:

* three failed attempts make a task wait for its next cycle, not ``failed``;
* a 429 waits as long as the service asked, and spends nothing;
* a conversation past its time limit is told to stop, and the task goes on;
* another process picks a run up where the first left it;
* two workers never start one task twice;
* a result that does not verify is reworked, never accepted;
* cancel stops the conversations, hides the run, keeps every row, and holds
  the branch until the stop is confirmed;
* cancel and completion at the same moment end one way or the other.

Reports are in ``tests/test_run_reports.py``.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_retry  # noqa: E402
from orchestration.cloud_client import (  # noqa: E402
    CloudClient,
    CloudError,
    CloudNotFound,
    CloudRateLimited,
    CloudUnavailable,
)
from orchestration.cloud_store import InvalidTransition, PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    MAX_STOP_ATTEMPTS,
    LabScheduler,
    TaskReport,
    Verification,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
ALICE = "user:alice"
REPO = "acme/app"


def reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class Conversations:
    """An executor whose conversations do what the test says.

    Every start is a new conversation, ``running`` until the test changes its
    state. ``refuse`` holds errors to raise instead of starting, first in first
    out. ``stop`` is the real signal's stand-in: it is recorded, and the
    conversation becomes ``paused`` unless ``deaf`` says it ignores the signal.
    """

    def __init__(self):
        self.started: list[tuple[str, str]] = []       # (task_key, conversation)
        self.states: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.refuse: list[Exception] = []
        self.stop_calls: list[str] = []
        self.deaf: set[str] = set()
        self.stop_errors: list[Exception] = []
        self.markers: dict[str, str] = {}
        self.follow_ups: list[tuple[str, str]] = []
        self.can_continue = False
        self._lock = threading.Lock()

    # ---- what the scheduler calls
    def dispatch(self, task):
        with self._lock:
            if self.refuse:
                raise self.refuse.pop(0)
            resume = task.get("resume_conversation_id")
            if self.can_continue and resume:
                from orchestration.nexus_lab import Dispatched

                self.follow_ups.append((task["task_key"], str(resume)))
                self.states[str(resume)] = "running"
                return Dispatched(str(resume), None, None, "continue")
            conversation = f"conv-{task['task_key']}-{len(self.started) + 1}"
            self.started.append((task["task_key"], conversation))
            self.states[conversation] = "running"
            return conversation

    def poll(self, task, conversation_id):
        state = self.states.get(conversation_id, "missing")
        return TaskReport(state=state, final_text=self.texts.get(conversation_id, "did the work"),
                          conversation_id=conversation_id)

    def stop(self, account_id, conversation_id, project_id=None):
        conversation = str(conversation_id)
        if self.stop_errors:
            raise self.stop_errors.pop(0)
        if conversation not in self.states:
            raise CloudNotFound("conversation not found")
        self.stop_calls.append(conversation)
        if conversation not in self.deaf:
            self.states[conversation] = "paused"
        return True

    def conversation_state(self, target):
        return self.states.get(str(target["conversation_id"]), "missing")

    def progress(self, task, conversation_id):
        return self.markers.get(conversation_id)

    # ---- what the test calls
    def of(self, task_key: str) -> list[str]:
        return [conv for key, conv in self.started if key == task_key]

    def finish(self, conversation: str, text: str = "did the work") -> None:
        self.states[conversation] = "finished"
        self.texts[conversation] = text


class Verdicts:
    """A verifier that answers from a script: per task, one verdict per call,
    the last one repeated. Anything not scripted verifies."""

    def __init__(self, script: dict[str, list] | None = None):
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def verify(self, task, report):
        self.calls.append((task["task_key"], report.conversation_id or ""))
        if report.state != "finished":
            return Verification("failed", f"conversation ended {report.state}",
                                {"state": report.state})
        queue = self.script.get(task["task_key"])
        verdict = (queue.pop(0) if queue and len(queue) > 1 else queue[0]) if queue \
            else "completed_verified"
        if isinstance(verdict, Verification):
            return verdict
        return Verification(verdict, f"scripted: {verdict}", {"task": task["task_key"]})


@pytest.fixture
def env():
    reset_schema()
    store = PostgresStore(DSN)
    projects = ProjectService(store)
    project = str(projects.create(ALICE, "Durable")["id"])
    goal = str(projects.create_goal(ALICE, project, "Ship it")[0]["id"])
    yield {"store": store, "projects": projects, "project": project, "goal": goal,
           "lab": LabStore(store)}
    store.close()


def start(env, tasks, *, policy="durable", repository="", **options):
    """A run with *tasks*. With *repository* every task works on its ``main``
    branch (the repository is allowed and linked to the project first)."""
    if repository:
        env["store"].add_repository(repository, default_branch="main",
                                    allowed_branches=["main"])
        ProjectRepositoryService(env["store"]).link(ALICE, env["project"], repository)
    run = env["lab"].start_run(ALICE, env["project"], env["goal"], policy=policy, **options)
    for task in tasks:
        where = {"repository": repository, "branch": "main"} if repository else {}
        env["lab"].add_task(ALICE, env["project"], run["id"], **{**where, **task})
    return str(run["id"])


def scheduler(env, executor, verifier=None, **options) -> LabScheduler:
    options.setdefault("cycle_wait", nexus_retry.cycle_wait)
    return LabScheduler(env["lab"], executor, verifier or Verdicts(), **options)


def tasks_of(env, run_id) -> dict[str, dict]:
    return {t["task_key"]: t for t in env["lab"].tasks_internal(run_id)}


def run_of(env, run_id) -> dict:
    return env["lab"].run_row(run_id)


def sql(env, statement, *params):
    with env["store"]._tx() as cur:
        cur.execute(statement, params)
        return cur.fetchall() if cur.description else None


def seconds_until(env, run_id, task_key) -> float:
    """How far in the future the task's ``retry_at`` is, on the database's clock."""
    rows = sql(env, "SELECT extract(epoch FROM retry_at - now()) AS s FROM nexus_run_tasks "
                    "WHERE run_id=%s AND task_key=%s", run_id, task_key)
    return float(rows[0]["s"])


def time_passes(env, run_id) -> None:
    """Every wait of the run is over: what a clock would have done."""
    sql(env, "UPDATE nexus_run_tasks SET retry_at = now() - interval '1 second' "
             "WHERE run_id=%s AND retry_at IS NOT NULL", run_id)
    sql(env, "UPDATE nexus_runs SET resume_at = now() - interval '1 second' "
             "WHERE id=%s AND resume_at IS NOT NULL", run_id)
    sql(env, "UPDATE nexus_executor_stops SET next_attempt_at = now() - interval '1 second' "
             "WHERE run_id=%s", run_id)


def fail_attempt(sched, executor, run_id, task_key) -> str:
    """One attempt of *task_key* that ends in an error: start it, break it,
    let the scheduler see it. Returns the conversation."""
    sched.tick(run_id)
    conversation = executor.of(task_key)[-1]
    executor.states[conversation] = "error"
    sched.tick(run_id)
    return conversation


# ------------------------------------------------- attempts, cycles, waiting
def test_three_failed_attempts_make_the_task_wait_for_its_next_cycle_not_fail(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor)

    for _ in range(3):
        fail_attempt(sched, executor, run_id, "build")
        sched.deliver_stops()

    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "waiting", task
    assert task["cycle"] == 1 and task["attempt"] == 0, "the next cycle has its attempts back"
    assert task["retry_at"] is not None and "цикл" in (task["wait_reason"] or "")
    run = run_of(env, run_id)
    assert run["state"] == "running" and run["last_error"] is None
    assert len(executor.of("build")) == 3

    # Nothing is started while the task waits, however often the loop comes by.
    for _ in range(5):
        assert sched.tick(run_id).waiting
    assert len(executor.of("build")) == 3

    # When the wait is over the task is taken again, and when its agent
    # delivers, the run completes.
    time_passes(env, run_id)
    sched.tick(run_id)
    assert len(executor.of("build")) == 4
    executor.finish(executor.of("build")[-1])
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"
    done = tasks_of(env, run_id)["build"]
    assert done["state"] == "completed_verified" and done["cycle"] == 1


def test_the_wait_between_cycles_doubles_up_to_a_ceiling(env, monkeypatch):
    assert [round(nexus_retry.cycle_wait(n, base=60, ceiling=3600)) for n in range(1, 9)] == \
        [60, 120, 240, 480, 960, 1920, 3600, 3600]
    # With a seed the wait is spread by at most a tenth, and is the same every time.
    spread = nexus_retry.cycle_wait(3, seed="task", base=60, ceiling=3600)
    assert 240 <= spread < 264 and spread == nexus_retry.cycle_wait(
        3, seed="task", base=60, ceiling=3600)

    monkeypatch.setenv("NEXUS_RETRY_BASE_SECONDS", "100")
    monkeypatch.setenv("NEXUS_RETRY_MAX_SECONDS", "250")
    run_id = start(env, [{"task_key": "build", "title": "Build"}], max_attempts_per_task=1)
    sql(env, "UPDATE nexus_run_tasks SET max_attempts=1 WHERE run_id=%s", run_id)
    executor = Conversations()
    sched = scheduler(env, executor)
    waits = []
    for _ in range(4):
        fail_attempt(sched, executor, run_id, "build")
        waits.append(seconds_until(env, run_id, "build"))
        time_passes(env, run_id)
        sched.deliver_stops()
    assert 99 <= waits[0] <= 111 and 199 <= waits[1] <= 221, waits
    assert 249 <= waits[2] <= 251 and 249 <= waits[3] <= 251, waits   # the ceiling
    assert tasks_of(env, run_id)["build"]["cycle"] == 4
    assert run_of(env, run_id)["state"] == "running"


def test_the_operators_ceiling_on_cycles_stops_the_task_for_a_person(env, monkeypatch):
    """``NEXUS_RETRY_MAX_CYCLES`` is the operator's own limit. Without it there
    is none; with it, a task that has used them is not thrown away either: the
    run is blocked, and "resume" gives the task a new cycle."""
    monkeypatch.setenv("NEXUS_RETRY_MAX_CYCLES", "2")
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    sql(env, "UPDATE nexus_run_tasks SET max_attempts=1 WHERE run_id=%s", run_id)
    executor = Conversations()
    sched = scheduler(env, executor)
    fail_attempt(sched, executor, run_id, "build")          # cycle 0 used -> waits
    assert tasks_of(env, run_id)["build"]["state"] == "waiting"
    time_passes(env, run_id)
    fail_attempt(sched, executor, run_id, "build")          # cycle 1 used -> the ceiling
    last = sched.run_until_done(run_id)
    run = run_of(env, run_id)
    assert run["state"] == "blocked", (run, last)
    assert "NEXUS_RETRY_MAX_CYCLES" in tasks_of(env, run_id)["build"]["verification"]["reason"]

    resumed = env["lab"].set_run_state(ALICE, env["project"], run_id, "running")
    assert resumed["state"] == "running"
    assert tasks_of(env, run_id)["build"]["state"] == "pending"


def test_a_bounded_run_stops_where_it_always_did(env):
    """The policy before this work, kept for a caller that names a budget."""
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}],
                   policy="bounded")
    executor = Conversations()
    sched = scheduler(env, executor)
    for attempt in (1, 2, 3):
        fail_attempt(sched, executor, run_id, "build")
        sched.run_until_done(run_id)
        assert run_of(env, run_id)["state"] == "blocked"
        if attempt < 3:
            env["lab"].set_run_state(ALICE, env["project"], run_id, "running")
    assert run_of(env, run_id)["state"] == "blocked"
    assert tasks_of(env, run_id)["build"]["state"] == "failed"
    with pytest.raises(InvalidTransition, match="used all its attempts"):
        env["lab"].set_run_state(ALICE, env["project"], run_id, "running")


# ------------------------------------------------------------------- refusals
def test_a_refused_start_waits_as_long_as_the_service_asked_and_spends_nothing(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    executor.refuse = [CloudRateLimited("429 Too Many Requests", retry_after=120.0)]
    sched = scheduler(env, executor)

    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "pending" and task["attempt"] == 0, "a refusal is not an attempt"
    assert task["refusals"] == 1
    assert run_of(env, run_id)["iteration"] == 0, "nor a task start of the run"
    # The claim's lease is the wait: the task is not offered again before it ends.
    lease = sql(env, "SELECT extract(epoch FROM lease_expires - now()) AS s "
                     "FROM nexus_run_tasks WHERE id=%s", task["id"])[0]["s"]
    assert 118 <= float(lease) <= 121
    assert "not counted as an attempt" in task["last_error"]
    assert sched.tick(run_id).waiting and not executor.started

    # Refused again, this time without Retry-After: the wait grows with the
    # number of refusals in a row.
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", task["id"])
    executor.refuse = [CloudRateLimited("429 Too Many Requests")]
    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    lease = sql(env, "SELECT extract(epoch FROM lease_expires - now()) AS s "
                     "FROM nexus_run_tasks WHERE id=%s", task["id"])[0]["s"]
    assert task["refusals"] == 2 and 118 <= float(lease) <= 121     # 60 s doubled once
    assert task["attempt"] == 0 and run_of(env, run_id)["state"] == "running"

    # The service takes the start: the count of refusals is forgotten.
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", task["id"])
    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "running" and task["attempt"] == 1 and task["refusals"] == 0


def test_refusals_never_use_up_a_task(env):
    """Twenty refusals in a row leave the task with every attempt it had."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor, start_wait=lambda error, refusals=0: 0.0)
    executor.refuse = [CloudRateLimited("429", code="daily_conversation_limit_reached")
                       for _ in range(20)]
    for _ in range(20):
        sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    assert (task["state"], task["attempt"], task["cycle"]) == ("pending", 0, 0)
    assert run_of(env, run_id)["state"] == "running"
    sched.tick(run_id)
    assert tasks_of(env, run_id)["build"]["state"] == "running"


def test_a_daily_limit_is_waited_out_in_longer_steps():
    daily = CloudRateLimited("Daily conversation limit of 20 reached",
                             code="daily_conversation_limit_reached")
    from orchestration.nexus_lab import refused_start_wait

    assert [refused_start_wait(daily, n) for n in range(4)] == [900, 1800, 3600, 3600]
    assert refused_start_wait(CloudRateLimited("429", retry_after=5.0)) == 30      # a floor
    assert refused_start_wait(CloudRateLimited("429", retry_after=99999.0)) == 3600


def test_a_provider_that_is_down_is_tried_again_and_again(env):
    """A temporary failure of the API is a failed start, not the end: three in
    a row use the cycle's attempts and the task waits for the next one."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    executor.refuse = [CloudUnavailable("503 Service Unavailable") for _ in range(3)]
    sched = scheduler(env, executor)
    for _ in range(3):
        sched.tick(run_id)
        sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
                 "WHERE run_id=%s AND state='pending'", run_id)
    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "waiting" and task["cycle"] == 1 and task["attempt"] == 0
    assert "CloudUnavailable" in task["wait_reason"]
    time_passes(env, run_id)
    sched.tick(run_id)
    assert tasks_of(env, run_id)["build"]["state"] == "running"
    assert run_of(env, run_id)["state"] == "running"


# ------------------------------------------------------------ time limit, stall
def test_a_conversation_past_its_time_limit_is_stopped_and_the_task_goes_on(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    first = executor.of("build")[0]
    sql(env, "UPDATE nexus_run_tasks SET started_at = now() - interval '3 hours', "
             "timeout_seconds = 60 WHERE run_id=%s", run_id)

    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "pending" and task["attempt"] == 1, "tried again, not failed"
    assert run_of(env, run_id)["state"] == "running"
    stops = env["lab"].stops(run_id)
    assert [(s["conversation_id"], s["state"]) for s in stops] == [(first, "requested")]
    assert "timed out" in stops[0]["reason"]

    # Until the first conversation is confirmed stopped it may still push, so
    # nothing else is started on its branch - not even the task's own retry.
    for _ in range(3):
        sched.tick(run_id)
    assert executor.of("build") == [first]

    assert sched.deliver_stops() == {"confirmed": 1, "pending": 0, "abandoned": 0}
    assert executor.stop_calls == [first]
    assert env["lab"].stops(run_id)[0]["state"] == "confirmed"
    sched.tick(run_id)
    assert len(executor.of("build")) == 2

    history = env["lab"].executions(run_id)
    assert [(e["conversation_id"], e["outcome"]) for e in history] == [
        (first, "timed_out"), (executor.of("build")[1], None)]


def test_a_conversation_that_shows_no_sign_of_life_is_taken_for_hung(env, monkeypatch):
    monkeypatch.setenv("NEXUS_TASK_STALL_SECONDS", "600")
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    executor.markers[conversation] = "event-1"

    def an_hour_later():
        sql(env, "UPDATE nexus_run_tasks SET progress_checked_at = now() - interval '2 minutes', "
                 "progress_at = progress_at - interval '1 hour' WHERE run_id=%s", run_id)

    sched.tick(run_id)                       # the first look records the marker
    an_hour_later()
    executor.markers[conversation] = "event-2"
    sched.tick(run_id)                       # it moved: alive
    assert tasks_of(env, run_id)["build"]["state"] == "running"
    an_hour_later()
    sched.tick(run_id)                       # the same marker an hour on: hung
    stop = env["lab"].stops(run_id)[0]
    assert stop["conversation_id"] == conversation and "no activity" in stop["reason"]
    # The task is given a new conversation (it has no branch to wait for), and
    # the one that hung is on record as that.
    task = tasks_of(env, run_id)["build"]
    second = executor.of("build")[1]
    assert (task["state"], task["attempt"], task["conversation_id"]) == ("running", 2, second)
    assert env["lab"].executions(run_id)[0]["outcome"] == "stalled"
    assert sched.deliver_stops()["confirmed"] == 1 and executor.stop_calls == [conversation]

    # A transport that cannot tell is never judged by it: the second
    # conversation gives no marker, and an hour of that is not "hung".
    sched.tick(run_id)
    an_hour_later()
    sched.tick(run_id)
    assert tasks_of(env, run_id)["build"]["conversation_id"] == second
    assert len(env["lab"].stops(run_id)) == 1


# ------------------------------------------------------- verification, rework
def test_a_result_that_does_not_verify_is_reworked_and_never_accepted(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}])
    executor = Conversations()
    verifier = Verdicts({"build": ["completed_unverified", "completed_unverified",
                                   "completed_verified"]})
    sched = scheduler(env, executor, verifier)

    sched.tick(run_id)
    for attempt in (1, 2):
        assert tasks_of(env, run_id)["build"]["attempt"] == attempt
        executor.finish(executor.of("build")[-1], "Готово, поверьте на слово.")
        sched.tick(run_id)           # not accepted, and started again in a new conversation
        task = tasks_of(env, run_id)["build"]
        assert task["state"] == "running" and task["attempt"] == attempt + 1
        assert len(executor.of("build")) == attempt + 1
        assert run_of(env, run_id)["state"] == "running"
        assert not executor.of("ship"), "nothing is built on a result that did not verify"

    executor.finish(executor.of("build")[-1], "Коммит есть.")
    sched.tick(run_id)
    assert tasks_of(env, run_id)["build"]["state"] == "completed_verified"
    sched.tick(run_id)
    executor.finish(executor.of("ship")[-1])
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"
    # Every attempt is on record with the verifier's reason.
    reasons = [e.get("reason") for e in tasks_of(env, run_id)["build"]["evidence"]
               if isinstance(e, dict) and e.get("reason")]
    assert reasons.count("scripted: completed_unverified") == 2


def test_what_is_missing_is_said_in_the_conversation_that_did_the_work(env):
    """A finished conversation whose result was not accepted still has the
    work in its workspace: it is told what is missing there, once, before a
    new conversation is started from nothing."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    executor.can_continue = True
    verifier = Verdicts({"build": [
        Verification("completed_unverified", "reported commit is not the branch head",
                     {"named_sha": "a" * 40, "remote_sha": "b" * 40}),
        "completed_unverified", "completed_verified"]})
    sched = scheduler(env, executor, verifier)
    seen: list[dict] = []
    original = executor.dispatch
    executor.dispatch = lambda task: (seen.append(dict(task)), original(task))[1]
    sched.tick(run_id)
    first = executor.of("build")[0]
    executor.finish(first, "Закоммитил локально.")
    sched.tick(run_id)                          # not accepted: the same conversation is told
    assert executor.follow_ups == [("build", first)] and executor.of("build") == [first]
    assert seen[-1]["resume_conversation_id"] == first
    assert "is not the head" in seen[-1]["resume_note"] and "b" * 40 in seen[-1]["resume_note"]
    task = tasks_of(env, run_id)["build"]
    assert (task["attempt"], task["conversation_id"]) == (2, first)

    executor.finish(first, "Всё ещё локально.")
    sched.tick(run_id)
    # Told once and still not delivered: it is left for a new conversation, and
    # told to stop so that it cannot wake up beside its successor.
    assert [s["conversation_id"] for s in env["lab"].stops(run_id)] == [first]
    assert len(executor.of("build")) == 2 and len(executor.follow_ups) == 1
    assert sched.deliver_stops()["confirmed"] == 1
    executor.finish(executor.of("build")[1])
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"
    kinds = [e["kind"] for e in env["lab"].executions(run_id)]
    assert kinds == ["start", "continue", "start"]


def test_a_check_that_could_not_be_made_is_made_again_and_costs_nothing(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    verifier = Verdicts({"build": [
        Verification("blocked", "GitHub did not answer", {}, retry="reverify"),
        "completed_verified"]})
    sched = scheduler(env, executor, verifier)
    sched.tick(run_id)
    executor.finish(executor.of("build")[0])
    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    assert task["state"] == "running" and task["attempt"] == 1, "kept as it is"
    assert "waiting to be checked" in (task["wait_reason"] or "")
    sched.tick(run_id)
    assert len(verifier.calls) == 1, "not asked again before the pause is over"
    time_passes(env, run_id)
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"
    assert len(executor.of("build")) == 1 and len(verifier.calls) == 2


# ------------------------------------------------------------------- recovery
def test_another_process_goes_on_where_the_first_one_stopped(env):
    """Nothing about a run is in a process: the one that started a task can be
    gone, and the next reads the same rows."""
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}])
    executor = Conversations()
    scheduler(env, executor, worker_id="first").tick(run_id)
    conversation = executor.of("build")[0]

    other_store = PostgresStore(DSN)
    try:
        second = LabScheduler(LabStore(other_store), executor, Verdicts(), worker_id="second")
        assert second.tick(run_id).claimed is None, "the task at work is not started again"
        executor.finish(conversation)
        second.tick(run_id)
        assert tasks_of(env, run_id)["build"]["state"] == "completed_verified"
        second.tick(run_id)
        executor.finish(executor.of("ship")[0])
        second.run_until_done(run_id)
    finally:
        other_store.close()
    assert run_of(env, run_id)["state"] == "completed"
    assert [key for key, _ in executor.started] == ["build", "ship"]


def test_a_task_claimed_by_a_worker_that_died_before_starting_is_taken_back(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claimed = env["lab"].claim_task(run_id, "dead-worker", lease_seconds=900)
    assert claimed["state"] == "dispatched" and claimed["attempt"] == 1
    executor = Conversations()
    sched = scheduler(env, executor)
    assert sched.tick(run_id).claimed is None, "its lease is still good"
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE run_id=%s", run_id)
    result = sched.tick(run_id)
    assert result.reclaimed == 1 and result.dispatched
    assert len(executor.started) == 1
    assert tasks_of(env, run_id)["build"]["state"] == "running"


def test_two_workers_never_start_one_task_twice(env, monkeypatch):
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "0")        # no limit: all may run
    keys = [f"t{n:02d}" for n in range(12)]
    run_id = start(env, [{"task_key": key, "title": key} for key in keys])
    executor = Conversations()
    stores = [PostgresStore(DSN) for _ in range(4)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(stores))

    def work(index: int) -> None:
        try:
            sched = LabScheduler(LabStore(stores[index]), executor, Verdicts(),
                                 worker_id=f"worker-{index}")
            barrier.wait(10)
            for _ in range(30):
                sched.tick(run_id)
        except BaseException as e:  # noqa: BLE001 - reported by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    for store in stores:
        store.close()
    assert not errors, errors
    started = [key for key, _ in executor.started]
    assert sorted(started) == keys, "each task was started exactly once"
    assert len(env["lab"].executions(run_id)) == len(keys)
    assert run_of(env, run_id)["total_iterations"] == len(keys)


def test_two_workers_recovering_one_waiting_task_start_it_once(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    sql(env, "UPDATE nexus_run_tasks SET state='waiting', cycle=1, "
             "retry_at = now() - interval '1 second' WHERE run_id=%s", run_id)
    executor = Conversations()
    stores = [PostgresStore(DSN) for _ in range(6)]
    barrier = threading.Barrier(len(stores))

    def work(index: int) -> None:
        sched = LabScheduler(LabStore(stores[index]), executor, Verdicts(),
                             worker_id=f"recovery-{index}")
        barrier.wait(10)
        sched.tick(run_id)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for store in stores:
        store.close()
    assert len(executor.started) == 1
    assert tasks_of(env, run_id)["build"]["attempt"] == 1


def test_no_more_tasks_are_at_work_at_once_than_the_limit(env, monkeypatch):
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "2")
    run_id = start(env, [{"task_key": f"t{n}", "title": f"t{n}"} for n in range(5)])
    executor = Conversations()
    sched = scheduler(env, executor)
    for _ in range(6):
        sched.tick(run_id)
    assert len(executor.started) == 2
    executor.finish(executor.started[0][1])
    sched.tick(run_id)
    sched.tick(run_id)
    assert len(executor.started) == 3


def test_the_runs_own_budget_renews_instead_of_ending_it(env):
    """A durable run that has used its task starts pauses and goes on with
    them restored; the count of every start it ever made is kept."""
    run_id = start(env, [{"task_key": f"t{n}", "title": f"t{n}"} for n in range(3)],
                   max_iterations=2)
    executor = Conversations()
    sched = scheduler(env, executor)
    for _ in range(2):
        sched.tick(run_id)
        executor.finish(executor.started[-1][1])
        sched.tick(run_id)
    result = sched.tick(run_id)
    run = run_of(env, run_id)
    assert run["state"] == "running" and run["resume_at"] is not None, (run, result)
    assert "iteration budget used" in run["wait_reason"]
    assert len(executor.started) == 2
    time_passes(env, run_id)
    sched.tick(run_id)
    run = run_of(env, run_id)
    assert run["cycle"] == 1 and run["total_iterations"] == 3 and run["iteration"] == 1
    executor.finish(executor.started[-1][1])
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"


def test_a_durable_run_is_not_ended_by_a_deadline(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}], deadline_seconds=60)
    sql(env, "UPDATE nexus_runs SET deadline_at = now() - interval '1 day', "
             "created_at = now() - interval '2 days' WHERE id=%s", run_id)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    assert executor.of("build"), "a durable run starts work whatever the clock says"
    executor.finish(executor.of("build")[0])
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "completed"

    # A bounded run is still held to it.
    goal = str(env["projects"].create_goal(ALICE, env["project"], "Another")[0]["id"])
    bounded = str(env["lab"].start_run(ALICE, env["project"], goal, policy="bounded")["id"])
    env["lab"].add_task(ALICE, env["project"], bounded, task_key="x", title="x")
    sql(env, "UPDATE nexus_runs SET deadline_at = now() - interval '1 minute' WHERE id=%s",
        bounded)
    sched.run_until_done(bounded)
    assert run_of(env, bounded)["state"] == "failed"
    assert run_of(env, bounded)["last_error"] == "deadline passed"


# --------------------------------------------------------------------- cancel
def test_cancel_stops_the_conversations_hides_the_run_and_keeps_every_row(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}],
                   repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]

    cancelled = env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    assert cancelled["state"] == "cancelled" and cancelled["hidden"] is True
    assert cancelled["cancelled_by"] == ALICE and cancelled["cancelled_at"] is not None

    # Out of sight...
    assert env["lab"].list_runs(ALICE, env["project"]) == []
    # ...and everything it recorded is still there.
    assert [r["id"] for r in env["lab"].list_runs(ALICE, env["project"],
                                                  include_hidden=True)] == [cancelled["id"]]
    assert env["lab"].get_run(ALICE, env["project"], run_id)["state"] == "cancelled"
    tasks = tasks_of(env, run_id)
    assert {key: t["state"] for key, t in tasks.items()} == {"build": "blocked",
                                                            "ship": "blocked"}
    assert tasks["build"]["conversation_id"] == conversation
    assert all(t["lease_expires"] is None for t in tasks.values()), "no lease is left behind"
    history = env["lab"].executions(run_id)
    assert [(e["conversation_id"], e["outcome"]) for e in history] == [(conversation, "cancelled")]
    events = sql(env, "SELECT kind, actor FROM nexus_project_events WHERE resource_id=%s "
                      "ORDER BY created_at", run_id)
    assert ("run.cancelled", ALICE) in [(e["kind"], e["actor"]) for e in events]

    # The executor is really told, and the stop is confirmed.
    stop = env["lab"].stops(run_id)[0]
    assert (stop["conversation_id"], stop["state"], stop["reason"]) == (
        conversation, "requested", "run cancelled")
    assert sched.deliver_stops() == {"confirmed": 1, "pending": 0, "abandoned": 0}
    assert executor.stop_calls == [conversation]
    assert executor.states[conversation] == "paused"
    stop = env["lab"].stops(run_id)[0]
    assert stop["state"] == "confirmed" and stop["signalled"] and stop["observed"] == "paused"

    # Nothing more is ever started or even looked at for this run.
    before = len(executor.started)
    for _ in range(3):
        assert sched.tick(run_id).finished
    assert len(executor.started) == before


def test_cancel_twice_is_the_same_as_once(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    scheduler(env, executor).tick(run_id)
    first = env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    again = env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    assert again["state"] == "cancelled"
    assert again["cancelled_at"] == first["cancelled_at"] and again["hidden_at"] == first["hidden_at"]
    assert len(env["lab"].stops(run_id)) == 1, "one stop per conversation, however often asked"


def test_a_cancelled_run_does_not_hold_its_goal_or_its_branch(env):
    """A new run of the same goal can start at once; but a task of it is not
    put on the branch until the cancelled run's agent is confirmed stopped -
    a stopped agent that is still pushing would move the head under it."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    old = executor.of("build")[0]
    executor.deaf.add(old)                       # it does not obey the first signal
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")

    again = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    assert again != run_id
    sched.tick(again)
    assert executor.of("build") == [old], "the branch is not free yet"

    assert sched.deliver_stops()["pending"] == 1
    assert env["lab"].stops(run_id)[0]["signalled"] is True
    sched.tick(again)
    assert executor.of("build") == [old]

    executor.deaf.clear()                        # the next signal takes
    time_passes(env, run_id)
    assert sched.deliver_stops()["confirmed"] == 1
    assert executor.stop_calls == [old, old]
    sched.tick(again)
    assert len(executor.of("build")) == 2


def test_an_agent_that_goes_on_after_the_signal_keeps_its_branch_for_hours(env):
    """An agent in the middle of a long step takes the signal and lets go only
    when the step ends. Its branch is not given to another agent after a few
    looks: the looks get rarer, and giving the stop up - which is what frees
    the branch - takes hours and says what was seen."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    old = executor.of("build")[0]
    executor.deaf.add(old)
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    again = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)

    waits = []
    for _ in range(MAX_STOP_ATTEMPTS - 1):
        assert sched.deliver_stops() == {"confirmed": 0, "pending": 1, "abandoned": 0}
        waits.append(round(float(sql(
            env, "SELECT extract(epoch FROM next_attempt_at - now()) AS s "
                 "FROM nexus_executor_stops WHERE run_id=%s", run_id)[0]["s"])))
        time_passes(env, run_id)
    # A pause usually takes seconds, so the first looks are close together...
    assert waits[:6] == [5] * 6
    # ...then rarer, and never further apart than ten minutes.
    assert waits[6:13] == [10, 20, 40, 80, 160, 320, 600] and set(waits[13:]) == {600}
    assert sum(waits) > 4 * 3600, "given up within hours, not minutes"

    stop = env["lab"].stops(run_id)[0]
    assert stop["state"] == "requested" and stop["signalled"] is True
    sched.tick(again)
    assert executor.of("build") == [old], "the branch is still held"

    # The last look. The stop is given up, says why, and the branch is free.
    assert sched.deliver_stops() == {"confirmed": 0, "pending": 0, "abandoned": 1}
    stop = env["lab"].stops(run_id)[0]
    assert stop["state"] == "abandoned" and stop["observed"] == "running"
    assert "still reports running" in stop["last_error"]
    assert sched.deliver_stops() == {"confirmed": 0, "pending": 0, "abandoned": 0}
    sched.tick(again)
    assert len(executor.of("build")) == 2
    # Nothing of the cancelled run was removed for it.
    assert env["lab"].get_run(ALICE, env["project"], run_id)["state"] == "cancelled"


def test_a_stop_that_cannot_be_delivered_is_owed_until_it_is(env):
    """The signal is a row. A failure to send it, or a restart, changes
    nothing about its being owed."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")

    executor.stop_errors = [CloudUnavailable("503"), CloudError("boom")]
    for _ in range(2):
        assert sched.deliver_stops() == {"confirmed": 0, "pending": 1, "abandoned": 0}
        time_passes(env, run_id)
    stop = env["lab"].stops(run_id)[0]
    assert stop["state"] == "requested" and stop["attempts"] == 2 and "boom" in stop["last_error"]

    # Another process, later.
    other = PostgresStore(DSN)
    try:
        fresh = LabScheduler(LabStore(other), executor, Verdicts(), worker_id="after-restart")
        assert fresh.deliver_stops()["confirmed"] == 1
    finally:
        other.close()
    assert executor.stop_calls == [conversation]


def test_a_conversation_that_is_gone_needs_no_stop(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    del executor.states[conversation]
    assert sched.deliver_stops()["confirmed"] == 1
    stop = env["lab"].stops(run_id)[0]
    assert stop["observed"] == "missing" and executor.stop_calls == []


def test_cancel_and_completion_at_the_same_moment_end_one_way(env):
    # Cancel first: the scheduler's completion finds the run cancelled and
    # leaves it; no report is written for it.
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    executor.finish(executor.of("build")[0])
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    assert env["lab"].set_run_state_internal(run_id, "completed") is None
    assert sched.tick(run_id).finished
    assert run_of(env, run_id)["state"] == "cancelled"

    # Completion first: the cancel is refused, and the run stays completed and
    # in sight.
    goal = str(env["projects"].create_goal(ALICE, env["project"], "Another")[0]["id"])
    other = str(env["lab"].start_run(ALICE, env["project"], goal, policy="durable")["id"])
    env["lab"].add_task(ALICE, env["project"], other, task_key="x", title="x")
    sched.tick(other)
    executor.finish(executor.of("x")[0])
    sched.run_until_done(other)
    assert run_of(env, other)["state"] == "completed"
    with pytest.raises(InvalidTransition):
        env["lab"].set_run_state(ALICE, env["project"], other, "cancelled")
    assert run_of(env, other)["hidden"] is False


def test_a_cancel_between_the_claim_and_the_start_still_stops_the_conversation(env):
    """The run is cancelled while the executor is starting a conversation for
    a task that was just claimed. The conversation that comes back is not
    left running for a run that no longer exists in anyone's sight."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Conversations()
    original = executor.dispatch

    def cancel_then_start(task):
        env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
        return original(task)

    executor.dispatch = cancel_then_start
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    assert tasks_of(env, run_id)["build"]["state"] == "blocked"
    assert [s["conversation_id"] for s in env["lab"].stops(run_id)] == [conversation]
    assert sched.deliver_stops()["confirmed"] == 1
    assert executor.states[conversation] == "paused"


def test_only_a_run_that_is_over_can_be_hidden_and_it_can_be_shown_again(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}], policy="bounded")
    with pytest.raises(InvalidTransition):
        env["lab"].set_hidden(ALICE, env["project"], run_id, True)
    env["lab"].set_run_state_internal(run_id, "failed", last_error="nothing worked")
    assert env["lab"].set_hidden(ALICE, env["project"], run_id, True)["hidden"] is True
    assert env["lab"].list_runs(ALICE, env["project"]) == []
    assert env["lab"].set_hidden(ALICE, env["project"], run_id, False)["hidden"] is False
    assert len(env["lab"].list_runs(ALICE, env["project"])) == 1


def test_a_stranger_cannot_cancel_or_see_a_run(env):
    from orchestration.cloud_store import NotFound

    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    with pytest.raises(NotFound):
        env["lab"].set_run_state("user:mallory", env["project"], run_id, "cancelled")
    with pytest.raises(NotFound):
        env["lab"].list_runs("user:mallory", env["project"], include_hidden=True)
    assert run_of(env, run_id)["state"] == "running"


# ------------------------- left behind by a switch or a failover (``abandoned``)
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


def _account_b(env) -> str:
    sql(env, "INSERT INTO nexus_openhands_accounts(id, principal, label, base_url, "
             "api_key_encrypted) VALUES (%s, %s, 'B', 'https://example.test', %s)",
        ACCOUNT_B, ALICE, b"\x00")
    return ACCOUNT_B


def test_what_a_switch_left_behind_is_stopped_even_after_its_run_has_ended(env):
    """A switch records the old conversation in the task's ``abandoned`` list,
    and the run's own pass stops it. A run that is cancelled a moment later has
    no pass any more - and the list would hold the branch against every later
    run, for ever. The worker's loop stops what such a run left behind."""
    account = _account_b(env)
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    old = executor.of("build")[0]
    env["lab"].switch_task_executor(tasks_of(env, run_id)["build"]["id"],
                                    target_account_id=account)
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    assert [e["conversation_id"] for e in env["lab"].pending_stops(run_id)] == [old]
    assert env["lab"].ended_runs_with_abandoned() == [run_id]

    again = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    for _ in range(3):
        assert sched.tick(run_id).finished        # an ended run is not advanced
    sched.tick(again)
    assert executor.of("build") == [old], "the branch is held by what was left behind"

    sched.deliver_stops()
    assert executor.stop_calls == [old] and executor.states[old] == "paused"
    assert env["lab"].pending_stops(run_id) == []
    assert env["lab"].ended_runs_with_abandoned() == []
    sched.tick(again)
    assert len(executor.of("build")) == 2


def test_a_run_that_is_alive_stops_what_it_left_behind_itself(env):
    """Nothing is drained twice: a live run's list is its own pass's business,
    and the loop's look at ended runs does not see it."""
    account = _account_b(env)
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    old = executor.of("build")[0]
    env["lab"].switch_task_executor(tasks_of(env, run_id)["build"]["id"],
                                    target_account_id=account)
    assert env["lab"].ended_runs_with_abandoned() == []
    sched.deliver_stops()
    assert executor.stop_calls == []
    sched.tick(run_id)                            # stops the old one, starts the new
    assert executor.stop_calls == [old]
    assert len(executor.of("build")) == 2


def _cloud_conversation(server, client) -> str:
    """A conversation at work on the stand-in Cloud, started through the client."""
    start_task = client.start_conversation(initial_message="do the thing", title="t")
    conversation = start_task.app_conversation_id or client.get_start_task(
        start_task.id).app_conversation_id
    server.cloud.conversations[conversation]["execution_status"] = "running"
    return conversation


def test_the_cloud_transport_stops_a_conversation_by_pausing_its_own_sandbox():
    """Over real HTTP against the stand-in Cloud. The Cloud has no
    conversation-scoped stop; its own client pauses the sandbox, and a
    conversation started through this transport has one of its own."""
    from orchestration.nexus_lab import OpenHandsExecutor
    from tests.fake_cloud import FakeCloudServer

    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key="test-key", timeout=10.0)
        executor = OpenHandsExecutor(client)
        target = {"conversation_id": None, "project_id": None}

        conversation = _cloud_conversation(server, client)
        sandbox = server.cloud.conversations[conversation]["sandbox_id"]
        assert executor.conversation_state({**target, "conversation_id": conversation}) \
            == "running"
        assert executor.stop(None, conversation) is True
        assert server.cloud.paused == [sandbox]
        assert executor.conversation_state({**target, "conversation_id": conversation}) \
            == "paused"
        assert conversation in server.cloud.conversations, "the transcript is kept"

        # A pause the service refuses stops nothing, and is not taken for a stop.
        second = _cloud_conversation(server, client)
        server.cloud.pause_status = 503
        assert client.stop_conversation(second) is False
        assert executor.stop(None, second) is False
        server.cloud.pause_status = 0

        # A conversation that is gone has nothing left to stop: a failover away
        # from it must not hold its branch for ever.
        assert client.stop_conversation("0" * 32) is False
        assert executor.stop(None, "0" * 32) is True


def test_a_service_that_cannot_be_reached_has_stopped_nothing():
    from orchestration.nexus_lab import OpenHandsExecutor

    client = CloudClient("http://127.0.0.1:9", api_key="test-key", timeout=2.0)
    assert client.stop_conversation("whatever") is False
    assert OpenHandsExecutor(client).stop(None, "whatever") is False
