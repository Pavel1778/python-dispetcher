"""Optional Redis fan-out for the in-process event bus (stdlib only).

The WebSocket endpoint (`/ws/updates`) fans out events inside one process. A
deployment with a separate lab worker, or several web processes, needs those
events to cross a process boundary. This module adds that, without a client
library:

* `RedisConnection` is a small RESP2 client (AUTH/SELECT/PUBLISH/SUBSCRIBE) over
  a socket. It is deliberately narrow: it is not a general Redis client.
* `RedisBus` bridges one `EventBus` to a Redis channel. A local `publish` is
  also sent to Redis; a message from another process is fanned out locally with
  `publish_remote` and is **never re-forwarded**, so there is no loop.
* Each bridge tags its messages with an origin id and ignores its own echo, so a
  process does not deliver its own event twice.

Rules:

* **Off unless `REDIS_URL` is set.** With no URL, the bus stays local and the
  deployment behaves exactly as before.
* **Redis is a transport, never a record.** A message is a hint; the database
  and the HTTP reads remain authoritative. If Redis is down, local events still
  work and the bridge reconnects with backoff.
* **No secret is logged.** The URL (which may contain a password) is never
  formatted into a log line or an exception; `repr` carries the channel only.
* **Pub/Sub, not Streams.** These events are ephemeral hints; a Stream would
  add durable replay nobody consumes. A Stream can be added if replay is ever
  needed.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger("nexus_redis")

#: The channel every bridge publishes to and listens on.
CHANNEL = "nexus:events"
#: How long one socket operation may take before it is retried.
DEFAULT_TIMEOUT = 2.0
#: The subscriber's read timeout, so a stop is noticed within about a second.
SUBSCRIBER_TIMEOUT = 1.0
#: Events waiting to be published. A full outbox drops the oldest: a hint.
OUTBOX_SIZE = 1024


class RedisError(RuntimeError):
    """A Redis command failed or the connection broke."""


def redis_url() -> str | None:
    """The configured URL, or ``None``. Never log the value."""
    url = (os.environ.get("REDIS_URL") or os.environ.get("NEXUS_REDIS_URL") or "").strip()
    return url or None


def parse_url(url: str) -> dict[str, Any]:
    """``redis://[user:pass@]host:port/db`` -> connection settings.

    ``rediss://`` is refused: this client has no TLS. A password is returned as
    data, never in an exception message.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("redis",):
        raise ValueError("redis URL must start with redis:// (TLS rediss:// is not supported)")
    try:
        port = parts.port or 6379
    except ValueError as e:
        raise ValueError("redis URL has an invalid port") from e
    database = 0
    path = (parts.path or "").lstrip("/")
    if path:
        try:
            database = int(path)
        except ValueError as e:
            raise ValueError("redis URL database must be an integer") from e
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": port,
        "username": unquote(parts.username) if parts.username else None,
        "password": unquote(parts.password) if parts.password else None,
        "db": database,
    }


