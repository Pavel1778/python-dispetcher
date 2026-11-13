"""A start that the service has accepted is not lost with the process that
asked for it.

What production showed (2026-11-13). The server advances runs itself, and it
is deployed from the branch the agents push to. Every task ended with a push;
every push replaced the server about fifty seconds later; and by then the
server had claimed the next task and was waiting for its conversation to come
up - a start takes about a minute on the live service. The process died with
the start task in its memory. The conversation went to work with nobody
following it, and the task stood claimed until its lease ran out, fifteen
minutes later, when the orphan was found by its tags and adopted as a second
attempt. Five tasks in one night lost a quarter of an hour and an attempt each.

The start task is on the task's row now (migration 0034), so the process that
runs next finishes the start. These tests replace real processes - the web
server with its own loop, and the separate worker - at exactly that moment,
against real PostgreSQL and the socket servers the studio tests use.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.lab_worker import LabWorker  # noqa: E402
from tests.test_durable_runs import (  # noqa: E402,F401
    Conversations,
    env,
    scheduler,
    sql,
    start,
    tasks_of,
)
from tests.test_lab_inline import (  # noqa: E402
    HEAD,
    _finish,
    _start_run,
    _starts,
    _task_conversation,
    _until,
)
from tests.test_lab_worker_process import REPO_ROOT, Worker, _detail, _worker_env  # noqa: E402
from tests.test_studio_api import DSN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run lab tests")

DONE = f"Готово. Коммит {HEAD}."


class Server:
    """``python -m orchestration.web_server`` as a deployment starts it: its
    own process, advancing runs in its own loop (worker ``web-lab``)."""

    def __init__(self, studio):
        environment = _worker_env(studio)
        environment.update({"WEB_AUTH_TOKEN": "machine-token", "AUTH_USERNAME": "owner",
                    "AUTH_SESSION_SECRET": "handover-test-secret",
                    "PYTHONUNBUFFERED": "1"})
        environment.pop("AUTH_PASSWORD_HASH", None)
        from orchestration.web_auth import hash_password

        environment["AUTH_PASSWORD_HASH"] = hash_password("handover-password", iterations=1000)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "orchestration.web_server", "--host", "127.0.0.1",
             "--port", "0", "--poll-interval", "0.3"],
            cwd=REPO_ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self, *, sig=signal.SIGTERM, timeout: float = 25.0):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.proc.returncode

    def output(self) -> str:
        try:
            return self.proc.stdout.read() if self.proc.stdout else ""
        except Exception:  # noqa: BLE001 - a closed pipe while stopping
            return ""


def _task(studio, key: str) -> dict:
    with studio.app.store._tx() as cur:
        cur.execute("SELECT * FROM nexus_run_tasks WHERE task_key=%s", (key,))
        return LabStore._task_row(cur.fetchone())


def _rows(studio, statement: str, *params) -> list[tuple]:
    with studio.app.store._tx() as cur:
        cur.execute(statement, params)
        return [tuple(row.values()) for row in cur.fetchall()]


def _start_is_recorded(studio, key: str = "audit") -> dict:
    """Wait for the moment production kept hitting: the service has the start,
    the task has it written down, and there is no conversation yet."""
    def recorded():
        task = _task(studio, key)
        return task if task.get("pending_start") else None

    task = _until(recorded, what="the accepted start to be written on the task")
    assert task["state"] == "dispatched" and task["conversation_id"] is None
    assert task["pending_start"]["start_task_id"]
    return task


def _sandbox_is_slow(studio) -> None:
    studio.cloud.cloud.start_polls_required = 10 ** 6


def _sandbox_is_ready(studio) -> None:
    studio.cloud.cloud.start_polls_required = 0


def _bound(studio, key: str = "audit") -> dict:
    def running():
        task = _task(studio, key)
        return task if task["conversation_id"] else None

    return _until(running, what="the conversation to be bound to the task")


def _is_the_first_attempt(studio, task: dict) -> None:
    """What "nothing was lost" means: the task is running in the conversation
    its one start produced, on its first attempt, and it never waited out a
    lease."""
    assert task["state"] == "running" and task["attempt"] == 1, task
    assert task["conversation_id"] == _task_conversation(studio, task["task_key"])
    assert len(_starts(studio)) == 1, "the task was started a second time"
    assert task["pending_start"] is None
    assert "lease expired" not in (task["last_error"] or "")
    assert not [item for item in task["evidence"] if item.get("reclaimed")]
    assert _rows(studio, "SELECT kind, attempt FROM nexus_task_executions "
                         "WHERE task_id=%s ORDER BY started_at", task["id"]) == [("start", 1)]
    assert _rows(studio, "SELECT conversation_id FROM nexus_executor_stops") == []


def _run_ends(studio, project, run, first: str) -> None:
    _finish(studio, first, DONE)
    second = _until(lambda: _task_conversation(studio, "change"),
                    what="the next task to be started")
    _finish(studio, second, DONE)
    done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
        _detail(studio, project, run)), what="the run to finish")
    assert done["run"]["state"] == "completed", done["run"]
    assert {t["task_key"]: (t["state"], t["attempt"]) for t in done["tasks"]} == {
        "audit": ("completed_verified", 1), "change": ("completed_verified", 1)}
    assert len(_starts(studio)) == 2


# ------------------------------------------------ the server, as on Render
def test_a_deployment_replaces_the_server_while_a_conversation_is_coming_up(monkeypatch):
    """SIGTERM to the server that has just been given a start - what a
    deployment does to the process it replaces. It leaves at once, cleanly, and
    the server that takes its place binds the conversation when it is there."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _sandbox_is_slow(studio)
        project, run = _start_run(studio)
        old = Server(studio)
        try:
            _start_is_recorded(studio)
            began = time.monotonic()
            code = old.stop()
            took = time.monotonic() - began
            assert code == 0, f"the server did not stop cleanly ({code}):\n{old.output()}"
            # It does not wait for the conversation (that can be minutes).
            assert took < 15, f"stopping took {took:.1f} s"
            assert _rows(studio, "SELECT state FROM nexus_lab_workers "
                                 "WHERE worker_id='web-lab'") == [("stopped",)]
            # The claim is as it was: nothing released, nothing started over.
            task = _task(studio, "audit")
            assert (task["state"], task["attempt"], task["conversation_id"]) == (
                "dispatched", 1, None)
        finally:
            old.stop()

        new = Server(studio)
        try:
            # While the sandbox is not there, there is nothing to bind - and
            # nothing is started beside it.
            time.sleep(1.5)
            assert _task(studio, "audit")["conversation_id"] is None
            assert len(_starts(studio)) == 1
            _sandbox_is_ready(studio)
            task = _bound(studio)
            _is_the_first_attempt(studio, task)
            _run_ends(studio, project, run, task["conversation_id"])
        finally:
            assert new.stop() == 0


