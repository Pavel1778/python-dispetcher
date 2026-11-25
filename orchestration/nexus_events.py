"""In-process events and a minimal RFC 6455 server for ``/ws/updates``.

The studio already polls the run it is watching. This module lets the server
push the same facts instead: a task started, a task settled, a run failed, an
evidence record verified. The payloads are small and carry no secret - a task
key, a run id and a verdict, never a report, a prompt or a credential.

Design rules:

* **The bus is a hint, not a queue of record.** A subscriber that is slow or
  gone loses events; the database and the HTTP reads remain authoritative. A
  full subscriber queue drops its oldest event rather than growing without
  bound or blocking a worker.
* **In-process only.** A WebSocket client connected to this process sees the
  events published in this process. The default deployment advances runs in the
  web server's own loop, so that is the whole path; a separate worker's events
  would need Redis fan-out (not implemented).
* **No new dependency.** The handshake and frames are implemented here; the
  server is stdlib ``http.server``.
* **Authentication is the session cookie** (or a bearer token for a machine
  client). A browser cannot set an ``Authorization`` header on a WebSocket, so
  the cookie is the browser path; the same ``_require_read`` gate runs first.
"""
from __future__ import annotations

import base64
import hashlib
import json
import queue
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: RFC 6455 handshake constant.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: The most a subscriber queue holds before its oldest event is dropped.
QUEUE_SIZE = 256
#: The most concurrent WebSocket clients this process keeps.
MAX_SUBSCRIBERS = 200
#: The most one event may be, so a large value cannot fill a client's memory.
MAX_EVENT_BYTES = 16 * 1024


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_json(self) -> str:
        payload = {"kind": self.kind, "at": round(self.at, 3), **self.data}
        return json.dumps(payload, ensure_ascii=False, default=str)


class EventBus:
    """Fan out small JSON events to in-process subscribers."""

    def __init__(self, *, max_subscribers: int = MAX_SUBSCRIBERS,
                 queue_size: int = QUEUE_SIZE) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[str]] = set()
        self._max = max(1, int(max_subscribers))
        self._size = max(1, int(queue_size))

    def subscribe(self) -> queue.Queue[str] | None:
        """A queue of JSON strings, or ``None`` when the client cap is reached."""
        q: queue.Queue[str] = queue.Queue(maxsize=self._size)
        with self._lock:
            if len(self._subscribers) >= self._max:
                return None
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, kind: str, **data: Any) -> int:
        """Publish one event; returns how many subscribers it reached."""
        event = Event(kind=kind, data={k: v for k, v in data.items() if v is not None})
        payload = event.as_json()
        if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
            payload = json.dumps({"kind": kind, "at": round(event.at, 3),
                                  "truncated": True})
        with self._lock:
            subscribers = tuple(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Drop the oldest, keep the newest: a live view wants the latest.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass
        return len(subscribers)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)


#: The process-wide bus. Producers import this and call ``publish``.
BUS = EventBus()


# ------------------------------------------------------------------ websocket
def accept_key(key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value for a client's key."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_text(text: str) -> bytes:
    """A server-to-client text frame (unmasked, as the server must send)."""
    raw = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(raw)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + raw


def encode_control(opcode: int, payload: bytes = b"") -> bytes:
    """A close (0x8) or pong (0xA) frame."""
    return bytes([0x80 | opcode, len(payload)]) + payload


def encode_close(code: int = 1000, reason: str = "") -> bytes:
    raw = reason.encode("utf-8")[:120]
    return encode_control(0x8, struct.pack("!H", code) + raw)


class FrameError(Exception):
    """The peer sent something that is not a valid WebSocket frame."""


def read_frame(sock: Any, *, max_bytes: int = MAX_EVENT_BYTES) -> tuple[int, bytes]:
    """Read one frame. Returns ``(opcode, payload)``; raises ``FrameError``.

    The server must reject an oversized frame before buffering it. Client
    frames are masked (RFC 6455); an unmasked one is accepted for a simple
    machine client but is not trusted to be a browser.
    """
    def recv(size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                raise FrameError("connection closed")
            buf += chunk
        return buf

    first = recv(2)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv(8))[0]
    if length > max_bytes:
        raise FrameError("frame too large")
    mask = recv(4) if masked else b"\x00\x00\x00\x00"
    payload = recv(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload
