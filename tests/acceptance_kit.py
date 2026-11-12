"""Shared helpers for the acceptance suite (sections A-G of the task text).

Nothing here mocks a client. The servers are real HTTP servers on a loopback
socket, the store is real PostgreSQL, and the code under test makes real
requests over a socket. The helpers only *configure* a server's behaviour
(answer, refuse, delay, drop) so a failure mode can be produced on purpose and
deterministically.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def reset_schema(dsn: str) -> None:
    """Drop and re-apply the whole schema, so a test starts from nothing.

    Every file in ``migrations/`` is applied in lexical order, the way the
    runner does it, so a new migration cannot silently go unapplied.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(MIGRATIONS)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(MIGRATIONS, name)).read())


class ConfigurableCloud:
    """Mutable state backing :class:`ConfigurableCloudServer`."""

    def __init__(self) -> None:
        #: When set, only this key is accepted; any other is a 401.
        self.required_key: str | None = None
        #: When non-zero, every request answers with this status instead.
        self.status = 0
        #: Seconds to sleep before answering (to force a client timeout).
        self.delay = 0.0
        #: Close the connection without answering, for every request.
        self.drop = False
        #: Close the connection without answering this many times, then answer.
        self.drop_times = 0
        #: The conversation count the ``count`` endpoint reports.
        self.count = 0
        #: Every request the server saw: ``(method, path, authorization)``.
        self.requests: list[tuple[str, str, str | None]] = []
        self.settings: dict = {
            "llm_api_key_set": True,
            "active_agent_profile_id": "profile-1",
            "agent_settings": {"mcp_config": {}},
            "registered_marketplaces": [],
            "inherited_marketplaces": [],
            "disabled_skills": [],
            "provider_tokens_set": {},
        }


class _ConfigurableHandler(BaseHTTPRequestHandler):
    server_version = "ConfigurableCloud/1.0"

    def log_message(self, *args):  # noqa: ANN002 - silence the test log
        pass

    @property
    def cloud(self) -> ConfigurableCloud:
        return self.server.cloud  # type: ignore[attr-defined]

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        cloud = self.cloud
        cloud.requests.append(
            (self.command, self.path, self.headers.get("Authorization")))
        if cloud.drop or cloud.drop_times > 0:
            if cloud.drop_times > 0:
                cloud.drop_times -= 1
            self.close_connection = True
            return
        if cloud.delay:
            time.sleep(cloud.delay)
        if cloud.required_key is not None:
            if self.headers.get("Authorization") != f"Bearer {cloud.required_key}":
                return self._json(401, {"detail": "unauthorized"})
        if cloud.status:
            return self._json(cloud.status, {"detail": "forced"})
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/v1/app-conversations/count":
            return self._json(200, {"count": cloud.count})
        if path == "/api/v1/settings":
            return self._json(200, cloud.settings)
        if path == "/api/agent-profiles":
            return self._json(200, {"profiles": [], "active_agent_profile_id": None})
        return self._json(404, {"detail": "not found"})

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class ConfigurableCloudServer:
    """A real HTTP server whose answers a test can steer.

    It speaks the small part of the Cloud V1 surface the connectivity code
    uses (``count`` for health/ping, ``settings`` and ``agent-profiles`` for
    ``account_capabilities``), and can be told to refuse, delay or drop.
    """

    def __init__(self) -> None:
        self.cloud = ConfigurableCloud()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ConfigurableHandler)
        self._server.cloud = self.cloud  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "ConfigurableCloudServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


class RedirectServer:
    """A real HTTP server that answers every request with a 302.

    The target is a second listener that records the ``Authorization`` header it
    receives, so a test can prove the key was *not* forwarded.
    """

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        sink = self

        class _Sink(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ANN002
                pass

            def do_GET(self):  # noqa: N802
                sink.seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self._sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
        sink_port = self._sink.server_address[1]

        class _Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ANN002
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink_port}/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._redirect = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
        self._threads = [
            threading.Thread(target=self._sink.serve_forever, daemon=True),
            threading.Thread(target=self._redirect.serve_forever, daemon=True),
        ]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._redirect.server_address[1]}"

    def __enter__(self) -> "RedirectServer":
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc) -> None:
        for server in (self._redirect, self._sink):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