# ----------------------------------------------------- the separate worker
@pytest.mark.parametrize("replacement", ["handover-1", "handover-2"],
                         ids=["same worker id", "another worker id"])
def test_a_worker_killed_while_a_conversation_is_coming_up_loses_nothing(
        monkeypatch, replacement):
    """SIGKILL - a crash, an OOM, a platform out of patience: no handler runs.
    The start is on the row all the same, and the next worker - under the same
    name or another - finishes it."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _sandbox_is_slow(studio)
        project, run = _start_run(studio)
        first = Worker(studio, worker_id="handover-1")
        try:
            _start_is_recorded(studio)
        finally:
            first.stop(sig=signal.SIGKILL)
        second = Worker(studio, worker_id=replacement)
        try:
            _sandbox_is_ready(studio)
            task = _bound(studio)
            _is_the_first_attempt(studio, task)
            _run_ends(studio, project, run, task["conversation_id"])
        finally:
            second.stop()


def test_a_worker_told_to_stop_does_not_wait_for_the_conversation(monkeypatch):
    """SIGTERM to the worker process. Before, it stayed in the wait for the
    conversation - up to five minutes - and whoever stopped it lost patience
    and killed it."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _sandbox_is_slow(studio)
        project, run = _start_run(studio)
        first = Worker(studio, worker_id="handover-term")
        _start_is_recorded(studio)
        began = time.monotonic()
        assert first.stop(timeout=12) == 0, first.output()
        assert time.monotonic() - began < 10
        second = Worker(studio, worker_id="handover-term")
        try:
            _sandbox_is_ready(studio)
            task = _bound(studio)
            _is_the_first_attempt(studio, task)
        finally:
            second.stop()


