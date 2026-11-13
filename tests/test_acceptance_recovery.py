"""Acceptance section E — Recovery.

The task text asks for: a forced stop of the worker, a lost connection to
OpenHands, an expired lease, worker recovery, the task being picked up again,
and a check that nothing was duplicated.

Two layers are exercised:

* the **process** layer (a real ``lab_worker`` child process, SIGKILLed and
  restarted) — the deployment's own recovery;
* the **store** layer (real PostgreSQL): an expired lease is reclaimed, a task
  whose conversation exists is *not* requeued (that would be a duplicate), and a
  reclaimed task keeps a monotonic fencing token so a stale worker cannot write
  to it.

The invariant throughout: recovery never starts a second conversation for work
that is already under way.
"""
from __future__ import annotations

import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.acceptance_kit import reset_schema  # noqa: E402
from tests.test_lab_inline import (  # noqa: E402
    _finish,
    _start_run,
    _starts,
    _task_conversation,
    _until,
)
from tests.test_lab_worker_process import Worker  # noqa: E402
from tests.test_studio_api import DSN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run lab tests")

HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


def _detail(studio, project, run):
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    return detail


# --------------------------------------------------- process-level recovery
def test_a_sigkilled_worker_is_recovered_by_a_new_process(monkeypatch):
    """Force-stop the worker mid-run; a fresh process continues, no duplicate.

    The task's conversation was already started, so the work is in the Cloud.
    A new worker follows it rather than starting a second conversation - the
    "проверка отсутствия дублей" the task text asks for.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)
        first_worker = Worker(studio, worker_id="acc-kill-1")
        first = _until(lambda: _task_conversation(studio, "audit"),
                       what="the first worker to dispatch")
        # "Already started" means recorded as well: the conversation exists in
        # the Cloud an instant before the worker has bound it to the task, and a
        # kill inside that instant is another case - the task is then taken back
        # when its claim runs out and the conversation is adopted
        # (tests/test_durable_runs.py), which takes longer than this test waits.
        _until(lambda: next((t for t in _detail(studio, project, run)["tasks"]
                             if t["task_key"] == "audit"
                             and t["conversation_id"] == first), None),
               what="the first worker to record the conversation it started")
        first_worker.stop(sig=signal.SIGKILL)          # a crash, not a shutdown

        second_worker = Worker(studio, worker_id="acc-kill-2")
        try:
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the second worker to continue")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed"
            assert len(_starts(studio)) == 2, "recovery started a task twice"
        finally:
            second_worker.stop()


def test_a_restart_after_graceful_stop_resumes_the_run(monkeypatch):
    """A worker that is stopped cleanly is replaced and the run finishes."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)
        first_worker = Worker(studio, worker_id="acc-grace-1")
        first = _until(lambda: _task_conversation(studio, "audit"),
                       what="the first worker to dispatch")
        first_worker.stop()                            # graceful SIGTERM

        second_worker = Worker(studio, worker_id="acc-grace-2")
        try:
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the replacement worker to continue")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed"
            assert len(_starts(studio)) == 2
        finally:
            second_worker.stop()


# ---------------------------------------------------- store-level recovery
@pytest.fixture
def lab(monkeypatch):
    reset_schema(DSN)
    store = PostgresStore(DSN)
    projects = ProjectService(store)
    project = str(projects.create("user:alice", "Recovery")["id"])
    goal = str(projects.create_goal("user:alice", project, "Recover")[0]["id"])
    yield {"store": store, "lab": LabStore(store), "project": project, "goal": goal}
    store.close()


def _one_task_run(lab):
    return lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                                plan=[{"id": "T1", "title": "work", "description": "",
                                       "acceptance": [], "dependencies": []}])


def _seed_account(lab, label: str) -> str:
    """Insert a minimal OpenHands account row so a task can be bound to one.

    The API key is a Fernet-shaped placeholder: these tests never decrypt it,
    they only need a real row for the foreign key.
    """
    with lab["store"]._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_openhands_accounts(principal, label, api_key_encrypted) "
            "VALUES (%s,%s,%s) RETURNING id",
            ("user:alice", label, b"placeholder-ciphertext"))
        return str(cur.fetchone()["id"])


