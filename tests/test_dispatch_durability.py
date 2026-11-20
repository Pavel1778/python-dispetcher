"""The crash window *between* the accepted start and its record.

Phase 2 asks for every window to be injected, not just the ones already
covered. ``tests/test_start_handover.py`` covers a worker replaced **after**
``LabStore.record_start`` put the start task on the row. What it does not cover
is the window the report named as the remaining gap: the service has accepted
the start (it exists, and it is at work), and the process dies **before**
anything about it reached the database.

Why that window matters: a Cloud start has no idempotency key (see
``CloudClient.start_conversation``), so a blind retry can create a second
conversation for the same task. The correct outcome is not a retry - it is a
*reconciliation*: the conversation is findable by the tags every start carries
(``nexusrun``/``nexustask``/``nexusattempt``), so whoever runs next adopts it
instead of starting beside it.

These cases drive the real ``OpenHandsExecutor`` and the real
``LabScheduler``/``LabStore`` over real PostgreSQL, with a client that stands in
for the service. A "crash" is raised as ``BaseException`` - a ``SIGKILL`` and a
``KeyboardInterrupt`` are not ``Exception``, and code that only guards
``Exception`` must be seen to leak.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_lab import OpenHandsExecutor  # noqa: E402
from tests.test_durable_runs import (  # noqa: E402,F401
    env,
    sql,
    start,
    tasks_of,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_SUPABASE_DB_URL"),
    reason="set TEST_SUPABASE_DB_URL to run lab tests")

HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"
DONE = f"Готово. Коммит {HEAD}."


class _StartTask:
    """What ``CloudClient.start_conversation`` returns: a start task to poll."""

    def __init__(self, id: str, status: str = "STARTING_CONVERSATION",
                 app_conversation_id: str | None = None):
        self.id = id
        self.status = status
        self.app_conversation_id = app_conversation_id
        self.sandbox_id = None
        self.agent_server_url = None
        self.detail = None
        self.files = ()

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    @property
    def failed(self) -> bool:
        return self.status == "ERROR"


class _CrashingClient:
    """A service that accepts a start and then the process dies.

    ``accept`` is how many starts the service will take before the crash. The
    crash is raised from ``get_start_task`` - the first thing the executor does
    after the service has answered - so the start exists on the service and
    nowhere in our database.
    """

    supports_git_secrets = False
    supports_conversation_stop = False

    def __init__(self, *, crash_after_accepts: int = 1):
        self.accepted: list[dict] = []
        self.crash_after_accepts = crash_after_accepts
        self.search_calls: list[dict] = []
        self.crashed = False
        #: Conversations the *service* holds, as a reconciliation search sees them.
        self.conversations: list[dict] = []

    def start_conversation(self, **kwargs):
        self.accepted.append(kwargs)
        index = len(self.accepted)
        conv = f"conv-{index}"
        self.conversations.append({
            "id": conv, "conversation_id": conv,
            "tags": dict(kwargs.get("tags") or {}), "status": "RUNNING"})
        return _StartTask(f"start-{index}")

    def get_start_task(self, start_task_id):
        if not self.crashed and len(self.accepted) >= self.crash_after_accepts:
            self.crashed = True
            # A SIGKILL/KeyboardInterrupt is a BaseException, not an Exception:
            # this is the crash the test injects.
            raise KeyboardInterrupt("process died before the start was recorded")
        return None

    def search_conversations(self, *, limit=50, tag=None):
        self.search_calls.append({"limit": limit, "tag": tag})
        found = []
        if tag is not None:
            key, value = tag
            found = [c for c in self.conversations if c["tags"].get(key) == value]
        return [type("C", (), {"id": c["id"], "conversation_id": c["id"],
                               "tags": c["tags"], "status": c["status"],
                               "execution_status": c["status"]})()
                for c in found], None

    def stop_conversation(self, conversation_id):
        return True


@contextlib.contextmanager
def _crash():
    """A process death is a BaseException; catch it so the test continues."""
    try:
        yield
    except KeyboardInterrupt:
        return
    raise AssertionError("expected the injected crash")


def _executor(client, store):
    executor = OpenHandsExecutor(client, specs=None, router=None)
    executor.on_start = None          # the scheduler sets it; here it is explicit
    return executor


# ------------------------------------------------------- the window itself
def test_a_crash_after_the_service_accepts_the_start_leaves_no_record(env):  # noqa: F811
    """The defect this file exists for: nothing about the start is on the row.

    This is the *observation*, not the fix: it pins the fact that the window
    exists, so the fix below is measured against it.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)
    assert claim is not None

    client = _CrashingClient()
    executor = _executor(client, env["store"])
    with _crash():
        executor.dispatch(claim)

    # The service has the start; the database does not.
    assert len(client.accepted) == 1
    assert lab.conversation_of(claim["id"]) is None
    assert lab.pending_starts(run_id) == []