def test_two_live_workers_bind_one_start_once(monkeypatch):
    """The worker that asked is alive and waiting; another worker sees the same
    start on the row and asks too. Both read; one binds; the other's write
    changes nothing - one entry in the history, no stop signal for the
    conversation, one start."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _sandbox_is_slow(studio)
        project, run = _start_run(studio)
        first = Worker(studio, worker_id="handover-a")
        second = None
        try:
            _start_is_recorded(studio)
            second = Worker(studio, worker_id="handover-b")
            time.sleep(1.5)                      # both are asking now
            assert first.alive() and second.alive()
            _sandbox_is_ready(studio)
            task = _bound(studio)
            time.sleep(2.5)                      # the slower of the two has written too
            _is_the_first_attempt(studio, _task(studio, "audit"))
            _run_ends(studio, project, run, task["conversation_id"])
            assert _rows(studio, "SELECT count(*) FROM nexus_task_executions") == [(2,)]
        finally:
            first.stop()
            if second is not None:
                second.stop()


def test_a_start_that_fails_after_its_worker_left_is_a_failed_start(monkeypatch):
    """The sandbox never came up. The worker that asked is gone; the one that
    follows the start is told so by the service and treats it as the failed
    dispatch it is: the task is tried again at once, with the real reason on
    it - not left until a lease runs out."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _sandbox_is_slow(studio)
        project, run = _start_run(studio)
        first = Worker(studio, worker_id="handover-fail")
        try:
            _start_is_recorded(studio)
        finally:
            first.stop(sig=signal.SIGKILL)
        lost = _task_conversation(studio, "audit")
        studio.cloud.cloud.start_fail = True      # this start ends in ERROR
        _sandbox_is_ready(studio)
        second = Worker(studio, worker_id="handover-fail")
        try:
            def tried_again():
                task = _task(studio, "audit")
                return task if task["attempt"] == 2 else None

            _until(lambda: len(_starts(studio)) == 2, what="the task to be started again")
            studio.cloud.cloud.start_fail = False
            task = _until(lambda: (lambda t: t if t["conversation_id"] else None)(tried_again()),
                          what="the second attempt to be bound")
            assert task["state"] == "running" and task["conversation_id"] != lost
            assert "start task failed" in (task["last_error"] or ""), task["last_error"]
            assert "lease expired" not in (task["last_error"] or "")
        finally:
            second.stop()


# ------------------------------------------------------------- the row itself
RECORD = {"start_task_id": "start-task-1", "account_id": None, "kind": "start",
          "delivery": {"mode": "inline"}, "base_sha": "a" * 40, "previous_account": None}


def test_a_start_is_kept_for_the_claim_that_asked_and_only_while_it_holds(env):  # noqa: F811
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    assert lab.pending_starts(run_id) == []
    claim = lab.claim_task(run_id, "first", lease_seconds=900)
    assert claim["pending_start"] is None

    assert lab.record_start(claim["id"], start=RECORD, lease_token=claim["lease_token"]) is True
    [kept] = lab.pending_starts(run_id)
    assert kept["id"] == claim["id"]
    assert kept["pending_start"] == {**RECORD, "lease_token": claim["lease_token"]}
    # A worker whose claim was taken away meanwhile writes nothing.
    assert lab.record_start(claim["id"], start={**RECORD, "start_task_id": "stale"},
                            lease_token=claim["lease_token"] - 1) is False
    assert lab.pending_starts(run_id)[0]["pending_start"]["start_task_id"] == "start-task-1"

    # Its failure is taken by one worker, once.
    assert lab.take_start(claim["id"], lease_token=claim["lease_token"] - 1) is False
    assert lab.take_start(claim["id"], lease_token=claim["lease_token"]) is True
    assert lab.take_start(claim["id"], lease_token=claim["lease_token"]) is False
    assert lab.pending_starts(run_id) == []
    assert lab.record_start(claim["id"], start=RECORD, lease_token=claim["lease_token"]) is True

    # The claim runs out with nothing to show: the start is followed no longer,
    # and the task is taken back as it always was.
    sql(env, "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
             "WHERE id=%s", claim["id"])
    assert lab.pending_starts(run_id) == []
    [taken_back] = lab.reclaim_expired(run_id)
    assert taken_back["state"] == "pending"
    assert lab.pending_starts(run_id) == []
    # The next claim owes nothing to the old start.
    again = lab.claim_task(run_id, "second", lease_seconds=900)
    assert again["attempt"] == 2 and again["pending_start"] is None
    assert again["lease_token"] > claim["lease_token"]
    assert lab.record_start(again["id"], start=RECORD, lease_token=claim["lease_token"]) is False
    assert lab.pending_starts(run_id) == []


