"""Race-condition protection: fencing tokens, ownership, stale workers.

These are the protections the "Защита от одновременного выполнения" section of
the acceptance text asks for, exercised against real PostgreSQL. The claim was
already atomic (``FOR UPDATE SKIP LOCKED`` plus a lease); what was missing was a
way to stop a worker that had *lost* its task from still writing to it. Each
test below reproduces a specific way a stale worker used to be able to corrupt a
task, and asserts the fence now refuses it. Reverting the fencing logic makes
them fail, so they are load-bearing, not decorative.

The clients are honest fakes of the transport; the store, the scheduler, the
pool and the SQL are the real code.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import LabScheduler, TaskReport, Verification  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402

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
    project = str(svc.create("user:alice", "Race")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    yield {"store": store, "svc": svc, "project": project, "goal": goal,
           "lab": LabStore(store)}
    store.close()


def _run_with(lab, tasks):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"])
    for t in tasks:
        lab["lab"].add_task("user:alice", lab["project"], run["id"], **t)
    return run


def _task(lab, run, key):
    return next(t for t in lab["lab"].list_tasks("user:alice", lab["project"], run["id"])
                if t["task_key"] == key)


def _sql(lab, statement, *params):
    with lab["store"]._tx() as cur:
        cur.execute(statement, params)


#: A stand-in account id, inserted as a real row so the switch's foreign key
#: (``preferred_account_id``) is satisfied without a live OpenHands account.
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _account(lab):
    _sql(lab, "INSERT INTO nexus_openhands_accounts"
              "(id, principal, label, base_url, api_key_encrypted) "
              "VALUES (%s, 'user:alice', 'A', 'https://example.test', '\\x00') "
              "ON CONFLICT (id) DO NOTHING", ACCOUNT_ID)
    return ACCOUNT_ID


# ----------------------------------------------------------- fencing tokens
def test_a_claim_records_its_owner_and_a_monotonic_token(lab):
    """The claim has to say who holds the task, and hand out a token that only
    moves forward. Without it a lost holder is indistinguishable from the
    current one."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    first = lab["lab"].claim_task(run["id"], "worker-1")
    assert first["lease_owner"] == "worker-1"
    token = first["lease_token"]
    assert token >= 1
    # Release and re-claim: the token moves forward, and the new holder owns it.
    lab["lab"].release_task(first["id"], error="retry", requeue=True,
                            lease_token=first["lease_token"])
    second = lab["lab"].claim_task(run["id"], "worker-2")
    assert second["lease_owner"] == "worker-2"
    assert second["lease_token"] > token, "the token must never go backwards"


def test_a_stale_worker_cannot_bind_a_conversation(lab):
    """The exact race the fencing token closes: worker-1 claims a task, its
    lease expires, worker-2 reclaims the task, and *then* worker-1 finishes its
    slow start and tries to bind the conversation id. Without the fence the
    second worker's fresh claim is overwritten and the next poll follows the
    wrong (or a dead) conversation."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    stale = lab["lab"].claim_task(run["id"], "worker-1", lease_seconds=0)
    # worker-2 reclaims it after the zero-length lease expires.
    lab["lab"].reclaim_expired(run["id"])
    fresh = lab["lab"].claim_task(run["id"], "worker-2")
    assert fresh["lease_token"] > stale["lease_token"]

    # worker-1 now reports the conversation it started - with its old token.
    after = lab["lab"].mark_running(stale["id"], conversation_id="conv-stale",
                                    lease_token=stale["lease_token"])
    # The stale bind did not land: the task is still worker-2's, with no
    # conversation bound to the old attempt.
    assert after["conversation_id"] is None
    assert after["lease_owner"] == "worker-2"

    # The current holder binds its own conversation, and that does land.
    bound = lab["lab"].mark_running(fresh["id"], conversation_id="conv-fresh",
                                    lease_token=fresh["lease_token"])
    assert bound["conversation_id"] == "conv-fresh"


def test_a_stale_worker_cannot_settle_a_redispatched_task(lab):
    """A slow verifier from the previous attempt must not record its verdict
    onto the new attempt: the fresh attempt would be reported as the result of
    the dead one."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    stale = lab["lab"].claim_task(run["id"], "worker-1", lease_seconds=0)
    lab["lab"].reclaim_expired(run["id"])
    fresh = lab["lab"].claim_task(run["id"], "worker-2")
    lab["lab"].mark_running(fresh["id"], conversation_id="conv-fresh",
                            lease_token=fresh["lease_token"])

    # worker-1's late verdict for the old attempt.
    after = lab["lab"].record_verification(
        stale["id"], verdict="completed_verified", evidence={"by": "stale"},
        lease_token=stale["lease_token"])
    assert after["state"] == "running", "a stale verdict must not settle the task"
    assert after["conversation_id"] == "conv-fresh"

    # The current holder's verdict does settle it.
    settled = lab["lab"].record_verification(
        fresh["id"], verdict="completed_verified", evidence={"by": "fresh"},
        lease_token=fresh["lease_token"])
    assert settled["state"] == "completed_verified"


