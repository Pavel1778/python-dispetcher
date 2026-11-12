"""No two writers on one branch after an executor switch or a failover.

``0028_task_lease_fencing.sql`` stopped a worker that had *lost* a task from
recording work on it. This proves the other half: the old *conversation* cannot
go on editing the checkout beside the new one.

A switch (an operator's, or a failover) starts a new conversation on another
account while the old one may still be running in its sandbox. Before this, the
old conversation was unbound from the task and forgotten - so the next claim
started a second writer on the same branch at once. Now the conversation is
recorded in the task's ``abandoned`` list, the branch is held
(``_BRANCH_FREE``) until the worker stops it for real, and the stop is retried
while it fails. A transport that cannot stop a conversation at all (the Cloud V1
app-conversation surface) is honest about it, so the branch is not held forever.

Real PostgreSQL; only the executor/verifier are small in-process fakes.
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

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
REPO = "acme/repo"
BRANCH = "main"


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
    project = str(svc.create("user:alice", "Branch lock")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship it")[0]["id"])
    # The dispatch gate re-checks the allowlist and the project link, so the
    # repository has to be allowed and linked or a real tick refuses to start.
    store.add_repository(REPO, default_branch=BRANCH, allowed_branches=[BRANCH])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link("user:alice", project, REPO)
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_openhands_accounts(id, principal, label, base_url, "
                    "api_key_encrypted) VALUES "
                    "('22222222-2222-2222-2222-222222222222','user:alice','B',"
                    "'https://example.test','\\x00')")
    yield {"store": store, "svc": svc, "project": project, "goal": goal,
           "lab": LabStore(store)}
    store.close()


def _run(lab, **task):
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                               repository=REPO, branch=BRANCH)
    body = {"task_key": "a", "title": "A", "repository": REPO, "branch": BRANCH}
    body.update(task)
    lab["lab"].add_task("user:alice", lab["project"], run["id"], **body)
    return run


def _only_task(lab, run):
    return lab["lab"].list_tasks("user:alice", lab["project"], run["id"])[0]


ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


# ------------------------------------------------------------ the store holds
def test_a_switch_records_the_abandoned_conversation_and_holds_the_branch(lab):
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-old",
                            lease_token=task["lease_token"])
    switched = lab["lab"].switch_task_executor(task["id"], target_account_id=ACCOUNT_B)
    assert switched["conversation_id"] is None
    assert [e["conversation_id"] for e in switched["abandoned"]] == ["conv-old"]
    # The evidence record the operator reads is kept too.
    assert switched["evidence"][-1]["conversation_id"] == "conv-old"
    # The branch is held: a second writer cannot start on it yet.
    assert lab["lab"].claim_task(run["id"], "w2") is None
    # Once the conversation is really stopped, the branch is free.
    lab["lab"].mark_abandoned_stopped(task["id"], "conv-old")
    assert lab["lab"].claim_task(run["id"], "w2") is not None


def test_an_abandoned_writer_holds_the_branch_across_tasks(lab):
    """The checkout is one workspace, so a *different* task on the same branch is
    held too, not only the task that was switched."""
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                               repository=REPO, branch=BRANCH)
    first = lab["lab"].add_task("user:alice", lab["project"], run["id"],
                                task_key="a", title="A", repository=REPO, branch=BRANCH)
    lab["lab"].add_task("user:alice", lab["project"], run["id"],
                        task_key="b", title="B", repository=REPO, branch=BRANCH)
    claimed = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(claimed["id"], conversation_id="conv-a",
                            lease_token=claimed["lease_token"])
    lab["lab"].switch_task_executor(claimed["id"], target_account_id=ACCOUNT_B)
    assert lab["lab"].claim_task(run["id"], "w2") is None, "the branch is held"
    lab["lab"].mark_abandoned_stopped(first["id"], "conv-a")
    assert lab["lab"].claim_task(run["id"], "w2") is not None


def test_a_failover_records_the_abandoned_conversation_too(lab):
    """A failover is a switch: the conversation it leaves behind is recorded, so
    the branch is held until it is stopped."""
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-gone",
                            account_id=ACCOUNT_B, lease_token=task["lease_token"])
    requeued = lab["lab"].requeue_for_failover(
        task["id"], failed_account_id=ACCOUNT_B, error="executor gone",
        lease_token=task["lease_token"])
    assert requeued["conversation_id"] is None
    assert [e["conversation_id"] for e in requeued["abandoned"]] == ["conv-gone"]
    assert lab["lab"].claim_task(run["id"], "w2") is None


def test_pending_stops_lists_what_is_left_and_is_idempotent(lab):
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-old",
                            account_id=ACCOUNT_B, lease_token=task["lease_token"])
    lab["lab"].switch_task_executor(task["id"], target_account_id=ACCOUNT_B)
    stops = lab["lab"].pending_stops(run["id"])
    assert len(stops) == 1
    assert stops[0]["conversation_id"] == "conv-old"
    assert stops[0]["account_id"] == ACCOUNT_B
    assert stops[0]["task_key"] == "a"
    lab["lab"].mark_abandoned_stopped(stops[0]["task_id"], "conv-old")
    lab["lab"].mark_abandoned_stopped(stops[0]["task_id"], "conv-old")  # idempotent
    assert lab["lab"].pending_stops(run["id"]) == []


def test_the_schema_guard_names_the_abandoned_column(lab):
    assert "abandoned" in PostgresStore.REQUIRED_COLUMNS["nexus_run_tasks"]
    assert lab["store"].verify_schema() == []


def test_a_task_that_predates_the_migration_has_nothing_abandoned(lab):
    """An existing row defaults to an empty list: nothing to stop, no branch
    held, exactly the previous behaviour."""
    run = _run(lab)
    with lab["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, "
                    "repository, branch) VALUES (%s, %s, 'raw', 'Raw', %s, %s)",
                    (run["id"], lab["project"], REPO, BRANCH))
    raw = next(t for t in lab["lab"].list_tasks("user:alice", lab["project"], run["id"])
               if t["task_key"] == "raw")
    assert raw["abandoned"] == []


# --------------------------------------------------------- the scheduler stops
class StopRecordingExecutor:
    """Starts conversations and stops them; ``stop_fails`` makes the stop fail
    (the pause could not be performed), and ``can_stop=False`` is a transport
    with no conversation-scoped stop at all."""

    def __init__(self, *, stop_fails: bool = False, can_stop: bool = True):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.stop_fails = stop_fails
        self.can_stop = can_stop

    def dispatch(self, task):
        cid = f"conv-{task['task_key']}-{len(self.started)}"
        self.started.append(cid)
        return cid

    def poll(self, task, conversation_id):
        return TaskReport(state="running", conversation_id=conversation_id)

    def stop(self, account_id, conversation_id, project_id=None):
        if not self.can_stop:
            # The transport has no conversation-scoped stop: nothing to wait for.
            return True
        if self.stop_fails:
            return False
        self.stopped.append(conversation_id)
        return True


class VerifyingAll:
    def verify(self, task, report):
        return Verification("completed_verified", "ok", {"state": report.state})


def test_the_scheduler_stops_the_abandoned_conversation_before_dispatching(lab):
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-old",
                            lease_token=task["lease_token"])
    lab["lab"].switch_task_executor(task["id"], target_account_id=ACCOUNT_B)
    executor = StopRecordingExecutor()
    LabScheduler(lab["lab"], executor, VerifyingAll(), worker_id="w").tick(run["id"])
    assert executor.stopped == ["conv-old"], "the old executor must be stopped"
    assert lab["lab"].pending_stops(run["id"]) == []
    assert executor.started, "the branch is free once the old writer is stopped"


def test_the_scheduler_holds_the_branch_while_the_stop_fails(lab):
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-old",
                            lease_token=task["lease_token"])
    lab["lab"].switch_task_executor(task["id"], target_account_id=ACCOUNT_B)
    executor = StopRecordingExecutor(stop_fails=True)
    LabScheduler(lab["lab"], executor, VerifyingAll(), worker_id="w").tick(run["id"])
    assert executor.started == [], "no second writer while the old one cannot be stopped"
    assert [e["conversation_id"] for e in lab["lab"].pending_stops(run["id"])] == ["conv-old"]


def test_a_transport_that_cannot_stop_does_not_hold_the_branch_forever(lab):
    """The Cloud V1 app-conversation surface cannot stop a conversation. Waiting
    forever for a stop that can never happen would strand the task, so such a
    transport releases the branch and the run goes on (the switch's hand-off is
    still recorded)."""
    run = _run(lab)
    task = lab["lab"].claim_task(run["id"], "w1")
    lab["lab"].mark_running(task["id"], conversation_id="conv-old",
                            lease_token=task["lease_token"])
    lab["lab"].switch_task_executor(task["id"], target_account_id=ACCOUNT_B)
    executor = StopRecordingExecutor(can_stop=False)
    LabScheduler(lab["lab"], executor, VerifyingAll(), worker_id="w").tick(run["id"])
    assert lab["lab"].pending_stops(run["id"]) == []
    assert executor.started, "the task must go on, not be stranded"
