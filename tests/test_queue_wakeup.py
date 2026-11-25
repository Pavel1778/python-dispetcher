"""Commit-only queue hints and listener fallback against disposable PostgreSQL."""
from __future__ import annotations

import os
import threading

import pytest

from orchestration.queue_wakeup import QueueWakeup, from_environment

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
requires_pg = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")


def test_listener_is_opt_in(monkeypatch):
    monkeypatch.delenv("NEXUS_LISTEN_DSN", raising=False)
    assert from_environment({}) is None


@requires_pg
def test_transactional_hints_and_real_listener():
    import psycopg
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(DSN)
    registered = threading.Event()
    task_wake = threading.Event()
    lab_wake = threading.Event()

    def task_callback():
        task_wake.set()
        registered.set()

    listener = QueueWakeup(DSN, {"task": task_callback, "lab": lab_wake.set})
    try:
        listener.start()
        assert registered.wait(5), "listener did not subscribe"
        assert lab_wake.wait(5), "listener did not finish initial wake"
        task_wake.clear()
        lab_wake.clear()
        with psycopg.connect(DSN) as conn:
            conn.execute("INSERT INTO tasks(goal) VALUES (%s)", ("will rollback",))
            assert not task_wake.wait(0.2), "notification arrived before commit"
            conn.rollback()
        assert not task_wake.wait(0.2), "rollback emitted a notification"

        store.submit_task("committed task")
        assert task_wake.wait(5), "committed task did not wake listener"
        task_wake.clear()
        projects = ProjectService(store)
        project = str(projects.create("user:wake-test", "Wakeup test")["id"])
        goal = str(projects.create_goal("user:wake-test", project, "Test")[0]["id"])
        LabStore(store).start_run("user:wake-test", project, goal)
        assert lab_wake.wait(5), "committed run did not wake listener"
        assert not task_wake.is_set(), "lab hint woke the unrelated task callback"
    finally:
        listener.stop()
        store.close()


@requires_pg
def test_listener_reconnects_and_wakes_after_connection_loss():
    import psycopg
    from orchestration.cloud_store import PostgresStore

    store = PostgresStore(DSN)
    wakes = 0
    condition = threading.Condition()

    def wake():
        nonlocal wakes
        with condition:
            wakes += 1
            condition.notify_all()

    listener = QueueWakeup(DSN, {"task": wake})
    try:
        listener.start()
        with condition:
            assert condition.wait_for(lambda: wakes >= 1, timeout=5)
        with psycopg.connect(DSN, autocommit=True) as conn:
            row = conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = %s AND datname = current_database()",
                ("nexus_queue_listener",),
            ).fetchone()
            assert row is not None and row[0]
        with condition:
            assert condition.wait_for(lambda: wakes >= 2, timeout=8), "no rescan after reconnect"
        store.submit_task("notification after reconnect")
        with condition:
            assert condition.wait_for(lambda: wakes >= 3, timeout=5)
    finally:
        listener.stop()
        store.close()


@requires_pg
def test_committed_task_wakes_dispatch_loop_without_waiting_for_timer(monkeypatch):
    from types import SimpleNamespace

    from orchestration import web_server
    from orchestration.cloud_store import PostgresStore

    store = PostgresStore(DSN)
    stop = threading.Event()
    dispatch_wake = threading.Event()
    subscribed = threading.Event()
    condition = threading.Condition()
    passes = 0

    class Tick:
        def as_dict(self):
            return {}

    def tick():
        nonlocal passes
        with condition:
            passes += 1
            condition.notify_all()
        return Tick()

    def notify():
        dispatch_wake.set()
        subscribed.set()

    listener = QueueWakeup(DSN, {"task": notify})
    app = SimpleNamespace(
        _stop=stop, _dispatch_wake=dispatch_wake, poll_interval=30,
        store=store, client=None,
        openhands_accounts=SimpleNamespace(execution_client_internal=None),
        runtime=SimpleNamespace(tick=tick), dispatcher=SimpleNamespace(tick=lambda: Tick()),
        verifier=SimpleNamespace(tick=lambda: Tick()), _maybe_sweep=lambda: None,
    )
    monkeypatch.setattr(web_server, "startup_sync", lambda *_args: None)
    thread = threading.Thread(target=web_server.App._run, args=(app,))
    try:
        listener.start()
        assert subscribed.wait(5)
        thread.start()
        with condition:
            assert condition.wait_for(lambda: passes >= 1, timeout=5)
            baseline = passes
        store.submit_task("wake the real dispatch loop")
        with condition:
            assert condition.wait_for(lambda: passes > baseline, timeout=5)
    finally:
        stop.set()
        dispatch_wake.set()
        thread.join(timeout=5)
        listener.stop()
        store.close()
        assert not thread.is_alive()