def test_the_orphan_is_findable_by_the_tags_every_start_carries(env):  # noqa: F811
    """The reconciliation input exists: a start is tagged with run+task+attempt.

    This is what makes "adopt instead of retry" possible at all; without it the
    only options would be a blind retry (a duplicate) or a dead task.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)

    client = _CrashingClient()
    executor = _executor(client, env["store"])
    with _crash():
        executor.dispatch(claim)

    tags = client.accepted[0]["tags"]
    assert tags["nexusrun"] == str(run_id)[:40]
    assert tags["nexustask"] == "build"
    assert tags["nexusattempt"] == "c0a1"
    # And the service can be asked for exactly that tag.
    found, _ = client.search_conversations(tag=("nexusrun", str(run_id)[:40]))
    assert [c.id for c in found] == ["conv-1"]


# --------------------------------------------- recovery must adopt, not retry
def test_recovery_adopts_the_orphan_instead_of_starting_a_second_conversation(env):  # noqa: F811
    """After the crash, the next pass must adopt, never start beside it.

    The claim runs out; the task is taken back with ``reclaimed``; the next
    dispatch looks for the conversation the lost worker started and adopts it.
    A second ``start_conversation`` is the failure this asserts against.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)

    client = _CrashingClient()
    executor = _executor(client, env["store"])
    try:
        executor.dispatch(claim)
    except KeyboardInterrupt:
        pass          # the process died; a new one takes over below
    assert len(client.accepted) == 1

    # The worker is gone; its lease runs out and the task comes back.
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", claim["id"])
    [taken_back] = lab.reclaim_expired(run_id)
    assert taken_back["state"] == "pending"
    assert taken_back["evidence"][-1]["reclaimed"] is True

    # A new worker claims it and dispatches. The service is now answering.
    again = lab.claim_task(run_id, "second", lease_seconds=900)
    assert again["attempt"] == 2
    client.crashed = True          # the service no longer dies; only the DB state differs
    executor2 = _executor(client, env["store"])
    adopted = executor2.dispatch(again)

    assert str(adopted) == "conv-1", adopted
    assert getattr(adopted, "kind", None) == "adopted"
    # The decisive assertion: no second external start.
    assert len(client.accepted) == 1, client.accepted


def test_recovery_starts_fresh_when_no_orphan_exists(env):  # noqa: F811
    """A crash *before* the service was asked leaves nothing to adopt.

    The complementary half: the reconciliation must not invent a conversation.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)

    class _NeverAccepted(_CrashingClient):
        """The service is never reached: the process dies before the request
        leaves. Only the first call dies - the recovery below is a new process
        talking to a working service."""

        def __init__(self):
            super().__init__()
            self.dead = True

        def start_conversation(self, **kwargs):
            if self.dead:
                self.dead = False
                raise KeyboardInterrupt("process died before the request left")
            # The recovery is a *new* process against a working service: the
            # crash-on-get_start_task of the parent must not fire again.
            self.crashed = True
            return super().start_conversation(**kwargs)

        def get_start_task(self, start_task_id):
            # After the crash the service answers normally and the start is
            # already READY, so the executor returns its conversation.
            return _StartTask(start_task_id, status="READY",
                              app_conversation_id=f"conv-{len(self.accepted)}")

    client = _NeverAccepted()
    executor = _executor(client, env["store"])
    with _crash():
        executor.dispatch(claim)
    assert client.accepted == []

    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", claim["id"])
    lab.reclaim_expired(run_id)
    again = lab.claim_task(run_id, "second", lease_seconds=900)
    executor2 = _executor(client, env["store"])
    started = executor2.dispatch(again)
    assert str(started) == "conv-1"
    assert len(client.accepted) == 1


# ------------------------------------------------- a stale holder cannot bind
def test_a_stale_lease_cannot_bind_the_conversation_it_started(env):  # noqa: F811
    """The claim was taken away while the start was in flight: no bind.

    ``LabStore.holds_lease`` is the fence; a worker that lost its claim must not
    write the conversation it happens to hold.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)

    # Take the claim away exactly as a reclaim would.
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", claim["id"])
    [taken_back] = lab.reclaim_expired(run_id)
    assert taken_back["lease_token"] > claim["lease_token"]

    assert lab.holds_lease(claim["id"], claim["lease_token"]) is False
    # And a fenced write with the stale token changes nothing.
    bound = lab.mark_running(claim["id"], conversation_id="conv-stale",
                             lease_token=claim["lease_token"])
    assert lab.conversation_of(claim["id"]) is None, bound


