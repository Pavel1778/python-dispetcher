"""Stage timing and the observability surface.

No database for the tracer itself (it is pure); a real PostgreSQL for the queue
snapshot and stuck-task view, because those are SQL.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.tracing import Span, Tracer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")


# ------------------------------------------------------------------- tracer
def test_a_disabled_tracer_records_nothing():
    tracer = Tracer(enabled=False)
    with tracer.span("dispatch", project_id="p", task_id="t") as span:
        assert span is None
    assert tracer.recent() == []
    assert tracer.summary()["spans"] == 0


def test_a_span_carries_the_ids_and_a_duration():
    tracer = Tracer(enabled=True)
    with tracer.span("poll", project_id="p1", task_id="t1", conversation_id="c1",
                     attempt_id="c0a1") as span:
        span.status = "ok"
    (record,) = tracer.recent()
    assert record["stage"] == "poll"
    assert record["project_id"] == "p1" and record["task_id"] == "t1"
    assert record["conversation_id"] == "c1" and record["attempt_id"] == "c0a1"
    assert record["duration_ms"] >= 0 and record["status"] == "ok"


def test_an_error_is_recorded_and_re_raised():
    tracer = Tracer(enabled=True)
    with pytest.raises(ValueError):
        with tracer.span("dispatch", task_id="t"):
            raise ValueError("boom")
    (record,) = tracer.recent()
    assert record["status"] == "error" and "ValueError" in record["error"]


def test_an_error_message_carries_no_environment_value(monkeypatch):
    """The record keeps the type and a bounded message; it never reads an env
    value, and a long message is cut."""
    tracer = Tracer(enabled=True)
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    try:
        with tracer.span("verify", task_id="t"):
            raise RuntimeError("x" * 5000)
    except RuntimeError:
        pass
    (record,) = tracer.recent()
    assert len(record["error"]) <= 300
    assert "do-not-leak" not in record["error"]


def test_the_summary_reports_percentiles_per_stage():
    tracer = Tracer(enabled=True)
    for stage, count in (("dispatch", 3), ("poll", 5)):
        for i in range(count):
            tracer.record(Span(stage=stage, started_at=1000.0 + i,
                               finished_at=1000.0 + i + 0.01))
    summary = tracer.summary()
    assert summary["stages"]["dispatch"]["count"] == 3
    assert summary["stages"]["poll"]["count"] == 5
    assert "p95_ms" in summary["stages"]["dispatch"]


# --------------------------------------------------------------- the route
@needs_db
def test_observability_endpoint_reports_the_queue_without_a_secret():
    from tests.test_studio_api import studio_stack

    class _MP:
        def setenv(self, *a, **k):
            os.environ[a[0]] = a[1]

        def delenv(self, *a, **k):
            os.environ.pop(a[0], None)

    with studio_stack(_MP()) as studio:
        status, body = studio.api.get("/api/observability")
        assert status == 200
        assert "queue" in body and "workers" in body and "latency" in body
        assert body["queue"]["pending"] == 0
        assert body["stuck_tasks"] == []
        # No secret anywhere in the answer.
        text = str(body)
        assert "cloud-key-value" not in text and "studio-tests-secret-key" not in text


@needs_db
def test_a_stuck_task_is_reported_with_its_reason():
    import psycopg

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "migrations")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())
    store = PostgresStore(DSN)
    try:
        projects = ProjectService(store)
        project = str(projects.create("machine:x", "P")["id"])
        goal = str(projects.create_goal("machine:x", project, "g")[0]["id"])
        lab = LabStore(store)
        run = lab.start_run("machine:x", project, goal, policy="durable")
        lab.add_task("machine:x", project, run["id"], task_key="t", title="T",
                     repository="", branch="")
        # Make it look like it has waited: backdate its creation.
        with store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET created_at = now() - interval '20 min' "
                        "WHERE run_id=%s", (run["id"],))
        snapshot = lab.queue_snapshot()
        assert snapshot["pending"] == 1 and snapshot["claimable_now"] == 1
        assert snapshot["oldest_claimable_seconds"] >= 1200
        stuck = lab.stuck_tasks(min_age_seconds=300)
        assert len(stuck) == 1 and stuck[0]["task_key"] == "t"
        assert stuck[0]["run_state"] == "running"
    finally:
        store.close()
