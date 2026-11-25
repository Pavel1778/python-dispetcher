"""Optional PostgreSQL wakeup hints; the database remains the durable queue.

NEXUS_LISTEN_DSN must point to the same database through a direct or session
pooled connection. Transaction-mode poolers cannot preserve LISTEN. The caller
continues polling even while disconnected or when notifications are lost.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import psycopg

log = logging.getLogger(__name__)

# Coalesce bursts, including transient-failure requeues caused by the worker
# itself. Without a cap, immediate wakeups can turn retries into a hot loop.
MIN_WAKE_INTERVAL = 0.5


class QueueWakeup:
    """Listen on a dedicated connection, reconnecting without blocking workers."""

    def __init__(self, dsn: str, callbacks: dict[str, Callable[[], None]]):
        self._dsn = dsn
        self._callbacks = callbacks
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nexus-queue-listener", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _wake_all(self) -> None:
        # A reconnect could have missed events; force a rescan after LISTEN.
        for callback in self._callbacks.values():
            callback()

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                with psycopg.connect(self._dsn, autocommit=True, connect_timeout=2,
                                     application_name="nexus_queue_listener") as conn:
                    conn.execute("LISTEN nexus_queue_wakeup")
                    self._wake_all()
                    delay = 1.0
                    pending: set[str] = set()
                    last_sent: dict[str, float] = {}
                    while not self._stop.is_set():
                        now = time.monotonic()
                        for channel in tuple(pending):
                            if now - last_sent.get(channel, 0.0) >= MIN_WAKE_INTERVAL:
                                pending.remove(channel)
                                last_sent[channel] = now
                                self._callbacks[channel]()
                        timeout = 1.0
                        if pending:
                            timeout = min(timeout, max(0.001, min(
                                last_sent.get(channel, 0.0) + MIN_WAKE_INTERVAL - time.monotonic()
                                for channel in pending)))
                        for notification in conn.notifies(timeout=timeout, stop_after=1):
                            if notification.payload in self._callbacks:
                                pending.add(notification.payload)
            except Exception as exc:  # noqa: BLE001 - polling remains available
                # Do not log DSN or exception text, which may contain credentials.
                log.warning("queue listener unavailable (%s); using polling", type(exc).__name__)
                self._stop.wait(delay)
                delay = min(delay * 2, 30.0)


def from_environment(callbacks: dict[str, Callable[[], None]]) -> QueueWakeup | None:
    """Opt in only when the operator supplies a session-capable DSN."""
    dsn = os.environ.get("NEXUS_LISTEN_DSN", "").strip()
    return QueueWakeup(dsn, callbacks) if dsn else None
