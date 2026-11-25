"""Redis fan-out: the RESP client, the bridge, and two buses over a real Redis.

`redis-server` is started on a free port for these tests. When it is not
installed the module skips cleanly; a bridge that cannot connect is still
covered by a local-only test, so the "Redis down" behaviour is tested even
without the server.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_events import EventBus  # noqa: E402
from orchestration.nexus_redis import (  # noqa: E402
    RedisBus, RedisConnection, install, parse_url, uninstall,
)


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture(scope="module")
def redis_url():
    """A real Redis: TEST_REDIS_URL (a CI service) or a local redis-server."""
    external = (os.environ.get("TEST_REDIS_URL") or "").strip()
    if external:
        conn = RedisConnection(**parse_url(external)).connect()
        try:
            assert conn.command("PING") == "PONG"
        finally:
            conn.close()
        yield external
        return
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server is not installed and TEST_REDIS_URL is unset")
    port = _free_port()
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--bind", "127.0.0.1",
         "--save", "", "--appendonly", "no", "--protected-mode", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}/0"
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            conn = RedisConnection(**parse_url(url)).connect()
            if conn.command("PING") == "PONG":
                conn.close()
                break
        except Exception:  # noqa: BLE001 - the server is still starting
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.skip("redis-server did not start")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _drain(q: queue.Queue[str]) -> list[dict]:
    out = []
    while True:
        try:
            out.append(json.loads(q.get_nowait()))
        except queue.Empty:
            return out


# ------------------------------------------------------------------- pure part
def test_parse_url_and_repr_do_not_leak_the_password():
    settings = parse_url("redis://user:secret@example.com:6380/2")
    assert settings == {"host": "example.com", "port": 6380,
                        "username": "user", "password": "secret", "db": 2}
    assert "secret" not in repr(RedisConnection(**settings))
    assert "secret" not in repr(RedisBus("redis://user:secret@example.com:6380/2",
                                         EventBus()))
    with pytest.raises(ValueError):
        parse_url("rediss://example.com:6380")


def test_no_url_means_no_bridge(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("NEXUS_REDIS_URL", raising=False)
    assert install(EventBus()) is None


def test_local_events_survive_redis_being_down():
    bus = EventBus()
    q = bus.subscribe()
    bridge = RedisBus("redis://127.0.0.1:1/0", bus, subscribe=False).start()
    try:
        bus.publish("task.completed", task="t3")
        assert json.loads(q.get(timeout=1))["task"] == "t3"
        assert _wait_for(lambda: bridge.last_error is not None)
    finally:
        bridge.stop()


# ----------------------------------------------------------------- real Redis
def test_the_resp_client_round_trips(redis_url):
    conn = RedisConnection(**parse_url(redis_url)).connect()
    try:
        assert conn.command("PING") == "PONG"
        assert conn.command("SET", "nexus:test", "v") == "OK"
        assert conn.command("GET", "nexus:test") == b"v"
    finally:
        conn.close()


def test_two_buses_fan_out_once_without_an_echo(redis_url):
    a, b = EventBus(), EventBus()
    qa, qb = a.subscribe(), b.subscribe()
    bridge_a = RedisBus(redis_url, a).start()
    bridge_b = RedisBus(redis_url, b).start()
    try:
        assert bridge_a.ready.wait(3), "bridge A did not subscribe"
        assert bridge_b.ready.wait(3), "bridge B did not subscribe"
        a.publish("task.completed", task="t1", verdict="completed_verified")
        assert _wait_for(lambda: not qb.empty()), "B did not receive the event"
        received = json.loads(qb.get(timeout=1))
        assert received["kind"] == "task.completed"
        assert received["task"] == "t1"
        # A gets its own event from the local fan-out, not again from Redis.
        first = json.loads(qa.get(timeout=1))
        assert first["task"] == "t1"
        time.sleep(0.5)
        assert qa.empty(), "the publisher received its own Redis echo"
    finally:
        bridge_a.stop()
        bridge_b.stop()


def test_a_publisher_only_bridge_reaches_a_subscriber(redis_url):
    worker_bus, web_bus = EventBus(), EventBus()
    q = web_bus.subscribe()
    web = RedisBus(redis_url, web_bus).start()
    worker = RedisBus(redis_url, worker_bus, subscribe=False).start()
    try:
        assert web.ready.wait(3)
        worker_bus.publish("task.started", worker="lab-1", task="t2")
        assert _wait_for(lambda: not q.empty()), "the web bus did not receive it"
        event = json.loads(q.get(timeout=1))
        assert event["kind"] == "task.started"
        assert event["worker"] == "lab-1"
    finally:
        worker.stop()
        web.stop()


def test_install_is_shared_and_refcounted(redis_url, monkeypatch):
    monkeypatch.setenv("REDIS_URL", redis_url)
    bus = EventBus()
    first = install(bus)
    second = install(bus)
    assert first is second
    assert bus.forwarding is True
    uninstall(first)
    assert bus.forwarding is True, "the second reference keeps the bridge alive"
    uninstall(second)
    assert bus.forwarding is False


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(os.environ["TEST_SUPABASE_DB_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            migrations = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "migrations")
            for name in sorted(os.listdir(migrations)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(migrations, name)).read())


def test_the_web_app_installs_and_removes_the_bridge(redis_url, monkeypatch):
    """The one line in ``App.start``/``App.stop`` is the actual wiring."""
    if not os.environ.get("TEST_SUPABASE_DB_URL"):
        pytest.skip("set TEST_SUPABASE_DB_URL to run the App wiring test")
    from orchestration import nexus_events
    from orchestration.cloud_client import CloudClient
    from orchestration.web_auth import hash_password
    from orchestration.web_server import App
    from tests.fake_cloud import FakeCloudServer

    _reset_schema()
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    with FakeCloudServer() as fake:
        app = App(os.environ["TEST_SUPABASE_DB_URL"], poll_interval=0.2, token="t")
        app.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
        app.start()
        try:
            assert app._redis_bridge is not None
            assert nexus_events.BUS.forwarding is True
        finally:
            app.stop()
    assert nexus_events.BUS.forwarding is False
