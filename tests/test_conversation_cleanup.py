"""What a run created in OpenHands is deleted when the run is over.

Three levels, each on the real thing below it:

* the scheduler and the store on PostgreSQL, with an executor whose
  conversations do what the test says (``tests/test_durable_runs.py``) - which
  conversations become owed a deletion, when, and what a failure does;
* the clients over real HTTP against the stand-in Cloud and the stand-in
  sandbox - what "deleted" is taken from, on both transports;
* the worker's own pass.

Time is not waited for: "the run ended a day ago" is the run's ``updated_at``
moved into the past, as a clock would have moved it.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import lab_worker  # noqa: E402
from orchestration.cloud_client import (  # noqa: E402
    CloudAuthError,
    CloudClient,
    CloudError,
    CloudNotFound,
    CloudRateLimited,
    CloudUnavailable,
)
from orchestration.cloud_direct import CloudDirectClient  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    CLEANUP_WAIT_CEILING,
    OpenHandsExecutor,
    cleanup_retention_seconds,
    cleanup_retry_wait,
)
from orchestration.nexus_lab_store import SANDBOX_QUIET_SECONDS  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.test_durable_runs import (  # noqa: E402
    ACCOUNT_B,
    ALICE,
    DSN,
    REPO,
    Conversations,
    Verdicts,
    _account_b,
    fail_attempt,
    reset_schema,
    run_of,
    scheduler,
    sql,
    start,
    tasks_of,
)

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

DAY = 24 * 3600


@pytest.fixture
def env():
    """A project with a goal on an empty database, as in ``test_durable_runs``."""
    reset_schema()
    store = PostgresStore(DSN)
    projects = ProjectService(store)
    project = str(projects.create(ALICE, "Cleanup")["id"])
    goal = str(projects.create_goal(ALICE, project, "Ship it")[0]["id"])
    yield {"store": store, "projects": projects, "project": project, "goal": goal,
           "lab": LabStore(store)}
    store.close()


class Deletable(Conversations):
    """Conversations that can also be deleted.

    ``remove`` is the executor's deletion: the conversation goes, and what
    became of it is said - ``deleted``, or ``missing`` when it was not there.
    ``remove_errors`` holds errors to raise instead, first in first out.
    """

    def __init__(self):
        super().__init__()
        self.removed: list[tuple[str | None, str]] = []      # (account, conversation)
        self.remove_errors: list[Exception] = []
        self.remove_calls = 0

    def remove(self, account_id, conversation_id, project_id=None):
        self.remove_calls += 1
        if self.remove_errors:
            raise self.remove_errors.pop(0)
        conversation = str(conversation_id)
        if self.states.pop(conversation, None) is None:
            return "missing"
        self.removed.append((account_id, conversation))
        return "deleted"

    def gone(self) -> list[str]:
        return [conversation for _account, conversation in self.removed]


def ended(env, run_id, seconds: float) -> None:
    """The run reached the state it is in *seconds* ago."""
    sql(env, "UPDATE nexus_runs SET updated_at = now() - %s * interval '1 second' WHERE id=%s",
        seconds, run_id)


def cleanups(env, run_id) -> dict[str, dict]:
    return {row["conversation_id"]: row for row in env["lab"].cleanups(run_id)}


def retry_now(env) -> None:
    """Every wait before another try at a deletion is over."""
    sql(env, "UPDATE nexus_conversation_cleanups SET next_attempt_at = now() - interval "
             "'1 second' WHERE state='requested'")


def complete(env, executor, sched, run_id) -> None:
    """Every task is started, delivers and verifies; the run completes."""
    for _ in range(20):
        sched.tick(run_id)
        for _key, conversation in executor.started:
            if executor.states.get(conversation) == "running":
                executor.finish(conversation)
        if run_of(env, run_id)["state"] == "completed":
            return
    raise AssertionError(f"the run did not complete: {run_of(env, run_id)}")


# ------------------------------------------------------- when, and what
def test_a_completed_runs_conversations_are_deleted_a_day_after_it_ended(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    build, ship = executor.of("build")[0], executor.of("ship")[0]

    # Just over: everything is kept, and nothing is even written down.
    assert sched.remove_conversations() == {
        "queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}
    ended(env, run_id, DAY - 60)
    assert sched.remove_conversations()["queued"] == 0
    assert executor.remove_calls == 0 and cleanups(env, run_id) == {}

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations() == {
        "queued": 2, "deleted": 2, "missing": 0, "pending": 0, "abandoned": 0}
    assert sorted(executor.gone()) == sorted([build, ship])
    rows = cleanups(env, run_id)
    assert {row["state"] for row in rows.values()} == {"deleted"}
    assert all(row["finished_at"] is not None and row["attempts"] == 1
               and row["last_error"] is None for row in rows.values())

    # Done once: another pass asks the service for nothing.
    assert sched.remove_conversations() == {
        "queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}
    assert executor.remove_calls == 2

    # The run itself, its tasks and its history are untouched.
    assert run_of(env, run_id)["state"] == "completed"
    assert tasks_of(env, run_id)["build"]["conversation_id"] == build
    assert len(env["lab"].executions(run_id)) == 2


def test_every_attempts_conversation_is_deleted_not_only_the_last(env):
    """A task that was tried three times left three conversations."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts({"build": [
        "completed_unverified", "completed_unverified", "completed_verified"]}))
    complete(env, executor, sched, run_id)
    assert len(executor.of("build")) == 3

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 3
    assert sorted(executor.gone()) == sorted(executor.of("build"))


