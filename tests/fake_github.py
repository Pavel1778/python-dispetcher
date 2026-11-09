"""A real local HTTP server that speaks the GitHub REST surface we consume.

Tests drive :class:`orchestration.github_client.GitHubClient` (and the guarded
:class:`orchestration.github_client.GitHubWriter`) over real sockets against this
server, so the client's request building, auth header, pagination, error mapping
and write bodies are all exercised for real. It is not a mock of the client.

Discovery reads ``GET /user/repos``; the detail views add ``branches``,
``commits``, ``issues`` and ``pulls``; the guarded writes are ``POST
/user/repos`` and ``PATCH /repos/{owner}/{name}``. Every write is recorded in
``gh.writes`` so a test can prove an unconfirmed action never reached GitHub.
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
        #: The authenticated login returned by GET /user.
        self.login = "Pavel1778"
        #: Every write the fake received, as ``(method, path, body)``.
        self.writes: list[tuple[str, str, dict]] = []
        #: Extra read payloads keyed by path suffix, for the detail endpoints.
        self.branches: list[dict] = [
            {"name": "main", "commit": {"sha": "abc123"}, "protected": True},
            {"name": "dev", "commit": {"sha": "def456"}, "protected": False},
        ]
        self.commits: list[dict] = [
            {"sha": "abc123", "commit": {"message": "Initial commit\n\nbody",
                                         "author": {"name": "Pavel", "date": "2026-01-03T00:00:00Z"}}},
        ]
        self.issues: list[dict] = [
            {"number": 1, "title": "Bug", "state": "open",
             "user": {"login": "Pavel"}, "created_at": "2026-01-01T00:00:00Z"},
            {"number": 2, "title": "A pull request", "state": "open",
             "user": {"login": "Pavel"}, "created_at": "2026-01-01T00:00:00Z",
             "pull_request": {"url": "x"}},
        ]
        self.pulls: list[dict] = [
            {"number": 3, "title": "Feature", "state": "open",
             "user": {"login": "Pavel"}, "head": {"ref": "feature"},
             "base": {"ref": "main"}, "created_at": "2026-01-02T00:00:00Z"},
        ]
        #: The repository tree, as GitHub's git/trees returns it.
        self.tree: list[dict] = [
            {"path": "README.md", "type": "blob", "size": 120},
            {"path": "src", "type": "tree", "size": None},
            {"path": "src/app.py", "type": "blob", "size": 340},
            {"path": "tests/test_app.py", "type": "blob", "size": 210},
            {"path": "requirements.txt", "type": "blob", "size": 30},
        ]
        #: File contents, keyed by path, returned base64 from /contents/{path}.
        self.contents: dict[str, str] = {
            "README.md": "# Demo project\n\nA tiny service used by tests.\n",
            "requirements.txt": "pytest\n",
            "src/app.py": "def add(a, b):\n    return a + b\n",
            "tests/test_app.py": "from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        }


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

        if parsed.path == "/user/repos":
            query = urllib.parse.parse_qs(parsed.query)
            page = int((query.get("page") or ["1"])[0])
            per_page = int((query.get("per_page") or ["100"])[0])
            if gh.page_size:
                per_page = gh.page_size
            start = (page - 1) * per_page
            self._json(200, gh.repos[start:start + per_page])
            return

        if parsed.path == "/user":
            self._json(200, {"login": gh.login, "type": "User"})
            return

        # /repos/{owner}/{name}[/section]
        # GitHub decodes percent-encoded path segments back to UTF-8, so the
        # fake does too: a Cyrillic owner/name/path must reach the same repo the
        # client encoded. Without this the regression test would pass only
        # because the client never encoded.
        parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
        if len(parts) >= 3 and parts[0] == "repos":
            full_name = f"{parts[1]}/{parts[2]}"
            repo = next((r for r in gh.repos if r["full_name"] == full_name), None)
            if repo is None:
                self._json(404, {"message": "Not Found"})
                return
            if len(parts) == 3:
                self._json(200, {**repo, "html_url": f"https://github.com/{full_name}"})
                return
            section = parts[3]
            if section == "branches":
                self._json(200, gh.branches)
                return
            if section == "commits":
                self._json(200, gh.commits)
                return
            if section == "issues":
                self._json(200, gh.issues)
                return
            if section == "pulls":
                self._json(200, gh.pulls)
                return
            # /repos/{owner}/{name}/git/trees/{ref}
            if len(parts) == 6 and section == "git" and parts[4] == "trees":
                self._json(200, {"tree": gh.tree, "truncated": False})
                return
            # /repos/{owner}/{name}/contents/{path...}
            if section == "contents" and len(parts) >= 5:
                rel = "/".join(parts[4:])
                content = gh.contents.get(rel)
                if content is None:
                    self._json(404, {"message": "Not Found"})
                    return
                import base64
                self._json(200, {
                    "path": rel, "encoding": "base64",
                    "content": base64.b64encode(content.encode()).decode(),
                })
                return
        self._json(404, {"message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        gh: FakeGitHub = self.server.gh  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_body()
        gh.writes.append(("POST", parsed.path, body))
        gh.auth_headers.append(self.headers.get("Authorization"))
        if gh.force_status:
            self._json(gh.force_status, {"message": "forced"})
            return
        if parsed.path == "/user/repos":
            name = body.get("name")
            owner = "Pavel1778"
            full = f"{owner}/{name}"
            if any(r["full_name"] == full for r in gh.repos):
                self._json(422, {"message": "name already exists"})
                return
            repo = {"full_name": full, "default_branch": "main",
                    "private": bool(body.get("private")), "archived": False,
                    "pushed_at": None, "html_url": f"https://github.com/{full}"}
            gh.repos.append(repo)
            self._json(201, repo)
            return
        self._json(404, {"message": "Not Found"})

    def do_PATCH(self) -> None:  # noqa: N802
        gh: FakeGitHub = self.server.gh  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_body()
        gh.writes.append(("PATCH", parsed.path, body))
        gh.auth_headers.append(self.headers.get("Authorization"))
        if gh.force_status:
            self._json(gh.force_status, {"message": "forced"})
            return
        parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
        if len(parts) == 3 and parts[0] == "repos":
            full = f"{parts[1]}/{parts[2]}"
            repo = next((r for r in gh.repos if r["full_name"] == full), None)
            if repo is None:
                self._json(404, {"message": "Not Found"})
                return
            if "private" in body:
                repo["private"] = bool(body["private"])
            self._json(200, {**repo, "html_url": f"https://github.com/{full}"})
            return
        self._json(404, {"message": "Not Found"})

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

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