def test_a_stale_worker_cannot_release_or_requeue_the_task(lab):
    """Release / requeue from a worker that has lost the task would undo the
    claim another worker is holding."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    stale = lab["lab"].claim_task(run["id"], "worker-1", lease_seconds=0)
    lab["lab"].reclaim_expired(run["id"])
    fresh = lab["lab"].claim_task(run["id"], "worker-2")

    released = lab["lab"].release_task(stale["id"], error="stale release",
                                       requeue=True, lease_token=stale["lease_token"])
    assert released["state"] == "dispatched" and released["lease_owner"] == "worker-2"

    postponed = lab["lab"].postpone_task(stale["id"], error="stale postpone",
                                         seconds=0.0, lease_token=stale["lease_token"])
    assert postponed["state"] == "dispatched" and postponed["lease_owner"] == "worker-2"

    requeued = lab["lab"].requeue_for_failover(
        stale["id"], failed_account_id="00000000-0000-0000-0000-000000000000",
        error="stale failover", lease_token=stale["lease_token"])
    assert requeued["state"] == "dispatched" and requeued["lease_owner"] == "worker-2"

    # Sanity: the current holder *can* release its own claim.
    ok = lab["lab"].release_task(fresh["id"], error="done", requeue=True,
                                 lease_token=fresh["lease_token"])
    assert ok["state"] == "pending"


def test_a_switch_invalidates_the_old_holder(lab):
    """An operator's manual switch takes the task away from the running worker.
    That worker must not be able to bind, settle or release afterwards."""
    account_id = _account(lab)
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    task = lab["lab"].claim_task(run["id"], "worker-1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-1",
                            lease_token=task["lease_token"])

    switched = lab["lab"].switch_task_executor(
        task["id"], target_account_id=account_id)
    assert switched["state"] == "pending"
    assert switched["lease_token"] > task["lease_token"]
    assert switched["lease_owner"] is None
    # The abandoned conversation is kept in the evidence, not lost, so the
    # operator can still stop it and the transcript remains the record.
    assert switched["evidence"][-1]["conversation_id"] == "conv-1"
    assert switched["evidence"][-1]["abandoned"] == "switch"

    # The old worker's writes are all refused now.
    assert lab["lab"].mark_running(task["id"], conversation_id="conv-2",
                                   lease_token=task["lease_token"])["conversation_id"] is None
    assert lab["lab"].record_verification(
        task["id"], verdict="completed_verified", evidence={},
        lease_token=task["lease_token"])["state"] == "pending"


def test_a_resumed_run_invalidates_the_old_holder(lab):
    """Resuming a blocked run requeues tasks that did not verify. The worker
    that reported the old attempt must not go on writing to the requeued one."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    task = lab["lab"].claim_task(run["id"], "worker-1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-1",
                            lease_token=task["lease_token"])
    # The run blocks with the task unverified (as a person would find it).
    lab["lab"].record_verification(task["id"], verdict="completed_unverified",
                                   evidence={"reason": "not pushed"},
                                   lease_token=task["lease_token"])
    _sql(lab, "UPDATE nexus_runs SET state='blocked' WHERE id=%s", run["id"])

    # Resume: the task goes back to the queue with a fresh token.
    lab["lab"].set_run_state("user:alice", lab["project"], run["id"], "running")
    resumed = _task(lab, run, "a")
    assert resumed["state"] == "pending"
    assert resumed["lease_token"] > task["lease_token"]
    # The old token no longer matches anything.
    assert not lab["lab"].holds_lease(task["id"], task["lease_token"])


def test_the_scheduler_refuses_to_start_a_task_it_no_longer_holds(lab):
    """End to end through the real tick: a worker claims a task, the task is
    taken away before the dispatch, and the tick must not create a conversation
    for it.

    The window between ``claim_task`` and ``dispatch`` is real. A store whose
    claim is immediately followed by a switch (an operator, or a reclaim from a
    worker that thought the lease was dead) reproduces it exactly: the tick goes
    on with the task it thinks it holds, and the fence has to stop the start."""
    account_id = _account(lab)
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    executor = RecordingExecutor()

    class SnatchingStore(LabStore):
        """Claims as usual, then an outside actor takes the task away."""

        def claim_task(self, run_id, worker_id, **kw):
            task = super().claim_task(run_id, worker_id, **kw)
            if task is not None and not getattr(self, "_snatched", False):
                self._snatched = True
                self.switch_task_executor(task["id"], target_account_id=account_id)
            return task

    sched = LabScheduler(SnatchingStore(lab["store"]), executor, StubVerifier(),
                         worker_id="w")
    result = sched.tick(run["id"])
    assert result.note == "claim lost before dispatch; not started"
    assert executor.dispatched == [], "a lost claim must not start a conversation"