@requires_pg
def test_separate_lab_worker_wakes_when_run_is_committed(monkeypatch):
    from types import SimpleNamespace

    from orchestration import execution_profiles, queue_wakeup
    from orchestration.cloud_store import PostgresStore
    from orchestration.lab_worker import LabWorker
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(DSN)
    projects = ProjectService(store)
    project = str(projects.create("user:lab-wakeup", "Wakeup")["id"])
    goal = str(projects.create_goal("user:lab-wakeup", project, "Wake")[0]["id"])
    worker = object.__new__(LabWorker)
    worker.store = store
    worker.client = None
    worker.worker_id = "test-queue-wakeup"
    worker.poll_interval = 30
    worker._stop = threading.Event()
    worker._wake = threading.Event()
    worker.lab = SimpleNamespace(register_worker=lambda *_a, **_kw: None,
                                 stop_worker=lambda *_a, **_kw: None)
    subscribed = threading.Event()
    ready = threading.Event()
    condition = threading.Condition()
    passes = 0

    def notify():
        worker.wake()
        subscribed.set()

    def tick():
        nonlocal passes
        with condition:
            passes += 1
            if passes == 1:
                assert subscribed.wait(5)
                worker._wake.clear()  # consume the initial subscribe rescan
                ready.set()
            condition.notify_all()
        return {"advanced": 0, "errors": []}

    worker.tick_once = tick
    monkeypatch.setattr(execution_profiles, "startup_sync", lambda *_args: None)
    monkeypatch.setattr(queue_wakeup, "from_environment", lambda _callbacks: QueueWakeup(DSN, {"lab": notify}))
    thread = threading.Thread(target=worker.run_forever)
    try:
        thread.start()
        assert ready.wait(5)
        with condition:
            baseline = passes
        LabStore(store).start_run("user:lab-wakeup", project, goal)
        with condition:
            assert condition.wait_for(lambda: passes > baseline, timeout=5)
    finally:
        worker.stop()
        thread.join(timeout=6)
        store.close()
        assert not thread.is_alive()


@requires_pg
def test_notification_burst_is_coalesced_without_losing_last_wakeup():
    import time

    import psycopg
    from orchestration.queue_wakeup import MIN_WAKE_INTERVAL

    times = []
    condition = threading.Condition()

    def wake():
        with condition:
            times.append(time.monotonic())
            condition.notify_all()

    listener = QueueWakeup(DSN, {"task": wake})
    try:
        listener.start()
        with condition:
            assert condition.wait_for(lambda: len(times) >= 1, timeout=5)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_notify('nexus_queue_wakeup','task')")
            with condition:
                assert condition.wait_for(lambda: len(times) >= 2, timeout=5)
            for _ in range(20):
                conn.execute("SELECT pg_notify('nexus_queue_wakeup','task')")
            with condition:
                assert condition.wait_for(lambda: len(times) >= 3, timeout=5)
                assert times[2] - times[1] >= MIN_WAKE_INTERVAL - 0.02
                assert not condition.wait_for(lambda: len(times) > 3, timeout=0.2)
    finally:
        listener.stop()


@requires_pg
def test_notification_fans_out_to_two_independent_listeners():
    import psycopg

    first, second = threading.Event(), threading.Event()
    listeners = [QueueWakeup(DSN, {"lab": event.set}) for event in (first, second)]
    try:
        for listener in listeners:
            listener.start()
        assert first.wait(5) and second.wait(5), "listeners did not subscribe"
        first.clear()
        second.clear()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_notify('nexus_queue_wakeup', 'lab')")
        assert first.wait(5) and second.wait(5)
    finally:
        for listener in listeners:
            listener.stop()


def test_listener_error_does_not_log_credentials(monkeypatch, caplog):
    import time

    from orchestration import queue_wakeup

    attempted = threading.Event()

    def fail(*_args, **_kwargs):
        attempted.set()
        raise ValueError("password=secret-value")

    monkeypatch.setattr(queue_wakeup.psycopg, "connect", fail)
    listener = QueueWakeup("password=secret-value", {"task": lambda: None})
    try:
        listener.start()
        assert attempted.wait(2)
        end = time.monotonic() + 2
        while "queue listener unavailable" not in caplog.text and time.monotonic() < end:
            time.sleep(0.01)
        assert "queue listener unavailable" in caplog.text
        assert "secret-value" not in caplog.text
    finally:
        listener.stop()


@requires_pg
def test_approval_of_a_queued_task_wakes_dispatcher():
    from orchestration.cloud_store import PostgresStore

    store = PostgresStore(DSN)
    wake = threading.Event()
    listener = QueueWakeup(DSN, {"task": wake.set})
    try:
        task = store.submit_task("requires approval", requires_approval=True)
        listener.start()
        assert wake.wait(5)
        wake.clear()
        store.approve(task.id, approved=True)
        assert wake.wait(5)
    finally:
        listener.stop()
        store.close()
