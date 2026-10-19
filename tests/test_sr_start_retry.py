"""A workflow must not stay 'queued' forever when Sr cannot start.

Requirement: a transient Cloud failure while starting Sr (429 while the account
is rate limited, a 5xx, a dropped connection) must not leave a workflow silently
queued with no Sr conversation and nothing to retry it.

The web server starts Sr inline on workflow creation. If that call fails, the
workflow row exists but has no ``sr_conversation_id``. Before this fix the
background loop skipped exactly those workflows (it needs an
``sr_conversation_id`` to service an Sr), so the workflow sat in ``queued``
forever and an operator had to delete and recreate it.

These tests run the real web server and the real SrRuntime against the fake
Cloud over real sockets and a real PostgreSQL.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run Sr start retry tests"
)

_MIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sessions, login_attempts, sr_tool_calls, "
                "task_events, conversations, tasks, repositories, workflows CASCADE"
            )
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    a = App(DSN, poll_interval=0.2, token="test-token")
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=5.0)
    a.dispatcher.client = a.client
    a.runtime.client = a.client
    a.verifier.client = a.client
    # No wall-clock waiting between retries: the test drives ticks itself.
    a.runtime.sr_start_backoff_base = 0.0
    yield a
    a.store.close()


@pytest.fixture
def server(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield ws
    ws.stop()


def test_transient_429_does_not_leave_the_workflow_queued(app, fake):
    """A 429 on the inline start is retried by the loop and recovers."""
    fake.cloud.start_status = 429
    fake.cloud.start_status_times = 1

    # The inline start fails, as it does in production.
    with pytest.raises(Exception):
        app.start_workflow("ship it")

    rows = app.store.list_workflows()
    assert len(rows) == 1
    wf = rows[0]["id"]
    assert rows[0]["state"] == "queued"
    assert app.store.sr_for(wf)["sr_conversation_id"] is None

    # The background loop must pick it up rather than leave it queued forever.
    # The single injected 429 is spent, so this pass starts Sr for real.
    report = app.runtime.tick()
    assert [r["workflow"] for r in report.started] == [wf]
    assert report.started[0]["recovered"] is True
    assert app.store.sr_for(wf)["sr_conversation_id"]
    # It is no longer a pending start, so no further retry is scheduled.
    assert app.store.pending_sr_starts() == []


def test_repeated_failure_blocks_with_evidence_instead_of_looping(app, fake):
    """Permanent trouble must end visible, not retry forever."""
    fake.cloud.start_status = 429
    fake.cloud.start_status_times = 99
    app.runtime.sr_start_max_attempts = 3

    with pytest.raises(Exception):
        app.start_workflow("ship it")

    wf = app.store.list_workflows()[0]["id"]
    for _ in range(3):
        app.runtime.tick()

    wf_row = app.store.sr_for(wf)
    assert wf_row["state"] == "blocked"
    assert wf_row["sr_last_error"]
    assert wf_row["sr_attempt"] >= 3


def test_retry_backoff_is_persisted(app, fake):
    """A retry scheduled in the future must not be attempted again at once."""
    fake.cloud.start_status = 500
    fake.cloud.start_status_times = 99

    with pytest.raises(Exception):
        app.start_workflow("ship it")

    wf = app.store.list_workflows()[0]["id"]
    app.runtime.sr_start_backoff_base = 300.0

    first = app.runtime.tick()
    assert [r["workflow"] for r in first.retried] == [wf]

    # Backoff has not elapsed, so the workflow is not due yet.
    second = app.runtime.tick()
    assert second.retried == []
    assert app.store.sr_for(wf)["state"] == "queued"


def test_a_bad_key_blocks_immediately_without_burning_attempts(app, fake):
    """A 401 is permanent: retrying wastes quota and hides the real problem."""
    fake.cloud.start_status = 401
    fake.cloud.start_status_times = 99

    with pytest.raises(Exception):
        app.start_workflow("ship it")

    wf = app.store.list_workflows()[0]["id"]
    report = app.runtime.tick()

    assert report.retry_blocked == [wf]
    wf_row = app.store.sr_for(wf)
    assert wf_row["state"] == "blocked"
    assert "auth" in (wf_row["sr_last_error"] or "").lower()


def test_sr_start_retry_never_duplicates_an_existing_sr(app, fake):
    """The retry pass must be a no-op once an Sr conversation exists."""
    app.start_workflow("ship it")
    wf = app.store.list_workflows()[0]["id"]
    before = app.store.sr_for(wf)["sr_conversation_id"]
    assert before

    report = app.runtime.tick()

    assert report.started == []
    assert report.retried == []
    assert app.store.sr_for(wf)["sr_conversation_id"] == before