# ------------------------------------- binding and transition are one statement
def test_binding_and_marking_running_happen_together(env):  # noqa: F811
    """There is no "bound but not running" state to crash inside.

    ``mark_running`` sets ``conversation_id`` and moves ``dispatched`` ->
    ``running`` in a single UPDATE. A crash between the two is therefore not a
    window this system has, which is why the window list can skip it - and this
    case pins that it stays one statement.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)
    assert claim["state"] == "dispatched" and claim["conversation_id"] is None

    row = lab.mark_running(claim["id"], conversation_id="conv-1",
                           lease_token=claim["lease_token"])
    assert row["state"] == "running"
    assert row["conversation_id"] == "conv-1"
    # Never an intermediate row.
    stored = tasks_of(env, run_id)["build"]
    assert not (stored["conversation_id"] and stored["state"] == "dispatched")


def test_binding_the_same_conversation_twice_is_a_no_op(env):  # noqa: F811
    """Two processes that saw one start both bind it; the second changes nothing."""
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)
    first = lab.mark_running(claim["id"], conversation_id="conv-1",
                             lease_token=claim["lease_token"])
    second = lab.mark_running(claim["id"], conversation_id="conv-1",
                              lease_token=claim["lease_token"])
    assert first["id"] == second["id"]
    assert second["state"] == "running"
    # One execution row, not two: the history is not written twice.
    count = sql(env, "SELECT count(*) AS n FROM nexus_task_executions WHERE task_id=%s",
                claim["id"])[0]["n"]
    assert count == 1, count


# --------------------------------------------- concurrent recovery, one winner
def test_two_workers_recovering_at_once_start_exactly_one_conversation(env):  # noqa: F811
    """Simultaneous recovery must not produce two external starts.

    Both workers see a reclaimed task and both call ``dispatch``. The service
    is asked **once**: the first ``start_conversation`` wins, and the second
    worker adopts the conversation the first created rather than starting
    beside it.
    """
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)

    client = _CrashingClient()
    executor = _executor(client, env["store"])
    with _crash():
        executor.dispatch(claim)
    assert len(client.accepted) == 1

    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", claim["id"])
    lab.reclaim_expired(run_id)
    again = lab.claim_task(run_id, "second", lease_seconds=900)

    # Worker B runs while worker A's conversation exists on the service.
    client.crashed = True
    b = _executor(client, env["store"])
    adopted = b.dispatch(again)
    assert getattr(adopted, "kind", None) == "adopted"
    assert len(client.accepted) == 1, "recovery started a second conversation"


# ------------------------------------------------------- the full DAG, one pass
def test_a_three_task_dag_runs_in_order_with_one_conversation_each(env):  # noqa: F811
    """claim -> run -> bind -> collect -> complete -> dependents, end to end.

    The worker lifecycle in one case: a dependent task is not claimable until
    its dependency is ``completed_verified``, and each task gets exactly one
    conversation.
    """
    from orchestration.nexus_lab import LabScheduler, Verification
    from tests.test_durable_runs import Conversations

    lab = env["lab"]
    run_id = start(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B", "dependencies": ["a"]},
        {"task_key": "c", "title": "C", "dependencies": ["b"]},
    ])

    class _Verifier:
        def verify(self, task, report):
            return Verification("completed_verified", "ok",
                                {"remote_sha": HEAD, "changed": True})

        def head(self, task):
            return "0" * 40

    executor = Conversations()
    scheduler = LabScheduler(lab, executor, _Verifier(), principal="machine:default")

    # One task at a time: the next is not claimable until the previous verified.
    for key in ("a", "b", "c"):
        scheduler.tick(run_id)
        assert len(executor.of(key)) == 1, (key, executor.started)
        executor.finish(executor.of(key)[-1], f"Готово. Коммит {HEAD}.")
    scheduler.run_until_done(run_id)

    states = {key: row["state"] for key, row in tasks_of(env, run_id).items()}
    assert states == {"a": "completed_verified", "b": "completed_verified",
                      "c": "completed_verified"}, states
    # One conversation per task, no duplicates.
    started = [key for key, _conv in executor.started]
    assert sorted(started) == ["a", "b", "c"], started
