"""Cancel must lock task rows before execution rows.

Every worker-side settle (``mark_running``, ``record_verification``,
``retry_task``, ``release_task``, ...) locks the task row first and closes the
execution second. The cancel path used to do the opposite inside
``LabStore._apply_run_state``: it batch-updated ``nexus_task_executions`` for
the whole run first and only then batch-updated ``nexus_run_tasks``. A worker
holding its task row and waiting on its executions, against a cancel holding
the run's executions and waiting on that task, is a lock cycle (the reported
``psycopg.errors.DeadlockDetected`` under load).

This test does not reproduce a deadlock by timing. It pins the lock order
itself: a worker holds the task row; a cancel starts; the cancel transaction
must block on the task row *without* holding the task's execution rows. The
worker's own settle (an UPDATE on ``nexus_task_executions`` with a short
``lock_timeout``) then succeeds. Before the reorder, the cancel held the
execution rows and the worker's UPDATE failed with a lock timeout instead -
that is the regression this test catches.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def test_cancel_waits_for_tasks_without_holding_executions():
    _reset()
    store = PostgresStore(DSN)
    try:
        svc = ProjectService(store)
        lab = LabStore(store)
        project = str(svc.create("user:alice", "D")["id"])
        goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
        run = lab.start_run("user:alice", project, goal)
        run_id = str(run["id"])
        task = lab.add_task("user:alice", project, run_id,
                            task_key="a", title="A")
        task_id = str(task["id"])
        # An open execution for the task - what a worker holds while settling.
        with store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_task_executions(run_id, task_id, project_id, task_key, "
                "  cycle, attempt, kind, conversation_id) "
                "VALUES (%s,%s,%s,%s,0,1,'start','conv-1') RETURNING id",
                (run_id, task_id, project, "a"))
            execution_id = str(cur.fetchone()["id"])

        # A worker holds the task row, as the first statement of its settle does.
        worker = psycopg.connect(DSN, autocommit=False)
        try:
            worker.execute(
                "UPDATE nexus_run_tasks SET progress_marker='held' WHERE id=%s",
                (task_id,))
            assert worker.info.transaction_status is not None  # tx open, lock held

            outcome: dict = {}

            def _cancel() -> None:
                try:
                    outcome["run"] = lab.set_run_state(
                        "user:alice", project, run_id, "cancelled")
                except Exception as e:  # noqa: BLE001 - surfaced to the assertion
                    outcome["error"] = e

            t = threading.Thread(target=_cancel, name="cancel-probe", daemon=True)
            t.start()
            # Wait until the cancel transaction is blocked on a lock (or done).
            blocked = False
            deadline = time.time() + 15.0
            with psycopg.connect(DSN, autocommit=True) as admin:
                while time.time() < deadline:
                    row = admin.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE state = 'active' AND wait_event_type = 'Lock' "
                        "AND query LIKE 'UPDATE nexus_run_tasks%'").fetchone()
                    if row[0]:
                        blocked = True
                        break
                    if not t.is_alive():
                        break
                    time.sleep(0.05)
            assert blocked, "cancel did not block on the task row as expected"

            # The worker closes its execution. With the fixed order the cancel,
            # which is waiting on the task row, holds nothing the worker needs,
            # so this lands at once. Before the reorder the cancel already held
            # the execution row and this raised a lock timeout.
            with worker.cursor() as cur:
                # 200 ms: far below the default deadlock_timeout (1 s), so the
                # outcome is decided by lock order alone, never by which
                # transaction the deadlock detector happens to pick as victim.
                cur.execute("SET LOCAL lock_timeout = '200ms'")
                cur.execute(
                    "UPDATE nexus_task_executions SET ended_at=now(), outcome='test', "
                    "reason='worker settle' WHERE id=%s",
                    (execution_id,))
            worker.rollback()  # release the task lock; the cancel proceeds
            t.join(15.0)
        finally:
            worker.close()

        assert "error" not in outcome, outcome.get("error")
        assert outcome["run"]["state"] == "cancelled"
        # The execution the worker closed was rolled back by the worker; the
        # cancel then closed it for real (or it stays open - never lost).
        with store._tx() as cur:
            cur.execute("SELECT outcome, ended_at FROM nexus_task_executions WHERE id=%s",
                        (execution_id,))
            row = cur.fetchone()
            assert row is not None
            assert row["ended_at"] is not None
            assert row["outcome"] == "cancelled"
    finally:
        store.close()
