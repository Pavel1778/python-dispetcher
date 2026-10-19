"""A real local HTTP server that speaks the GitHub REST surface we consume.

Tests drive :class:`orchestration.github_client.GitHubClient` over real sockets
against this server, so the client's request building, auth header, pagination
and error mapping are all exercised for real. It is not a mock of the client.

Only ``GET /user/repos`` is implemented: the dispatcher reads repositories and
writes nothing to GitHub.
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitHub:
    """Mutable state backing the fake server."""

    def __init__(self, repos: list[dict] | None = None):
        self.repos: list[dict] = repos if repos is not None else [
            {"full_name": "Pavel1778/python-dispetcher", "default_branch": "main",
             "private": True, "archived": False, "pushed_at": "2026-01-03T00:00:00Z"},
            {"full_name": "Pavel1778/Klyrshift", "default_branch": "main",
             "private": False, "archived": False, "pushed_at": "2026-01-02T00:00:00Z"},
            {"full_name": "Pavel1778/old-archive", "default_branch": "master",
             "private": False, "archived": True, "pushed_at": "2025-01-01T00:00:00Z"},
        ]
        #: When set, every request answers with this status instead.
        self.force_status = 0
        #: Sent as X-RateLimit-Remaining when force_status is 403.
        self.rate_remaining: str | None = None
        #: The Authorization header values seen, in order. Lets a test assert the
        #: token is sent without ever putting the token in an assertion message.
        self.auth_headers: list[str | None] = []
        self.paths: list[str] = []
        #: Page size used to emulate pagination; 0 disables paging.
        self.page_size = 0


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeGitHub/1.0"

    def log_message(self, *args):  # noqa: A003 - silence the test log
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        gh: FakeGitHub = self.server.gh  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        gh.paths.append(parsed.path)
        gh.auth_headers.append(self.headers.get("Authorization"))

        if gh.force_status:
            body = b'{"message": "forced"}'
            self.send_response(gh.force_status)
            self.send_header("Content-Type", "application/json")
            if gh.rate_remaining is not None:
                self.send_header("X-RateLimit-Remaining", gh.rate_remaining)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path != "/user/repos":
            self._json(404, {"message": "Not Found"})
            return

        query = urllib.parse.parse_qs(parsed.query)
        page = int((query.get("page") or ["1"])[0])
        per_page = int((query.get("per_page") or ["100"])[0])
        if gh.page_size:
            per_page = gh.page_size
        start = (page - 1) * per_page
        self._json(200, gh.repos[start:start + per_page])

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeGitHubServer:
    """Context manager around a threaded fake GitHub server."""

    def __init__(self, gh: FakeGitHub | None = None):
        self.gh = gh or FakeGitHub()
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.gh = self.gh  # type: ignore[attr-defined]
        import threading

        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeGitHubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