def test_conversations_named_only_in_a_tasks_evidence_are_found(env):
    """A run from before the execution history existed: its earlier attempts
    are named in the tasks' evidence and nowhere else."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts({"build": [
        "completed_unverified", "completed_verified"]}))
    complete(env, executor, sched, run_id)
    sql(env, "DELETE FROM nexus_task_executions WHERE run_id=%s", run_id)

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 2
    assert sorted(executor.gone()) == sorted(executor.of("build"))


def test_a_run_that_is_at_work_keeps_its_conversations_however_old(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts({"build": [
        "completed_unverified", "completed_verified"]}))
    sched.tick(run_id)
    executor.finish(executor.of("build")[0])
    sched.tick(run_id)                      # not accepted: a second conversation is at work
    assert len(executor.of("build")) == 2 and run_of(env, run_id)["state"] == "running"

    ended(env, run_id, 30 * DAY)
    assert sched.remove_conversations()["queued"] == 0
    assert executor.remove_calls == 0, "not even the attempt that is over"

    for state in ("paused",):
        env["lab"].set_run_state(ALICE, env["project"], run_id, state)
        ended(env, run_id, 30 * DAY)
        assert sched.remove_conversations()["queued"] == 0, state


def test_a_cancelled_runs_conversations_are_stopped_first_and_deleted_a_day_later(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}], repository=REPO)
    executor = Deletable()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")

    assert sched.deliver_stops()["confirmed"] == 1
    assert sched.remove_conversations()["queued"] == 0
    assert executor.states[conversation] == "paused", "stopped, and kept for now"

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 1
    assert executor.gone() == [conversation]


def test_a_conversation_that_never_reached_a_task_is_deleted_too(env):
    """Started by a worker that had lost the task meanwhile: it is on no task
    and in no history, only in the stop signal it was sent."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    task = tasks_of(env, run_id)["build"]
    executor.states["stray"] = "running"
    env["lab"].request_stop(task, "stray", reason="started by a worker that had lost the task")
    env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 2
    assert sorted(executor.gone()) == sorted(["stray", executor.of("build")[0]])


def test_a_blocked_run_is_cleaned_up_and_a_resumed_one_is_left_alone(env):
    """A run that stopped and waits for a person is over as far as its
    conversations go: resuming it starts new ones. But once it is resumed it
    is at work again, and what is owed waits until it is over again."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}], policy="bounded")
    executor = Deletable()
    sched = scheduler(env, executor)
    first = fail_attempt(sched, executor, run_id, "build")
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "blocked"

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations(limit=0)["queued"] == 1      # written down, not yet made
    env["lab"].set_run_state(ALICE, env["project"], run_id, "running")
    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations() == {
        "queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}
    assert executor.remove_calls == 0

    second = fail_attempt(sched, executor, run_id, "build")
    sched.run_until_done(run_id)
    assert run_of(env, run_id)["state"] == "blocked" and second != first
    assert sched.remove_conversations()["deleted"] == 0, "blocked a moment ago"
    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 2
    assert sorted(executor.gone()) == sorted([first, second])


def test_a_conversation_a_task_will_be_continued_in_is_kept(env):
    """``resume_conversation_id``: the task's next attempt is made in it."""
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "docs", "title": "Docs"}], policy="bounded")
    executor = Deletable()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    sched.tick(run_id)
    build, docs = executor.of("build")[0], executor.of("docs")[0]
    for conversation in (build, docs):
        executor.states[conversation] = "error"
    sched.run_until_done(run_id)
    # The run stands; "docs" is to go on in the conversation it had.
    sql(env, "UPDATE nexus_runs SET state='blocked' WHERE id=%s", run_id)
    sql(env, "UPDATE nexus_run_tasks SET state='pending', resume_conversation_id=%s "
             "WHERE run_id=%s AND task_key='docs'", docs, run_id)

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["deleted"] == 1
    assert executor.gone() == [build] and docs in executor.states

    # Once nothing is to be continued in it any more, it goes as well.
    sql(env, "UPDATE nexus_run_tasks SET state='failed', resume_conversation_id=NULL "
             "WHERE run_id=%s AND task_key='docs'", run_id)
    assert sched.remove_conversations()["deleted"] == 1
    assert sorted(executor.gone()) == sorted([build, docs])