class RedisConnection:
    """A narrow RESP2 client: connect, one command, read one reply."""

    def __init__(self, *, host: str, port: int, username: str | None = None,
                 password: str | None = None, db: int = 0,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.db = db
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file: Any = None

    def __repr__(self) -> str:  # never the password
        return f"RedisConnection(host={self.host!r}, port={self.port}, db={self.db})"

    def connect(self) -> "RedisConnection":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._file = self._sock.makefile("rb")
        if self.password:
            if self.username:
                self.command("AUTH", self.username, self.password)
            else:
                self.command("AUTH", self.password)
        if self.db:
            self.command("SELECT", str(self.db))
        return self

    def set_timeout(self, seconds: float) -> None:
        self.timeout = float(seconds)
        if self._sock is not None:
            self._sock.settimeout(self.timeout)

    def close(self) -> None:
        for closer in (getattr(self._file, "close", None),
                       getattr(self._sock, "close", None)):
            try:
                if closer:
                    closer()
            except OSError:
                pass
        self._sock = None
        self._file = None

    def command(self, *args: Any) -> Any:
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        self._sock.sendall(self._encode(args))
        return self.read_reply()

    @staticmethod
    def _encode(args: tuple[Any, ...]) -> bytes:
        out = [f"*{len(args)}\r\n".encode()]
        for arg in args:
            raw = arg if isinstance(arg, bytes) else str(arg).encode()
            out.append(f"${len(raw)}\r\n".encode())
            out.append(raw)
            out.append(b"\r\n")
        return b"".join(out)

    def read_reply(self) -> Any:
        if self._file is None:
            raise RedisError("not connected")
        line = self._file.readline()
        if not line:
            raise RedisError("connection closed")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            raise RedisError(rest.decode("utf-8", "replace")[:200])
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            if size == -1:
                return None
            data = self._file.read(size + 2)
            return data[:-2]
        if kind == b"*":
            count = int(rest)
            if count == -1:
                return None
            return [self.read_reply() for _ in range(count)]
        raise RedisError("unexpected Redis reply")


class RedisBus:
    """Bridge one ``EventBus`` to Redis, in both directions."""

    def __init__(self, url: str, bus: Any, *, channel: str = CHANNEL,
                 subscribe: bool = True) -> None:
        self._settings = parse_url(url)
        self.bus = bus
        self.channel = channel
        self.subscribe = bool(subscribe)
        self._origin = uuid.uuid4().hex
        self._forwarder = self._forward
        #: The producer never blocks on Redis: it enqueues, and this thread
        #: publishes. A full outbox drops the oldest event (a hint).
        self._outbox: queue.Queue[str | None] = queue.Queue(maxsize=OUTBOX_SIZE)
        self._publisher_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set once the subscriber has actually issued SUBSCRIBE.
        self.ready = threading.Event()
        #: The last failure's type, for an operator; never a secret.
        self.last_error: str | None = None

    def __repr__(self) -> str:  # never the URL
        return f"RedisBus(channel={self.channel!r}, subscribe={self.subscribe})"

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "RedisBus":
        self.bus.set_forwarder(self._forwarder)
        self._publisher_thread = threading.Thread(
            target=self._publish_loop, name="nexus-redis-pub", daemon=True)
        self._publisher_thread.start()
        if self.subscribe:
            self._thread = threading.Thread(target=self._listen, name="nexus-redis", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.ready.clear()
        self.bus.clear_forwarder(self._forwarder)
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        for thread in (self._publisher_thread, self._thread):
            if thread is not None:
                thread.join(timeout=3)
        self._publisher_thread = None
        self._thread = None

    def __enter__(self) -> "RedisBus":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    # --------------------------------------------------------------- publishing
    def _forward(self, payload: str) -> None:
        """Enqueue an event for Redis. Never blocks the producer."""
        try:
            self._outbox.put_nowait(payload)
        except queue.Full:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                pass
            try:
                self._outbox.put_nowait(payload)
            except queue.Full:
                pass

    def _publish_loop(self) -> None:
        conn: RedisConnection | None = None
        try:
            while not self._stop.is_set():
                try:
                    item = self._outbox.get(timeout=1.0)
                except queue.Empty:
                    continue
                if item is None:
                    break
                envelope = json.dumps({"origin": self._origin, "payload": item})
                for attempt in (1, 2):
                    try:
                        if conn is None:
                            conn = RedisConnection(**self._settings).connect()
                        conn.command("PUBLISH", self.channel, envelope)
                        self.last_error = None
                        break
                    except Exception as e:  # noqa: BLE001 - a hint must not block
                        self.last_error = type(e).__name__
                        if conn is not None:
                            conn.close()
                            conn = None
                        if attempt == 2:
                            break       # drop this hint; local delivery already happened
        finally:
            if conn is not None:
                conn.close()

    # --------------------------------------------------------------- receiving
    def _listen(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            conn = None
            try:
                conn = RedisConnection(**self._settings,
                                       timeout=SUBSCRIBER_TIMEOUT).connect()
                self.ready.clear()
                conn.command("SUBSCRIBE", self.channel)
                self.ready.set()
                self.last_error = None
                while not self._stop.is_set():
                    try:
                        reply = conn.read_reply()
                    except TimeoutError:
                        continue
                    if (isinstance(reply, list) and len(reply) == 3
                            and reply[0] == b"message"):
                        self._receive(reply[2])
            except Exception as e:  # noqa: BLE001 - reconnect on the next round
                self.last_error = type(e).__name__
                log.warning("redis event bridge unavailable (%s); local events continue",
                            self.last_error)
            finally:
                if conn is not None:
                    conn.close()
            if not self._stop.is_set():
                self._stop.wait(delay)
                delay = min(delay * 2, 30.0)

    def _receive(self, raw: bytes | str) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(envelope, dict):
            return
        if envelope.get("origin") == self._origin:
            return                      # our own echo; it was already delivered locally
        payload = envelope.get("payload")
        if isinstance(payload, str):
            self.bus.publish_remote(payload)


#: One bridge per bus, refcounted, so several components in one process share it.
_REGISTRY: dict[int, list[Any]] = {}
_REGISTRY_LOCK = threading.Lock()


def install(bus: Any, *, subscribe: bool = True) -> RedisBus | None:
    """Start (or reuse) the bridge for *bus*. ``None`` when no URL is set."""
    url = redis_url()
    if not url:
        return None
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(id(bus))
        if entry is not None:
            entry[1] += 1
            return entry[0]
        try:
            bridge = RedisBus(url, bus, subscribe=subscribe).start()
        except Exception:  # noqa: BLE001 - an optional bridge must not stop boot
            log.warning("could not start the Redis event bridge", exc_info=True)
            return None
        _REGISTRY[id(bus)] = [bridge, 1]
        return bridge


def uninstall(bridge: RedisBus | None) -> None:
    """Release one reference; the last one stops the bridge."""
    if bridge is None:
        return
    key = id(bridge.bus)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del _REGISTRY[key]
    bridge.stop()
