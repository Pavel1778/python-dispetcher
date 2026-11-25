"""PostgreSQL LISTEN/NOTIFY: wake a worker when there is work.

The two loops that advance autonomous runs (``App._run_lab`` and
``orchestration.lab_worker``) already have a wake-up primitive - a
``threading.Event`` that shortens the sleep. This module feeds that primitive
from PostgreSQL, so a run that is started, resumed or retried, in **this**
process or another, is picked up within milliseconds instead of up to one poll
interval later.

Design rules (see ``docs/RFC-001_EVENT_DRIVEN.md``):

* **The database stays authoritative.** A notification is a hint. The payload
  names the run, but the worker still reads the run from PostgreSQL. A missed
  notification is recovered by the poll backstop; it can never lose work.
* **Sent only on commit.** ``notify_new_work`` runs ``pg_notify`` inside the
  caller's transaction, so a rolled-back transaction notifies nobody.
* **The listener owns a dedicated connection.** ``LISTEN`` lives on a session;
  it cannot share a pooled connection, and a transaction-mode pooler cannot
  guarantee the same backend. A listener that cannot connect is logged once and
  left off; the loop polls as before.
* **Off unless asked for.** ``NEXUS_NOTIFY=1`` turns it on. A deployment that
  sets nothing keeps the exact previous behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Callable

import psycopg

from . import nexus_metrics

log = logging.getLogger("nexus_notify")

#: The channel every producer and consumer agrees on.
CHANNEL = "nexus_work"

#: A channel name is an SQL identifier. It is a constant here, but validate it
#: anyway so a future caller cannot pass one through.
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def enabled() -> bool:
    """Is the listener switched on for this process?"""
    return os.environ.get("NEXUS_NOTIFY", "").strip().lower() in (
        "1", "true", "yes", "on")


def _payload(kind: str, ident: str, reason: str) -> str:
    return json.dumps({"kind": kind, "id": str(ident or ""), "reason": reason})


def notify_new_work(store: Any, run_id: str = "", *, reason: str = "wake") -> bool:
    """Publish one wake-up. Best effort: never raises, returns whether sent.

    Runs inside ``store._tx()``, so the notification is delivered only if that
    transaction commits. Gated when ``NEXUS_NOTIFY`` is off, so an untouched
    deployment makes no extra database call.
    """
    if not enabled():
        return False
    try:
        with store._tx() as cur:
            cur.execute("SELECT pg_notify(%s, %s)",
                        (CHANNEL, _payload("run", run_id, reason)))
        nexus_metrics.notification_sent(reason)
        return True
    except Exception:  # noqa: BLE001 - a hint must never fail its caller
        log.debug("could not send NOTIFY", exc_info=True)
        return False


def parse_payload(raw: str | bytes | None) -> dict:
    """A notification payload as a dict. A non-JSON body is not an error."""
    if not raw:
        return {"kind": "unknown", "id": "", "reason": ""}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"kind": "unknown", "id": str(raw)[:200], "reason": ""}
    if not isinstance(data, dict):
        return {"kind": "unknown", "id": "", "reason": ""}
    return {"kind": str(data.get("kind") or "unknown"),
            "id": str(data.get("id") or ""),
            "reason": str(data.get("reason") or "")}


class NotificationListener:
    """A background thread that LISTENs and calls *callback* per notification.

    The callback receives the parsed payload (a dict). It must be quick: it is
    the worker's ``wake()``, not a pass over the runs.
    """

    def __init__(self, dsn: str, callback: Callable[[dict], None], *,
                 channel: str = CHANNEL, poll_seconds: float = 1.0,
                 reconnect_seconds: float = 2.0) -> None:
        if not _IDENT.match(channel):
            raise ValueError(f"invalid NOTIFY channel: {channel!r}")
        self.dsn = dsn
        self.callback = callback
        self.channel = channel
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.reconnect_seconds = max(0.1, float(reconnect_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        #: True once a LISTEN has succeeded at least once.
        self.available = False
        #: The last failure, for an operator; never a secret.
        self.last_error: str | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "NotificationListener":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nexus-notify", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 3.0) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def __enter__(self) -> "NotificationListener":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    # ------------------------------------------------------------------ body
    def _run(self) -> None:
        while not self._stop.is_set():
            conn = None
            try:
                conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=10)
                self._conn = conn
                with conn.cursor() as cur:
                    cur.execute(f"LISTEN {self.channel}")
                self.available = True
                self.last_error = None
                log.info("listening on %s for wake-ups", self.channel)
                while not self._stop.is_set():
                    for notify in conn.notifies(timeout=self.poll_seconds, stop_after=1):
                        payload = parse_payload(getattr(notify, "payload", None))
                        nexus_metrics.notification_received(payload["kind"])
                        try:
                            self.callback(payload)
                        except Exception:  # noqa: BLE001 - one bad callback is not fatal
                            log.exception("notification callback failed")
            except Exception as e:  # noqa: BLE001 - reconnect on the next round
                self.available = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("notification listener unavailable: %s", self.last_error)
            finally:
                self._conn = None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001 - already gone
                        pass
            if not self._stop.is_set():
                self._stop.wait(self.reconnect_seconds)
