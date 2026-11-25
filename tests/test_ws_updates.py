"""WebSocket updates: the bus, the handshake, and a real event over a socket.

The server, the app and PostgreSQL are real. The client is a raw socket so the
test exercises the RFC 6455 handshake and frames, not a helper that could hide
a protocol bug.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_events  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.evidence_trust import (  # noqa: E402
    EvidenceTrustEngine, Keyring, generate_signer, public_key_b64,
)
from orchestration.nexus_evidence import EvidenceService  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "ws-token"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


# ------------------------------------------------------------------- pure part
def test_accept_key_matches_the_rfc6455_vector():
    assert nexus_events.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == \
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_a_full_queue_keeps_the_newest_events():
    bus = nexus_events.EventBus(max_subscribers=2, queue_size=2)
    q = bus.subscribe()
    for index in range(5):
        bus.publish("task.started", task=f"t{index}")
    kinds = []
    while not q.empty():
        kinds.append(json.loads(q.get_nowait())["task"])
    assert kinds == ["t3", "t4"]


def test_the_subscriber_cap_is_honest():
    bus = nexus_events.EventBus(max_subscribers=1, queue_size=1)
    first = bus.subscribe()
    assert first is not None
    assert bus.subscribe() is None
    bus.unsubscribe(first)
    assert bus.subscribe() is not None


def test_a_large_event_is_truncated_not_sent_whole():
    bus = nexus_events.EventBus(max_subscribers=1, queue_size=1)
    q = bus.subscribe()
    bus.publish("task.completed", note="x" * (nexus_events.MAX_EVENT_BYTES + 100))
    payload = json.loads(q.get_nowait())
    assert payload["truncated"] is True
    assert "note" not in payload


# ------------------------------------------------------------------ real server
pytestmark_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the WebSocket tests")


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    a = App(DSN, poll_interval=0.2, token=TOKEN)
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


def _handshake(base: str, *, token: str | None = TOKEN):
    parts = urlsplit(base)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        "GET /ws/updates HTTP/1.1",
        f"Host: {parts.hostname}:{parts.port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return sock, key, response


def _client_frame(opcode: int, payload: bytes = b"") -> bytes:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked


@pytestmark_db
def test_a_websocket_receives_a_published_event(base):
    sock, key, response = _handshake(base)
    try:
        head = response.decode("latin-1")
        assert "101" in head.split("\r\n", 1)[0]
        assert nexus_events.accept_key(key) in head
        nexus_events.BUS.publish("task.completed", task="t1",
                                 verdict="completed_verified", run="r1")
        opcode, payload = nexus_events.read_frame(sock)
        assert opcode == 0x1
        event = json.loads(payload)
        assert event["kind"] == "task.completed"
        assert event["task"] == "t1"
        assert event["verdict"] == "completed_verified"
    finally:
        sock.close()


@pytestmark_db
def test_an_unauthenticated_upgrade_is_refused(base):
    sock, _key, response = _handshake(base, token=None)
    try:
        assert b"401" in response.split(b"\r\n", 1)[0]
        assert b"101" not in response.split(b"\r\n", 1)[0]
    finally:
        sock.close()


@pytestmark_db
def test_a_ping_is_answered_with_a_pong(base):
    sock, _key, _response = _handshake(base)
    try:
        sock.sendall(_client_frame(0x9, b"hi"))
        opcode, payload = nexus_events.read_frame(sock)
        assert opcode == 0xA
        assert payload == b"hi"
    finally:
        sock.close()


@pytestmark_db
def test_a_close_frame_is_answered(base):
    sock, _key, _response = _handshake(base)
    try:
        sock.sendall(_client_frame(0x8, b"\x03\xe8"))
        opcode, _payload = nexus_events.read_frame(sock)
        assert opcode == 0x8
    finally:
        sock.close()


@pytestmark_db
def test_a_real_evidence_record_is_published(app):
    q = nexus_events.BUS.subscribe()
    assert q is not None
    try:
        signer = generate_signer("k1")
        keyring = Keyring()
        keyring.add("k1", public_key_b64(signer))
        service = EvidenceService(app.store, EvidenceTrustEngine(keyring, signer))
        service.record(subject_kind="manual", subject_id=None, project_id=None,
                       summary="ws test", created_by="test")
        event = json.loads(q.get(timeout=2))
        assert event["kind"] == "evidence.verified"
        assert event["verdict"] == "VERIFIED"
    finally:
        nexus_events.BUS.unsubscribe(q)


@pytestmark_db
def test_the_http_route_is_protected_without_an_upgrade(base):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(base + "/ws/updates", timeout=5)
    assert caught.value.code == 401