def test_a_conversation_at_work_in_a_run_that_stands_is_not_deleted(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}], policy="bounded")
    executor = Deletable()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    conversation = executor.of("build")[0]
    sql(env, "UPDATE nexus_runs SET state='blocked' WHERE id=%s", run_id)
    assert tasks_of(env, run_id)["build"]["state"] == "running"

    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations()["queued"] == 0
    assert executor.states[conversation] == "running"


# --------------------------------------------------------- what a failure does
def test_a_deletion_that_fails_is_owed_says_why_and_is_made_later(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    conversation = executor.of("build")[0]
    ended(env, run_id, DAY + 60)

    executor.remove_errors = [CloudError("the account still reports the conversation")]
    assert sched.remove_conversations() == {
        "queued": 1, "deleted": 0, "missing": 0, "pending": 1, "abandoned": 0}
    row = cleanups(env, run_id)[conversation]
    assert row["state"] == "requested" and row["finished_at"] is None
    assert row["last_error"] == "CloudError: the account still reports the conversation"
    assert conversation in executor.states, "nothing is called deleted that was not"

    # Not hammered: nothing is tried until the wait is over.
    assert sched.remove_conversations()["pending"] == 0 and executor.remove_calls == 1
    retry_now(env)
    assert sched.remove_conversations()["deleted"] == 1
    row = cleanups(env, run_id)[conversation]
    assert (row["state"], row["attempts"], row["last_error"]) == ("deleted", 2, None)


def test_a_deletion_is_never_given_up(env):
    """A key that is rejected today may be put right tomorrow: the deletion
    stays owed, tried less and less often."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    conversation = executor.of("build")[0]
    ended(env, run_id, DAY + 60)

    for attempt in range(1, 31):
        executor.remove_errors = [CloudAuthError("DELETE -> 401")]
        retry_now(env)
        assert sched.remove_conversations()["pending"] == 1, attempt
    row = cleanups(env, run_id)[conversation]
    assert (row["state"], row["attempts"]) == ("requested", 30)
    wait = sql(env, "SELECT extract(epoch FROM next_attempt_at - now()) AS s "
                    "FROM nexus_conversation_cleanups WHERE id=%s", row["id"])[0]["s"]
    assert CLEANUP_WAIT_CEILING - 120 < float(wait) <= CLEANUP_WAIT_CEILING

    retry_now(env)
    assert sched.remove_conversations()["deleted"] == 1


def test_the_wait_between_tries_grows_and_honours_the_service():
    assert [cleanup_retry_wait(n) for n in (1, 2, 3, 4)] == [60.0, 120.0, 240.0, 480.0]
    assert cleanup_retry_wait(40) == CLEANUP_WAIT_CEILING
    refused = CloudRateLimited("429", retry_after=900.0)
    assert cleanup_retry_wait(1, refused) == 900.0
    assert cleanup_retry_wait(1, CloudRateLimited("429", retry_after=1.0)) == 60.0


def test_a_service_that_cannot_be_reached_ends_the_pass(env):
    run_id = start(env, [{"task_key": key, "title": key} for key in ("a", "b", "c")])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    ended(env, run_id, DAY + 60)

    executor.remove_errors = [CloudUnavailable("DELETE: TimeoutError")]
    assert sched.remove_conversations() == {
        "queued": 3, "deleted": 0, "missing": 0, "pending": 1, "abandoned": 0}
    assert executor.remove_calls == 1, "the other two would only wait out the same timeout"
    assert sched.remove_conversations()["deleted"] == 2
    retry_now(env)
    assert sched.remove_conversations()["deleted"] == 1


def test_a_conversation_someone_already_removed_is_recorded_as_missing(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    conversation = executor.of("build")[0]
    del executor.states[conversation]           # deleted by hand in OpenHands
    ended(env, run_id, DAY + 60)

    assert sched.remove_conversations() == {
        "queued": 1, "deleted": 0, "missing": 1, "pending": 0, "abandoned": 0}
    assert cleanups(env, run_id)[conversation]["state"] == "missing"


def test_no_more_than_the_batch_is_deleted_in_one_pass(env):
    run_id = start(env, [{"task_key": f"t{n}", "title": f"T{n}"} for n in range(7)])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    ended(env, run_id, DAY + 60)

    assert sched.remove_conversations(limit=3)["deleted"] == 3
    assert sched.remove_conversations(limit=3, sweep=False)["deleted"] == 3
    assert sched.remove_conversations(limit=3)["deleted"] == 1
    assert len(executor.gone()) == 7


def test_two_workers_never_delete_one_conversation_twice(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations(limit=0)["queued"] == 1

    first = env["lab"].claim_cleanup("worker-a", retention_seconds=DAY)
    assert first is not None
    assert env["lab"].claim_cleanup("worker-b", retention_seconds=DAY) is None
    # A worker that died with it in hand gives it up when the lease runs out.
    sql(env, "UPDATE nexus_conversation_cleanups SET lease_expires = now() - interval '1 second'")
    assert env["lab"].claim_cleanup("worker-b", retention_seconds=DAY)["id"] == first["id"]


def test_a_worker_that_is_stopping_deletes_nothing_more(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    complete(env, executor, scheduler(env, executor), run_id)
    ended(env, run_id, DAY + 60)

    stopping = scheduler(env, executor, stopping=lambda: True)
    assert stopping.remove_conversations()["deleted"] == 0 and executor.remove_calls == 0
    assert scheduler(env, executor).remove_conversations()["deleted"] == 1


# ------------------------------------------------------------- whose key
def test_a_conversation_is_deleted_on_the_connection_it_ran_on(env):
    account = _account_b(env)
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "docs", "title": "Docs"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    build, docs = executor.of("build")[0], executor.of("docs")[0]
    sql(env, "UPDATE nexus_task_executions SET openhands_account_id=%s, executor_label='B' "
             "WHERE conversation_id=%s", account, build)
    ended(env, run_id, DAY + 60)

    assert sched.remove_conversations()["deleted"] == 2
    assert sorted(executor.removed, key=lambda item: item[1]) == sorted(
        [(account, build), (None, docs)], key=lambda item: item[1])
    rows = cleanups(env, run_id)
    assert rows[build]["executor_label"] == "B" and rows[docs]["executor_label"] == ""


def test_a_conversation_of_a_removed_connection_is_given_up_and_says_so(env):
    """Its key went with the connection. Asking with another key would be
    answered "no such conversation" - for a conversation that is still there."""
    account = _account_b(env)
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    conversation = executor.of("build")[0]
    sql(env, "UPDATE nexus_task_executions SET openhands_account_id=%s, executor_label='B' "
             "WHERE conversation_id=%s", account, conversation)
    ended(env, run_id, DAY + 60)
    assert sched.remove_conversations(limit=0)["queued"] == 1
    sql(env, "DELETE FROM nexus_openhands_accounts WHERE id=%s", account)

    assert sched.remove_conversations() == {
        "queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 1}
    row = cleanups(env, run_id)[conversation]
    assert row["state"] == "abandoned" and "(B) has been removed" in row["last_error"]
    assert executor.remove_calls == 0 and conversation in executor.states


def test_the_executor_deletes_with_the_key_of_the_connection():
    """With no router at all - the connection may have been switched off since
    the run - the conversation is still deleted with its own key."""
    calls: list[tuple[str, str]] = []

    class Client:
        def __init__(self, name, outcome="deleted"):
            self.name, self.outcome = name, outcome

        def remove_conversation(self, conversation_id):
            calls.append((self.name, conversation_id))
            return self.outcome

    executor = OpenHandsExecutor(Client("server"))
    executor.account_client = lambda account_id: Client(f"account:{account_id}")
    assert executor.remove(None, "c1") == "deleted"
    assert executor.remove(ACCOUNT_B, "c2") == "deleted"
    assert calls == [("server", "c1"), (f"account:{ACCOUNT_B}", "c2")]

    # A client that cannot delete, or answers something else, has deleted nothing.
    with pytest.raises(CloudError, match="cannot delete"):
        OpenHandsExecutor(object()).remove(None, "c3")
    with pytest.raises(CloudError, match="answered 'ok'"):
        OpenHandsExecutor(Client("server", outcome="ok")).remove(None, "c4")


# ------------------------------------------------------------- the setting
def test_the_retention_setting(env, monkeypatch):
    name = "NEXUS_OPENHANDS_CLEANUP_HOURS"
    monkeypatch.delenv(name, raising=False)
    assert cleanup_retention_seconds() == DAY
    for value, seconds in (("0", 0.0), ("1.5", 5400.0), ("72", 3 * DAY), (" 24 ", DAY)):
        monkeypatch.setenv(name, value)
        assert cleanup_retention_seconds() == seconds, value
    for value in ("off", "OFF", "never", "false"):
        monkeypatch.setenv(name, value)
        assert cleanup_retention_seconds() is None, value
    # A value that is not a number of hours must not turn into "delete at once".
    for value in ("24h", "soon", "-1", "nan", "inf"):
        monkeypatch.setenv(name, value)
        assert cleanup_retention_seconds() == DAY, value

    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)

    monkeypatch.setenv(name, "off")
    ended(env, run_id, 30 * DAY)
    assert sched.remove_conversations()["queued"] == 0 and executor.remove_calls == 0

    monkeypatch.setenv(name, "0")
    ended(env, run_id, 0)
    assert sched.remove_conversations()["deleted"] == 1


def test_the_runs_summary_says_what_became_of_its_conversations(env):
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "docs", "title": "Docs"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    summary = env["lab"].cleanup_summary(run_id, retention_seconds=DAY)
    assert summary == {"enabled": True, "due_at": None, "requested": 0, "deleted": 0,
                       "missing": 0, "abandoned": 0, "last_error": None}

    complete(env, executor, sched, run_id)
    summary = env["lab"].cleanup_summary(run_id, retention_seconds=DAY)
    due = sql(env, "SELECT updated_at + interval '1 day' AS due FROM nexus_runs WHERE id=%s",
              run_id)[0]["due"]
    assert summary["due_at"] == due and summary["requested"] == 0
    assert env["lab"].cleanup_summary(run_id, retention_seconds=None)["enabled"] is False

    ended(env, run_id, DAY + 60)
    executor.remove_errors = [CloudError("refused")]
    sched.remove_conversations()
    summary = env["lab"].cleanup_summary(run_id, retention_seconds=DAY)
    assert (summary["requested"], summary["deleted"]) == (1, 1)
    assert summary["last_error"] == "CloudError: refused"


# ------------------------------------------------ a sandbox that is in use
def test_a_sandbox_just_handed_to_a_conversation_keeps_what_it_holds(env):
    """The service removes a sandbox with the last conversation it knows of
    on it, and learns of a new conversation a moment after its start."""
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    executor = Deletable()
    sched = scheduler(env, executor)
    complete(env, executor, sched, run_id)
    conversation = executor.of("build")[0]
    store = env["store"]
    store.save_sandbox("sbx-1", agent_server_url="http://sbx-1.test", session_api_key="k")
    store.save_conversation_route(conversation, sandbox_id="sbx-1",
                                  agent_server_url="http://sbx-1.test", session_api_key="k")
    ended(env, run_id, DAY + 60)

    assert sched.remove_conversations() == {
        "queued": 1, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}
    assert executor.remove_calls == 0

    sql(env, "UPDATE cloud_sandboxes SET updated_at = now() - %s * interval '1 second'",
        SANDBOX_QUIET_SECONDS + 5)
    assert sched.remove_conversations()["deleted"] == 1


def test_reusing_a_sandbox_writes_down_that_it_was_handed_out(env):
    with FakeCloudServer() as cloud, FakeServer() as sandbox:
        cloud.cloud.agent_server_url = sandbox.base_url
        cloud.cloud.sandbox_session_key = sandbox.runtime.expected_key
        client = CloudDirectClient(CloudClient(cloud.base_url, api_key="k", timeout=10.0),
                                   env["store"], wait_interval=0.01, timeout=10.0)
        first = client.start_conversation(initial_message="one", title="t1")
        sql(env, "UPDATE cloud_sandboxes SET updated_at = now() - interval '1 hour'")
        second = client.start_conversation(initial_message="two", title="t2")
        assert second.sandbox_id == first.sandbox_id
        age = sql(env, "SELECT extract(epoch FROM now() - updated_at) AS s "
                       "FROM cloud_sandboxes WHERE sandbox_id=%s", first.sandbox_id)[0]["s"]
        assert float(age) < 60


# ------------------------------------------------- the clients, over HTTP
def _app_conversation(server, client) -> str:
    started = client.start_conversation(initial_message="do the thing", title="t")
    return started.app_conversation_id or client.get_start_task(started.id).app_conversation_id


def test_the_app_transport_deletes_and_checks_that_the_account_no_longer_has_it():
    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key="test-key", timeout=10.0)
        conversation = _app_conversation(server, client)

        assert client.remove_conversation(conversation) == "deleted"
        assert server.cloud.deleted == [conversation]
        assert conversation not in server.cloud.conversations
        # Asked for after the DELETE: the service's own word that it is gone.
        calls = [call for call in server.cloud.calls if conversation in call[1]
                 or call[1] == "/api/v1/app-conversations"]
        assert calls[-2:] == [("DELETE", f"/api/v1/app-conversations/{conversation}"),
                              ("GET", "/api/v1/app-conversations")]

        # Once more: there is nothing by that id.
        assert client.remove_conversation(conversation) == "missing"
        assert client.remove_conversation("0" * 32) == "missing"


def test_a_404_is_not_taken_for_a_deletion_while_the_account_still_has_it():
    """The live service answers 404 for a conversation it does not have and
    for a deletion it could not make. Only asking afterwards tells them apart."""
    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key="test-key", timeout=10.0)
        conversation = _app_conversation(server, client)
        executor = OpenHandsExecutor(client)

        server.cloud.delete_status = 404
        with pytest.raises(CloudError, match="still reports"):
            client.remove_conversation(conversation)
        with pytest.raises(CloudError, match="still reports"):
            executor.remove(None, conversation)
        assert conversation in server.cloud.conversations

        server.cloud.delete_status = 503
        with pytest.raises(CloudUnavailable):
            client.remove_conversation(conversation)
        server.cloud.delete_status = 429
        with pytest.raises(CloudRateLimited):
            client.remove_conversation(conversation)

        server.cloud.delete_status = 0
        assert executor.remove(None, conversation) == "deleted"


def test_a_rejected_key_or_a_dead_service_has_deleted_nothing():
    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key="test-key", timeout=10.0)
        conversation = _app_conversation(server, client)
        server.cloud.rejected_keys.add("test-key")
        with pytest.raises(CloudAuthError):
            client.remove_conversation(conversation)
        server.cloud.rejected_keys.clear()
        assert conversation in server.cloud.conversations

    dead = CloudClient("http://127.0.0.1:9", api_key="test-key", timeout=2.0)
    with pytest.raises(CloudUnavailable):
        dead.remove_conversation("whatever")
    with pytest.raises(CloudAuthError):
        CloudClient("http://127.0.0.1:9", api_key="", timeout=2.0).remove_conversation("x")


@pytest.fixture
def direct():
    """The direct transport on the stand-in Cloud and one stand-in sandbox."""
    reset_schema()
    store = PostgresStore(DSN)
    with FakeCloudServer() as cloud, FakeServer() as sandbox:
        cloud.cloud.agent_server_url = sandbox.base_url
        cloud.cloud.sandbox_session_key = sandbox.runtime.expected_key
        client = CloudDirectClient(CloudClient(cloud.base_url, api_key="k", timeout=10.0),
                                   store, wait_interval=0.01, timeout=10.0)
        yield {"client": client, "cloud": cloud.cloud, "sandbox": sandbox.runtime,
               "store": store}
    store.close()


def _direct_conversation(direct, text: str, *, known_to_the_account: bool = True) -> tuple:
    """A conversation started on a sandbox. On the live service the sandbox
    tells the service, and the account lists the conversation from then on."""
    started = direct["client"].start_conversation(initial_message=text, title=f"lab:{text}")
    if known_to_the_account:
        direct["cloud"].register(started.app_conversation_id, started.sandbox_id)
    return started.app_conversation_id, started.sandbox_id


def test_the_direct_transport_deletes_in_the_account_on_the_sandbox_and_here(direct):
    client, cloud, sandbox, store = (direct[k] for k in ("client", "cloud", "sandbox", "store"))
    first, sandbox_id = _direct_conversation(direct, "one")
    second, same = _direct_conversation(direct, "two")
    assert same == sandbox_id, "both share the sandbox"
    assert store.conversation_route(first) is not None

    assert client.remove_conversation(first) == "deleted"
    assert cloud.deleted == [first], "the account's record: what a person sees"
    assert sandbox.deleted == [first], "the copy the shared sandbox kept"
    assert store.conversation_route(first) is None, "and the route, with its session key"
    # The other conversation, and the sandbox they share, are untouched.
    assert sandbox_id in cloud.sandboxes and cloud.removed_sandboxes == []
    assert second in sandbox.conversations and store.conversation_route(second) is not None
    assert client.get_conversation(second).id == second
    with pytest.raises(CloudNotFound):
        client.get_conversation(first)

    # The last conversation takes the sandbox with it, as on the live service -
    # and the next start does not reuse a sandbox that is gone.
    assert client.remove_conversation(second) == "deleted"
    assert cloud.removed_sandboxes == [sandbox_id]
    third, other = _direct_conversation(direct, "three")
    assert other != sandbox_id and third in sandbox.conversations

    assert client.remove_conversation(first) == "missing"


def test_a_conversation_the_account_never_listed_is_deleted_on_its_sandbox(direct):
    client, cloud, sandbox, store = (direct[k] for k in ("client", "cloud", "sandbox", "store"))
    conversation, _ = _direct_conversation(direct, "one", known_to_the_account=False)

    assert client.remove_conversation(conversation) == "deleted"
    assert cloud.deleted == [] and sandbox.deleted == [conversation]
    assert store.conversation_route(conversation) is None


def test_a_sandbox_that_no_longer_answers_does_not_keep_the_deletion_owed(direct):
    """Paused, or removed along with everything on it: there is nothing to ask
    of it. What the account lists is what decides."""
    client, cloud, sandbox, store = (direct[k] for k in ("client", "cloud", "sandbox", "store"))
    conversation, _ = _direct_conversation(direct, "one")
    sandbox.fail_times = 10

    assert client.remove_conversation(conversation) == "deleted"
    assert cloud.deleted == [conversation] and sandbox.deleted == []
    assert store.conversation_route(conversation) is None


def test_the_direct_transport_touches_nothing_when_the_account_cannot_be_asked(direct):
    client, cloud, sandbox, store = (direct[k] for k in ("client", "cloud", "sandbox", "store"))
    conversation, _ = _direct_conversation(direct, "one")

    cloud.delete_status = 503
    with pytest.raises(CloudUnavailable):
        client.remove_conversation(conversation)
    cloud.delete_status = 404          # "could not delete": it is still listed
    with pytest.raises(CloudError, match="still reports"):
        client.remove_conversation(conversation)
    assert sandbox.deleted == [] and store.conversation_route(conversation) is not None
    assert client.get_conversation(conversation).id == conversation

    cloud.delete_status = 0
    assert client.delete_conversation(conversation) is True
    assert client.delete_conversation(conversation) is False


# ---------------------------------------------------------- the worker's pass
def test_the_worker_deletes_through_the_real_client_on_its_own_pass(env, monkeypatch):
    """A run on the stand-in Cloud completes; a day later the worker's pass
    deletes its conversation there, and not again on the next pass."""
    from orchestration.github_client import GitHubClient
    from orchestration.lab_worker import LabWorker

    monkeypatch.setenv("OPENHANDS_API_KEY", "cloud-key-value")
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key="cloud-key-value", timeout=10.0)
        worker = LabWorker(env["store"], worker_id="w1", client=client,
                           github=GitHubClient(base_url="http://127.0.0.1:9", token="t"),
                           discover_router=False)
        worker.verifier = Verdicts()
        worker.executor.poll_interval = 0.01
        worker.lab.register_worker("w1")

        worker.tick_once()
        conversation = tasks_of(env, run_id)["build"]["conversation_id"]
        assert conversation in server.cloud.conversations
        server.cloud.conversations[conversation]["execution_status"] = "finished"
        server.cloud.add_finish(conversation, "done")
        for _ in range(5):
            worker.tick_once()
        assert run_of(env, run_id)["state"] == "completed"
        assert server.cloud.deleted == []

        ended(env, run_id, DAY + 60)
        # The worker does not look on every pass...
        assert "cleanup" not in worker.tick_once()
        assert server.cloud.deleted == []
        # ...but when it is time, it does.
        worker._cleanup_at = worker._cleanup_sweep_at = 0.0
        summary = worker.tick_once()
        assert summary["cleanup"] == {"queued": 1, "deleted": 1, "missing": 0, "abandoned": 0}
        assert server.cloud.deleted == [conversation]
        assert cleanups(env, run_id)[conversation]["state"] == "deleted"

        worker._cleanup_at = worker._cleanup_sweep_at = 0.0
        assert "cleanup" not in worker.tick_once()
        assert server.cloud.deleted == [conversation]


def test_the_worker_looks_for_deletions_now_and_then_not_on_every_pass(monkeypatch):
    calls: list[bool] = []

    class Scheduler:
        def remove_conversations(self, *, sweep):
            calls.append(sweep)
            return {"queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}

    clock = [1000.0]
    monkeypatch.setattr(lab_worker.time, "monotonic", lambda: clock[0])
    worker = lab_worker.LabWorker.__new__(lab_worker.LabWorker)
    worker._cleanup_at = worker._cleanup_sweep_at = 0.0

    worker._cleanup(Scheduler())                   # the first pass after a start
    worker._cleanup(Scheduler())                   # a few seconds later: nothing
    assert calls == [True]
    clock[0] += lab_worker.CLEANUP_INTERVAL_SECONDS
    worker._cleanup(Scheduler())                   # deletions that are owed, no new look
    assert calls == [True, False]
    clock[0] += lab_worker.CLEANUP_SWEEP_SECONDS
    worker._cleanup(Scheduler())
    assert calls == [True, False, True]


# ------------------------------------------- the older Sr/Jr workflows ("Код")
def workflow(env, state: str, *, sr: str, jr: tuple[str, ...] = (), age: float = DAY + 60,
             project: bool = True) -> str:
    """A workflow of the chat's "Код" mode as the server leaves it: an Sr
    conversation, one conversation per task, in *state* since *age* seconds."""
    store = env["store"]
    workflow_id = store.create_workflow("Починить вход", project_id=env["project"] if project
                                        else None)
    store.bind_sr(workflow_id, conversation_id=sr, status="running")
    for conversation in jr:
        task = store.submit_task("Исправить форму", workflow_id=workflow_id)
        store.bind_conversation(task.id, conversation, status="running")
    sql(env, "UPDATE workflows SET state=%s, updated_at = now() - %s * interval '1 second' "
             "WHERE id=%s", state, age, workflow_id)
    return workflow_id


def workflow_cleanups(env, workflow_id) -> dict[str, dict]:
    rows = sql(env, "SELECT * FROM nexus_conversation_cleanups WHERE workflow_id=%s", workflow_id)
    return {row["conversation_id"]: row for row in rows}


def test_a_finished_workflows_sr_and_task_conversations_are_deleted(env):
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts())
    for conversation in ("sr-done", "jr-done-1", "jr-done-2", "sr-stopped", "jr-stopped"):
        executor.states[conversation] = "finished"
    done = workflow(env, "completed", sr="sr-done", jr=("jr-done-1", "jr-done-2"))
    stopped = workflow(env, "cancelled", sr="sr-stopped", jr=("jr-stopped",), project=False)

    summary = sched.remove_conversations(limit=10)
    assert summary == {"queued": 5, "deleted": 5, "missing": 0, "pending": 0, "abandoned": 0}
    # With the server's own key (no stored connection): what the workflows ran on.
    assert sorted(executor.removed) == [(None, "jr-done-1"), (None, "jr-done-2"),
                                        (None, "jr-stopped"), (None, "sr-done"),
                                        (None, "sr-stopped")]
    rows = workflow_cleanups(env, done)
    assert sorted(rows) == ["jr-done-1", "jr-done-2", "sr-done"]
    assert {row["state"] for row in rows.values()} == {"deleted"}
    assert {str(row["project_id"]) for row in rows.values()} == {env["project"]}
    assert {row["run_id"] for row in rows.values()} == {None}
    assert {row["project_id"] for row in workflow_cleanups(env, stopped).values()} == {None}

    # Nothing is asked twice.
    assert sched.remove_conversations(limit=10) == {
        "queued": 0, "deleted": 0, "missing": 0, "pending": 0, "abandoned": 0}
    assert executor.remove_calls == 5


@pytest.mark.parametrize("state", ["queued", "running", "verifying", "blocked", "failed"])
def test_a_workflow_that_can_still_go_on_keeps_its_conversations(env, state):
    """``blocked`` and ``failed`` are resumed in the Sr conversation they have
    (``resume_workflow``), so only the two states nothing reopens count."""
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts())
    executor.states.update({"sr-open": "running", "jr-open": "running"})
    open_one = workflow(env, state, sr="sr-open", jr=("jr-open",), age=30 * DAY)

    assert sched.remove_conversations(limit=10)["queued"] == 0
    assert executor.removed == [] and workflow_cleanups(env, open_one) == {}

    # A person stops it: from then on the retention runs, and then they go.
    env["store"].cancel_workflow(open_one)
    assert sched.remove_conversations(limit=10)["queued"] == 0, "cancelled a moment ago"
    sql(env, "UPDATE workflows SET updated_at = now() - %s * interval '1 second' WHERE id=%s",
        DAY + 60, open_one)
    assert sched.remove_conversations(limit=10)["deleted"] == 2
    assert sorted(executor.gone()) == ["jr-open", "sr-open"]


def test_a_workflow_that_ended_less_than_a_day_ago_is_left_alone(env):
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts())
    executor.states["sr-fresh"] = "finished"
    fresh = workflow(env, "completed", sr="sr-fresh", age=DAY - 600)

    assert sched.remove_conversations(limit=10)["queued"] == 0
    summary = env["lab"].cleanup_summary(workflow_id=fresh, retention_seconds=DAY)
    due = sql(env, "SELECT updated_at + interval '1 day' AS due FROM workflows WHERE id=%s",
              fresh)[0]["due"]
    assert summary == {"enabled": True, "due_at": due, "requested": 0, "deleted": 0,
                       "missing": 0, "abandoned": 0, "last_error": None}

    sql(env, "UPDATE workflows SET updated_at = now() - %s * interval '1 second' WHERE id=%s",
        DAY + 1, fresh)
    assert sched.remove_conversations(limit=10)["deleted"] == 1
    summary = env["lab"].cleanup_summary(workflow_id=fresh, retention_seconds=DAY)
    assert summary["deleted"] == 1 and summary["requested"] == 0
    # A workflow that is at work has no date: its conversations are kept.
    live = workflow(env, "running", sr="sr-live")
    assert env["lab"].cleanup_summary(workflow_id=live, retention_seconds=DAY)["due_at"] is None
    assert env["lab"].cleanup_summary(workflow_id=fresh, retention_seconds=None)["enabled"] is False


def test_a_workflow_deletion_that_fails_is_owed_and_says_why(env):
    executor = Deletable()
    sched = scheduler(env, executor, Verdicts())
    executor.states["sr-held"] = "finished"
    held = workflow(env, "completed", sr="sr-held")
    executor.remove_errors.append(CloudAuthError("the key was rejected -> 401"))

    assert sched.remove_conversations(limit=10)["pending"] == 1
    row = workflow_cleanups(env, held)["sr-held"]
    assert row["state"] == "requested" and "401" in row["last_error"]
    summary = env["lab"].cleanup_summary(workflow_id=held, retention_seconds=DAY)
    assert summary["requested"] == 1 and "401" in summary["last_error"]

    retry_now(env)
    assert sched.remove_conversations(limit=10)["deleted"] == 1
    assert workflow_cleanups(env, held)["sr-held"]["state"] == "deleted"