def test_binding_the_conversation_a_task_already_has_changes_nothing(env):  # noqa: F811
    """Two workers that followed one start both come to bind it."""
    lab = env["lab"]
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    claim = lab.claim_task(run_id, "first", lease_seconds=900)
    lab.record_start(claim["id"], start=RECORD, lease_token=claim["lease_token"])

    first = lab.mark_running(claim["id"], conversation_id="conv-1", base_sha="a" * 40,
                             delivery={"mode": "inline"}, lease_token=claim["lease_token"])
    assert first["state"] == "running" and first["conversation_id"] == "conv-1"
    assert first["pending_start"] is None, "a bound start is not followed again"
    assert lab.pending_starts(run_id) == []
    started_at = tasks_of(env, run_id)["build"]["started_at"]

    second = lab.mark_running(claim["id"], conversation_id="conv-1", base_sha="b" * 40,
                              delivery={"mode": "file"}, kind="adopted",
                              lease_token=claim["lease_token"])
    assert second["conversation_id"] == "conv-1" and second["base_sha"] == "a" * 40
    assert tasks_of(env, run_id)["build"]["started_at"] == started_at
    with env["store"]._tx() as cur:
        cur.execute("SELECT kind, outcome, ended_at FROM nexus_task_executions")
        history = [tuple(row.values()) for row in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM nexus_executor_stops")
        stops = cur.fetchone()["n"]
    assert history == [("start", None, None)], "one conversation, one entry, still open"
    assert stops == 0, "the conversation at work must not be told to stop"

    # A different conversation for the same task is still what it was: the
    # first is superseded.
    lab.mark_running(claim["id"], conversation_id="conv-2", lease_token=claim["lease_token"])
    with env["store"]._tx() as cur:
        cur.execute("SELECT conversation_id, outcome FROM nexus_task_executions "
                    "ORDER BY started_at, id")
        assert [tuple(row.values()) for row in cur.fetchall()] == [
            ("conv-1", "superseded"), ("conv-2", None)]



# ------------------------------------------------------ a worker that is stopping
def test_a_worker_that_is_stopping_claims_nothing_new(env):  # noqa: F811
    """A task claimed while the process is being stopped would be a start it
    cannot see through. What is at work is still looked at."""
    run_id = start(env, [{"task_key": "build", "title": "Build"},
                         {"task_key": "ship", "title": "Ship", "dependencies": ["build"]}])
    executor = Conversations()
    scheduler(env, executor).tick(run_id)                    # build is at work
    executor.finish(executor.of("build")[0])

    stopping = scheduler(env, executor, stopping=lambda: True)
    result = stopping.tick(run_id)
    assert result.verified == "build", "the task at work was looked at and its result kept"
    assert result.claimed is None and result.note.startswith("stopping")
    assert tasks_of(env, run_id)["ship"]["state"] == "pending"
    assert [key for key, _ in executor.started] == ["build"]

    # The next process takes it from there.
    assert scheduler(env, executor).tick(run_id).claimed == "ship"


def test_a_pass_begins_no_other_run_once_the_worker_is_told_to_stop(env):  # noqa: F811
    run_id = start(env, [{"task_key": "build", "title": "Build"}])
    told = {"stop": True}
    worker = LabWorker(env["store"], worker_id="leaving", client=object(), github=object(),
                       discover_router=False, stopping=lambda: told["stop"])
    summary = worker.tick_once()
    assert summary["runs"] == 0 and summary.get("stopping") is True
    assert tasks_of(env, run_id)["build"]["state"] == "pending"
    told["stop"] = False
    assert worker.tick_once()["runs"] == 1