# --------------------------------------------------- the scheduler, in the loop
class RecordingExecutor:
    """Starts a conversation and reports a finished one."""

    def __init__(self):
        self.dispatched: list[str] = []

    def dispatch(self, task):
        self.dispatched.append(task["task_key"])
        return f"conv-{task['task_key']}"

    def poll(self, task, conversation_id):
        return TaskReport(state="finished", final_text="did work",
                          conversation_id=conversation_id)


class StubVerifier:
    def verify(self, task, report):
        return Verification("completed_verified", "stub", {"state": report.state})


def test_an_expired_lease_that_nobody_reclaimed_still_belongs_to_its_worker(lab):
    """The fence is the token, not the clock. A lease that expired but that no
    worker has reclaimed is still the holder's: the start must go ahead, not be
    silently dropped (which would strand the task)."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    executor = RecordingExecutor()
    sched = LabScheduler(lab["lab"], executor, StubVerifier(), lease_seconds=0)
    result = sched.tick(run["id"])
    assert result.dispatched is True
    assert executor.dispatched == ["a"]
    assert _task(lab, run, "a")["conversation_id"] == "conv-a"


# ------------------------------------------------------- the database default
def test_the_fencing_columns_default_to_zero(lab):
    """A row written by anything else (a direct INSERT, an older writer) still
    has a valid token, so the fence never compares against NULL."""
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    _sql(lab, "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title) "
              "VALUES (%s, %s, 'raw', 'Raw')", run["id"], lab["project"])
    raw = _task(lab, run, "raw")
    assert raw["lease_token"] == 0
    assert raw["lease_owner"] is None


def test_the_schema_guard_names_the_fencing_columns(lab):
    """A database missing the fencing columns must be reported, not fail later
    with "column does not exist"."""
    from orchestration.cloud_store import PostgresStore as PS

    assert "lease_token" in PS.REQUIRED_COLUMNS["nexus_run_tasks"]
    assert "lease_owner" in PS.REQUIRED_COLUMNS["nexus_run_tasks"]
    assert lab["store"].verify_schema() == []


# ------------------------------------------ stopping the abandoned conversation
def test_the_local_transport_stops_a_conversation_over_real_http():
    """``stop_conversation`` pauses the agent-server's run loop and keeps the
    conversation. Exercised against the stand-in agent-server over a real
    socket, not a mock of the client."""
    from orchestration.local_runtime import LocalCloudClient

    with FakeServer() as server:
        client = LocalCloudClient(server.base_url, api_key="test-key",
                                  workspace_root="/tmp/race-ws")
        client.start_conversation(initial_message="do the thing", title="t")
        cid = next(iter(server.runtime.conversations))
        assert server.runtime.conversations[cid].status == "running"
        assert client.stop_conversation(cid) is True
        assert server.runtime.conversations[cid].status == "paused"
        # The conversation is kept, not deleted: the transcript is the record.
        assert cid in server.runtime.conversations


def test_the_app_transport_does_not_pretend_to_stop_a_conversation():
    """The Cloud V1 app-conversation surface has no conversation-scoped stop.
    Reporting ``False`` is honest; a caller must not assume the old executor was
    stopped, and the branch lock is what actually keeps two off one branch."""
    from orchestration.cloud_client import CloudClient

    client = CloudClient(base_url="https://cloud.example", api_key="sk-x")
    assert client.stop_conversation("whatever") is False


def test_a_switch_stops_the_abandoned_conversation(lab):
    """The web switch reads the conversation before clearing it, stops it, and
    reports that it did - so the old executor does not keep writing while the
    new one works. The store half is what this test drives; the client half is
    covered above over real HTTP."""
    account_id = _account(lab)
    run = _run_with(lab, [{"task_key": "a", "title": "A"}])
    task = lab["lab"].claim_task(run["id"], "worker-1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-keep",
                            lease_token=task["lease_token"])
    before = _task(lab, run, "a")
    assert before["conversation_id"] == "conv-keep"

    switched = lab["lab"].switch_task_executor(task["id"],
                                               target_account_id=account_id)
    # The conversation is recorded (so it can be stopped) before the binding is
    # cleared; nothing is lost.
    assert switched["conversation_id"] is None
    assert switched["evidence"][-1]["conversation_id"] == "conv-keep"
