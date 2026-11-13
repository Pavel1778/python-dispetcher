"""Acceptance section D — Autonomous Worker.

The task text asks for the deployment path to be exercised for real: start the
API, start ``lab_worker``, submit a task, close the client connection, and prove
the work continues and that its result is still there after reconnecting.

Nothing here is simulated in-process. The web server is the real
:class:`orchestration.web_server.WebServer` on real PostgreSQL with its own loop
switched *off* (``NEXUS_LAB_INLINE=0``), so the only thing that can advance a run
is the separate ``python -m orchestration.lab_worker`` child process. The
outside world is the same three socket servers ``test_studio_api`` uses.

The point of the section is the property a browser cannot provide: the run does
not depend on a client being connected, and a fresh connection sees the result.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_lab_inline import (  # noqa: E402
    _finish,
    _start_run,
    _starts,
    _task_conversation,
    _until,
)
from tests.test_lab_worker_process import Worker, _live_worker_rows  # noqa: E402
from tests.test_studio_api import DSN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run lab tests")

HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


def _detail(studio, project, run):
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    return detail


def test_the_api_and_a_separate_worker_advance_a_run_with_no_client(monkeypatch):
    """API started, worker started, task submitted, client gone: work goes on.

    The run is created over HTTP; from then on the test stops talking to the
    server. The worker process alone takes it from pending to completed, which
    is the property the task text calls "продолжение выполнения" with the client
    connection closed.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        # The API is up and accepts the submission, but advances nothing.
        assert studio.app.lab_inline is False
        project, run = _start_run(studio)
        assert _starts(studio) == [], "the server itself must not start a task"

        worker = Worker(studio, worker_id="acc-worker-1")
        try:
            # The worker is registered and live to the diagnostics.
            live = _until(lambda: _live_worker_rows(studio, "acc-worker-1"),
                          what="the worker to register")
            assert live[0]["kind"] == "worker"

            # The client disconnects: the test keeps only the database and the
            # fakes. The worker dispatches anyway.
            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch with no client connected")
            assert len(_starts(studio)) == 1
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to continue with no client connected")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")

            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                _detail(studio, project, run)), what="the run to finish")
            assert done["run"]["state"] == "completed"
            assert len(_starts(studio)) == 2, "a task was started twice"
        finally:
            worker.stop()


def test_a_result_is_readable_after_reconnecting(monkeypatch):
    """Close the client, let the worker finish, reconnect, read the result.

    The run's outcome is in PostgreSQL, not in the connection that started it,
    so a brand-new client sees the same completed run.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)
        worker = Worker(studio, worker_id="acc-worker-2")
        try:
            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch")
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to continue")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")
            _until(lambda: (lambda d: d if d["run"]["state"] == "completed" else None)(
                _detail(studio, project, run)), what="the run to finish")
        finally:
            worker.stop()

        # A fresh connection to the *same* database reads the result. Restart the
        # server process, as a redeploy or a wake-from-idle would.
        studio.boot()
        detail = _detail(studio, project, run)
        assert detail["run"]["state"] == "completed"
        assert {t["task_key"]: t["state"] for t in detail["tasks"]} == {
            "audit": "completed_verified", "change": "completed_verified"}
        assert len(_starts(studio)) == 2, "restarting the server started a task again"


def test_the_worker_outlives_the_server_process(monkeypatch):
    """The worker is independent: stopping the API does not stop the run.

    The server is stopped entirely (its HTTP listener and its inline loop are
    gone), and the worker still dispatches the task and finishes the run.
    """
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)
        worker = Worker(studio, worker_id="acc-worker-3")
        try:
            # Stop the web server: no client can reach it now.
            for server in list(studio.servers):
                server.stop()
            studio.servers = []

            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch after the server stopped")
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to continue after the server stopped")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")

            # Read the run straight from the database: there is no HTTP server.
            from orchestration.nexus_lab_store import LabStore

            lab = LabStore(studio.app.store)

            def _state():
                row = lab.get_run("user:owner", project, run)
                return row if row["state"] == "completed" else None

            done = _until(_state, what="the run to finish with no server")
            assert done["state"] == "completed"
        finally:
            worker.stop()