def test_an_expired_lease_with_no_conversation_is_reclaimed(lab):
    """A worker died between claiming and starting: the task comes back."""
    run = _one_task_run(lab)
    task = lab["lab"].claim_task(str(run["id"]), "w", lease_seconds=-1)
    assert task["state"] == "dispatched"
    reclaimed = lab["lab"].reclaim_expired(str(run["id"]))
    assert [t["task_key"] for t in reclaimed] == ["T1"]
    again = lab["lab"].claim_task(str(run["id"]), "w2")
    assert again is not None and again["task_key"] == "T1"
    # Reclaiming bumped the fencing token, so the dead worker's writes no-op.
    assert again["lease_token"] > task["lease_token"]
    assert not lab["lab"].holds_lease(task["id"], task["lease_token"])


def test_an_expired_lease_with_a_conversation_is_not_requeued(lab):
    """The work is under way in the Cloud: requeueing would duplicate it."""
    run = _one_task_run(lab)
    task = lab["lab"].claim_task(str(run["id"]), "w", lease_seconds=-1)
    lab["lab"].mark_running(task["id"], conversation_id="conv-1")
    # The lease expired, but a conversation exists, so reclaim leaves it alone.
    assert lab["lab"].reclaim_expired(str(run["id"])) == []
    refreshed = lab["lab"].get_task_internal(task["id"])
    assert refreshed["state"] == "running"
    assert refreshed["conversation_id"] == "conv-1"
    # And no second task can be claimed for the same work.
    assert lab["lab"].claim_task(str(run["id"]), "w2") is None


def test_a_task_that_exhausted_its_attempts_is_failed_not_requeued(lab):
    """Recovery must terminate: a task with no attempts left fails, it does not
    spin."""
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                               max_attempts_per_task=1,
                               plan=[{"id": "T1", "title": "work", "description": "",
                                      "acceptance": [], "dependencies": []}])
    lab["lab"].claim_task(str(run["id"]), "w", lease_seconds=-1)
    reclaimed = lab["lab"].reclaim_expired(str(run["id"]))
    assert [t["state"] for t in reclaimed] == ["failed"]
    assert lab["lab"].claim_task(str(run["id"]), "w2") is None


def test_a_lost_connection_requeues_the_task_for_failover(lab):
    """A conversation that vanished (the executor is gone) goes back for another
    account, with the failed account excluded, and the attempt is spent."""
    account_id = _seed_account(lab, "acct-a")
    run = _one_task_run(lab)
    task = lab["lab"].claim_task(str(run["id"]), "w")
    lab["lab"].mark_running(task["id"], conversation_id="conv-gone",
                            account_id=account_id)
    requeued = lab["lab"].requeue_for_failover(
        task["id"], failed_account_id=account_id,
        error="conversation missing; executor may be gone", requeue=True)
    assert requeued["state"] == "pending"
    assert account_id in requeued["tried_accounts"]
    # The abandoned conversation is kept for the loop to stop, so the branch is
    # held until then.
    assert requeued["abandoned"]
    assert requeued["abandoned"][0]["conversation_id"] == "conv-gone"


def test_lease_reclaim_is_fenced_so_the_dead_worker_cannot_write(lab):
    """The fencing token is the guarantee, not the clock."""
    run = _one_task_run(lab)
    task = lab["lab"].claim_task(str(run["id"]), "w", lease_seconds=-1)
    lab["lab"].reclaim_expired(str(run["id"]))
    fresh = lab["lab"].claim_task(str(run["id"]), "w2")
    # The dead worker tries to settle the task with its stale token: no-op.
    settled = lab["lab"].record_verification(
        task["id"], verdict="completed_verified", evidence={"by": "stale"},
        lease_token=task["lease_token"])
    assert settled["state"] == "dispatched", "a stale verdict must not land"
    assert settled["lease_token"] == fresh["lease_token"]
