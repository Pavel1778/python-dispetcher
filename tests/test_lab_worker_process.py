"""The separate lab worker as a real process: launched, registered, working.

Everything here is the deployment's own launch path - ``python -m
orchestration.lab_worker`` in a subprocess of its own - against real PostgreSQL
and the same three socket servers the studio tests use (the fake OpenHands Cloud,
the fake GitHub, a fake LLM). The web server is started with the inline loop
*off*, so the only thing that can advance a run is the worker process: if the
worker does not start, register, heartbeat, claim and dispatch, the run stays
where it was.

That is what makes these tests the acceptance for "настроен автономный worker":
the process is not simulated in-process, it is a child process the test starts,
signals and restarts, and it is judged by what changed in PostgreSQL and in the
fake Cloud.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_lab_store import LabStore  # noqa: E402
from tests.test_lab_inline import (  # noqa: E402
    _finish,
    _start_run,
    _starts,
    _task_conversation,
    _until,
)
from tests.test_studio_api import DSN, SERVER_GITHUB_TOKEN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run lab tests")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The commit the fake GitHub reports as the branch head; the agent names it.
HEAD_TEXT = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


def _detail(studio, project, run):
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    return detail


def _worker_env(studio) -> dict:
    """The environment the worker process reads: the same DB and the fakes.

    This is exactly how a deployment points the worker at its services -
    ``SUPABASE_DB_URL`` for the database, ``OPENHANDS_CLOUD_URL`` for Cloud,
    ``GITHUB_API_URL`` for GitHub - so the test exercises the documented launch,
    not a shortcut.
    """
    env = os.environ.copy()
    env["SUPABASE_DB_URL"] = DSN
    env["AUTO_MIGRATE"] = "true"
    env["OPENHANDS_CLOUD_URL"] = studio.cloud.base_url
    env["OPENHANDS_API_KEY"] = "cloud-key-value"
    env["OPENHANDS_TRANSPORT"] = "app"
    env["GITHUB_API_URL"] = studio.app.github.base_url
    env["GITHUB_TOKEN"] = SERVER_GITHUB_TOKEN
    # The worker is its own process; the web server's switch does not reach it.
    env.pop("NEXUS_LAB_INLINE", None)
    return env


class Worker:
    """A ``python -m orchestration.lab_worker`` child process, with cleanup."""

    def __init__(self, studio, *, worker_id: str, poll_interval: float = 0.3):
        self.env = _worker_env(studio)
        self.worker_id = worker_id
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "orchestration.lab_worker",
             "--worker-id", worker_id, "--poll-interval", str(poll_interval)],
            cwd=REPO_ROOT, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self, *, sig=signal.SIGTERM, timeout: float = 10.0):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.proc.returncode

    def alive(self) -> bool:
        return self.proc.poll() is None

    def output(self) -> str:
        if self.proc.stdout is None:
            return ""
        self.proc.stdout.flush()
        try:
            return self.proc.stdout.read() or ""
        except Exception:  # noqa: BLE001 - a closed pipe while stopping
            return ""


def _live_worker_rows(studio, worker_id: str) -> list[dict]:
    return [w for w in LabStore(studio.app.store).live_workers(max_age_seconds=30)
            if w["worker_id"] == worker_id]


def test_the_worker_process_registers_heartbeats_and_completes_a_run(monkeypatch):
    """The whole point: a separate process, started as a deployment starts it,
    takes a run from pending tasks to completed, and is a live worker to the
    diagnostics while it does."""
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        assert studio.app.lab_inline is False        # the server advances nothing
        project, run = _start_run(studio)            # a real run, tasks pending
        assert _starts(studio) == []                 # nothing started yet

        worker = Worker(studio, worker_id="proc-lab-1")
        try:
            # 1. Registered and heartbeating: the diagnostics' live-worker check.
            live = _until(lambda: _live_worker_rows(studio, "proc-lab-1"),
                          what="the worker to register and heartbeat")
            assert live[0]["kind"] == "worker"
            status, report = studio.api.get("/api/openhands/diagnostics")
            assert status == 200
            check = next(c for c in report["checks"] if c["name"] == "lab_worker")
            assert check["status"] == "VERIFIED", check
            assert "proc-lab-1" in check["evidence"]["live_workers"]

            # 2. Claimed and dispatched the first task, in the fake Cloud.
            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch the first task")
            assert len(_starts(studio)) == 1

            # 3. The agent's report names the branch head; the task verifies and
            #    the worker starts the next one.
            _finish(studio, first, f"Готово. Коммит {HEAD_TEXT}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to dispatch the second task")
            _finish(studio, second, f"Готово. Коммит {HEAD_TEXT}.")

            # 4. The run reaches completed - the worker, and only the worker,
            #    did it.
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed", done["run"]
            assert {t["task_key"]: t["state"] for t in done["tasks"]} == {
                "audit": "completed_verified", "change": "completed_verified"}
            assert len(_starts(studio)) == 2, "a task was started more than once"

            # 5. The process is still up: it did not die finishing the run.
            assert worker.alive(), worker.output()
        finally:
            worker.stop()


def test_a_restarted_worker_process_continues_the_run(monkeypatch):
    """A worker that is killed mid-run does not lose the run.

    The task's conversation was already started, so the work is in the Cloud;
    a fresh worker process follows it and finishes the run. Nothing is started
    twice - the crash of the process that dispatched it is not a reason to start
    a second conversation.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)

        first_worker = Worker(studio, worker_id="proc-lab-kill")
        first = _until(lambda: _task_conversation(studio, "audit"),
                       what="the first worker to dispatch the task")
        # Kill it hard, as a crash or an OOM would: no graceful shutdown.
        first_worker.stop(sig=signal.SIGKILL)

        # A second worker process, on the same database, picks the run up.
        second_worker = Worker(studio, worker_id="proc-lab-2")
        try:
            _finish(studio, first, f"Готово. Коммит {HEAD_TEXT}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the second worker to continue the run")
            _finish(studio, second, f"Готово. Коммит {HEAD_TEXT}.")
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed", done["run"]
            # The first task was not started a second time by the new worker.
            assert len(_starts(studio)) == 2
        finally:
            second_worker.stop()


def test_the_worker_keeps_working_with_no_client_connected(monkeypatch):
    """Closing the browser changes nothing: the worker is a separate process.

    The run is created over HTTP, then the test stops talking to the server
    entirely; the worker still dispatches the task. There is no client
    connection for the work to depend on.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)
        worker = Worker(studio, worker_id="proc-lab-headless")
        try:
            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch without a client")
            _finish(studio, first, f"Готово. Коммит {HEAD_TEXT}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to keep going without a client")
            _finish(studio, second, f"Готово. Коммит {HEAD_TEXT}.")
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed"
        finally:
            worker.stop()
