"""PostgreSQL LISTEN/NOTIFY: the producer, the listener and the integration.

Everything here is a real round trip over a real PostgreSQL: a notification
that is committed is received, one that is rolled back is not, a listener that
cannot connect leaves the loop polling, and ``App.wake_lab`` - the one line
every start/resume/retry already calls - publishes a wake-up.
"""
from __future__ import annotations

import os
import sys
import time

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_notify  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_notify import (  # noqa: E402
    CHANNEL, NotificationListener, notify_new_work, parse_payload,
)
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run notification tests")


def _reset_schema() -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def listener():
    received: list[dict] = []
    item = NotificationListener(DSN, received.append, poll_seconds=0.1)
    item.start()
    assert _wait_for(lambda: item.available), "the listener never connected"
    yield item, received
    item.stop()


def test_parse_payload_accepts_json_bytes_and_junk():
    assert parse_payload('{"kind":"run","id":"x","reason":"started"}') == {
        "kind": "run", "id": "x", "reason": "started"}
    assert parse_payload(b'{"kind":"run","id":"y"}')["id"] == "y"
    assert parse_payload("not json")["kind"] == "unknown"
    assert parse_payload(None)["kind"] == "unknown"
    assert parse_payload("[1,2]")["kind"] == "unknown"


def test_a_committed_notification_is_received(listener):
    item, received = listener
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)",
                     (CHANNEL, '{"kind":"run","id":"abc","reason":"started"}'))
    assert _wait_for(lambda: received), "the listener did not receive the notification"
    assert received[0]["id"] == "abc"
    assert received[0]["kind"] == "run"


def test_a_rolled_back_notification_is_not_received(listener):
    item, received = listener
    with psycopg.connect(DSN) as conn:          # not autocommit: a real transaction
        conn.execute("SELECT pg_notify(%s, %s)",
                     (CHANNEL, '{"kind":"run","id":"ghost","reason":"rolled-back"}'))
        conn.rollback()
    time.sleep(1.0)
    assert received == []


def test_notify_new_work_publishes_through_the_store(listener, monkeypatch):
    item, received = listener
    monkeypatch.setenv("NEXUS_NOTIFY", "1")
    store = PostgresStore(DSN)
    try:
        assert notify_new_work(store, "run-1", reason="started") is True
    finally:
        store.close()
    assert _wait_for(lambda: received)
    assert received[0]["id"] == "run-1"
    assert received[0]["reason"] == "started"


def test_notify_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("NEXUS_NOTIFY", raising=False)
    assert nexus_notify.enabled() is False
    assert notify_new_work(object(), "x") is False


def test_a_listener_that_cannot_connect_does_not_raise():
    item = NotificationListener(
        "postgresql://nobody:nobody@127.0.0.1:1/none", lambda payload: None,
        poll_seconds=0.1, reconnect_seconds=0.1)
    item.start()
    try:
        assert _wait_for(lambda: item.last_error is not None, timeout=5.0)
        assert item.available is False
        assert "Error" in item.last_error or "refused" in item.last_error.lower()
    finally:
        item.stop()


def test_an_invalid_channel_is_refused():
    with pytest.raises(ValueError):
        NotificationListener(DSN, lambda payload: None, channel="bad channel; drop")


def test_wake_lab_publishes_the_wake_up(listener, monkeypatch):
    """The exact line every start/resume/retry calls wakes another process."""
    item, received = listener
    _reset_schema()
    monkeypatch.setenv("NEXUS_NOTIFY", "1")
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    with FakeCloudServer() as fake:
        app = App(DSN, poll_interval=0.2, token="t", lab_inline=False)
        app.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
        try:
            app.wake_lab()
        finally:
            app.store.close()
    assert _wait_for(lambda: received), "wake_lab did not publish a notification"
    assert received[0]["kind"] == "run"
