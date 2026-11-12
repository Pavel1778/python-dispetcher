"""Planner validation and the durable worker's recovery behaviour.

The planner is validated with the real cascade against a local fake provider.
The worker is exercised with a fake executor/verifier so the *durability* is
what is tested: a restart re-reads the active runs and continues; a dead
worker's lease is reclaimed.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.lab_worker import LabWorker, active_run_ids  # noqa: E402
from orchestration.nexus_lab import TaskReport, Verification  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_planner import Planner, store_plan, validate_tasks  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.providers import ProviderCascade, GroqProvider  # noqa: E402
from tests.test_providers import FakeProvider, chat_body  # noqa: E402

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


# --------------------------------------------------------------------- validate
def test_validate_tasks_accepts_a_valid_dag():
    tasks = validate_tasks({"tasks": [
        {"id": "a", "title": "A"},
        {"id": "b", "title": "B", "dependencies": ["a"], "acceptance": ["works"]},
    ]})
    assert [t["id"] for t in tasks] == ["a", "b"]
    assert tasks[1]["dependencies"] == ["a"]


def test_validate_tasks_rejects_cycles_unknown_and_self_deps():
    for bad in (
        {"tasks": [{"id": "a", "title": "A", "dependencies": ["b"]},
                   {"id": "b", "title": "B", "dependencies": ["a"]}]},      # cycle
        {"tasks": [{"id": "a", "title": "A", "dependencies": ["ghost"]}]},  # unknown
        {"tasks": [{"id": "a", "title": "A", "dependencies": ["a"]}]},      # self
        {"tasks": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]},  # dup id
        {"tasks": [{"id": "a"}]},                                            # no title
        {"tasks": []},                                                       # empty
    ):
        with pytest.raises(ValueError):
            validate_tasks(bad)


# ------------------------------------------------------------------ planner live
def test_planner_produces_validated_tasks(gh_server=None):
    from http.server import ThreadingHTTPServer
    import threading

    plan = {"tasks": [
        {"id": "audit", "title": "Audit", "acceptance": ["report exists"]},
        {"id": "impl", "title": "Implement", "dependencies": ["audit"],
         "acceptance": ["tests pass"]},
    ]}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    srv.script = [{"payload": chat_body(plan)}]
    srv.calls = []
    srv.index = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        cascade = ProviderCascade([
            GroqProvider(base_url=f"http://127.0.0.1:{srv.server_port}",
                         api_key="k", model="m", sleep=lambda _s: None)])
        tasks, meta = Planner(cascade).plan(goal="Ship it", project_name="Demo",
                                            context={"languages": {"Python": 3}},
                                            memory=[{"content": "verified", "provenance": "test_verified"}])
        assert [t["id"] for t in tasks] == ["audit", "impl"]
        assert meta["provider"] == "groq" and meta["task_count"] == 2
    finally:
        srv.shutdown()
        t.join()
        srv.server_close()


# ------------------------------------------------------------------- worker live
class FakeExecutor:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}

    def dispatch(self, task):
        return f"conv-{task['task_key']}"

    def poll(self, task, conversation_id):
        return TaskReport(state=self.outcomes.get(task["task_key"], "finished"),
                          final_text="ok", conversation_id=conversation_id)


class FakeVerifier:
    def verify(self, task, report):
        return Verification("completed_verified", "fake", {})


@pytest.fixture
def env():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Lab")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    lab = LabStore(store)
    yield {"store": store, "svc": svc, "project": project, "goal": goal, "lab": lab}
    store.close()


def test_worker_completes_a_run_and_restart_is_a_non_event(env):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])

    # First worker: one pass dispatches, a second verifies and completes.
    w1 = LabWorker(env["store"], worker_id="w1", client=object(), github=object())
    w1.executor = FakeExecutor()
    w1.verifier = FakeVerifier()
    w1.tick_once()
    w1.tick_once()
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"

    # A fresh worker process sees no active runs and does nothing (no duplicates).
    w2 = LabWorker(env["store"], worker_id="w2", client=object(), github=object())
    w2.executor = FakeExecutor()
    w2.verifier = FakeVerifier()
    summary = w2.tick_once()
    assert summary["runs"] == 0


def test_worker_recovers_a_dead_worker_lease(env):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])
    # A dead worker claimed the task and vanished.
    env["lab"].claim_task(run["id"], "dead", lease_seconds=0)
    assert str(run["id"]) in active_run_ids(env["store"])
    # The live worker reclaims the expired lease and finishes.
    w = LabWorker(env["store"], worker_id="live", client=object(), github=object())
    w.executor = FakeExecutor()
    w.verifier = FakeVerifier()
    w.tick_once()   # reclaim + dispatch
    w.tick_once()   # verify + complete
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"


def test_worker_survives_a_failing_run(env):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])

    class BoomExecutor(FakeExecutor):
        def dispatch(self, task):
            raise RuntimeError("kaboom")

    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])
    w = LabWorker(env["store"], worker_id="w", client=object(), github=object())
    w.executor = BoomExecutor()
    w.verifier = FakeVerifier()
    summary = w.tick_once()  # must not raise
    assert summary["errors"] and summary["runs"] == 1


def test_the_worker_loop_outlives_a_transient_database_error(env, monkeypatch):
    """A database blip must not end the process.

    The worker holds one connection for its life and the store raises
    ``StoreUnavailable`` while it reconnects. The web server's own loop catches
    that and keeps going; the separate worker used to let it propagate out of
    ``run_forever``, so one transient error (a database restart, a pooler
    recycling its clients) killed the only thing advancing the runs. Here the
    run is real: the first pass sees an unavailable store, later passes work.
    """
    import threading
    import time

    from orchestration import lab_worker as lw
    from orchestration.cloud_store import StoreUnavailable

    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])

    real = lw.active_run_ids
    calls = {"n": 0}

    def flaky(store):
        calls["n"] += 1
        if calls["n"] == 2:                 # the first pass the loop makes
            raise StoreUnavailable("database unavailable")
        return real(store)

    monkeypatch.setattr(lw, "active_run_ids", flaky)
    w = LabWorker(env["store"], worker_id="live", poll_interval=0.02,
                  client=object(), github=object())
    w.executor = FakeExecutor()
    w.verifier = FakeVerifier()
    thread = threading.Thread(target=w.run_forever, daemon=True)
    thread.start()
    try:
        # The loop kept ticking through the error and finished the run: a
        # process that died on the first pass would never complete it.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed":
                break
            time.sleep(0.05)
        assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"
        assert thread.is_alive(), "the worker loop died on a transient database error"
        assert calls["n"] >= 3, "the loop did not retry after the error"
    finally:
        w.stop()
        thread.join(timeout=5)


def test_run_once_registers_and_heartbeats_like_a_long_lived_worker(env):
    """``--once`` is the cron-style deployment: it must leave the same trace a
    long-lived worker does, or the diagnostics read the runs as unserviced."""
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])
    w = LabWorker(env["store"], worker_id="cron-1", client=object(), github=object())
    w.executor = FakeExecutor()
    w.verifier = FakeVerifier()
    summary = w.run_once()
    assert summary["runs"] == 1
    # The pass advanced the run (one claim), and the worker row is there -
    # marked stopped, because a single pass cannot stay live.
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["iteration"] == 1
    with env["store"]._tx() as cur:
        cur.execute("SELECT worker_id, kind, state, heartbeat_at FROM nexus_lab_workers "
                    "WHERE worker_id=%s", ("cron-1",))
        stored = cur.fetchone()
    assert stored is not None
    assert (stored["kind"], stored["state"]) == ("worker", "stopped")
    assert stored["heartbeat_at"] is not None


def test_run_once_still_ticks_when_it_cannot_register(env):
    """A pass that cannot write its worker row must still advance the runs."""
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    store_plan(env["lab"], "user:alice", env["project"], run["id"],
               tasks=[{"id": "a", "title": "A", "description": "", "acceptance": [],
                       "dependencies": []}])
    w = LabWorker(env["store"], worker_id="cron-2", client=object(), github=object())
    w.executor = FakeExecutor()
    w.verifier = FakeVerifier()

    def boom(_worker_id, kind="worker"):
        raise RuntimeError("database unavailable")

    w.lab.register_worker = boom
    summary = w.run_once()
    assert summary["runs"] == 1
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["iteration"] == 1
