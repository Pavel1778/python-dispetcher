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
        #: A history of its own for a repository, oldest commit first:
        #: ``{full_name: [{"sha", "message", "files": [{"filename", "status",
        #: "additions", "deletions"}]}]}``. A repository that has one answers
        #: ``/commits`` (newest first) and ``/compare/{base}...{head}`` from it;
        #: one that has none answers ``/commits`` with ``self.commits`` as
        #: before. ``push`` appends to it - what an agent's ``git push`` does.
        self.history: dict[str, list[dict]] = {}
        #: Check runs per commit: ``{sha: [{"name", "status", "conclusion"}]}``.
        self.check_runs: dict[str, list[dict]] = {}
        #: Which repositories a token can see: ``{token: {full_name, ...}}``.
        #: A token that is not listed sees everything (as before); a listed
        #: one gets 404 for anything else - what GitHub answers for a private
        #: repository the token has no access to.
        self.token_repos: dict[str, set[str]] = {}
        #: What a token may do with a repository it sees, where that differs
        #: from "everything": ``{(token, full_name): {"push": False, ...}}``.
        self.token_permissions: dict[tuple[str, str], dict] = {}
        #: File contents, keyed by path, returned base64 from /contents/{path}.
        self.contents: dict[str, str] = {
            "README.md": "# Demo project\n\nA tiny service used by tests.\n",
            "requirements.txt": "pytest\n",
            "src/app.py": "def add(a, b):\n    return a + b\n",
            "tests/test_app.py": "from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        }


    # ----------------------------------------------------------- for tests
    def push(self, full_name: str, sha: str, message: str = "work",
             files: list[dict] | None = None) -> str:
        """A new head for *full_name*'s branch: what a ``git push`` leaves."""
        self.history.setdefault(full_name, []).append(
            {"sha": sha, "message": message, "files": files or []})
        return sha

    def head(self, full_name: str) -> str | None:
        commits = self.history.get(full_name)
        return commits[-1]["sha"] if commits else None

    def sees(self, authorization: str | None, full_name: str) -> bool:
        token = (authorization or "").split(" ", 1)[-1]
        allowed = self.token_repos.get(token)
        return allowed is None or full_name in allowed

    def permissions(self, authorization: str | None, full_name: str) -> dict:
        token = (authorization or "").split(" ", 1)[-1]
        return {"admin": True, "push": True, "pull": True,
                **self.token_permissions.get((token, full_name), {})}

    @staticmethod
    def _commit(item: dict) -> dict:
        return {"sha": item["sha"],
                "commit": {"message": item.get("message") or "work",
                           "author": {"name": "agent", "date": "2026-01-04T00:00:00Z"}}}


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
            # A token is shown only what it has access to, as on GitHub.
            authorization = self.headers.get("Authorization")
            visible = [repo for repo in gh.repos if gh.sees(authorization, repo["full_name"])]
            self._json(200, visible[start:start + per_page])
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
            if repo is None or not gh.sees(self.headers.get("Authorization"), full_name):
                self._json(404, {"message": "Not Found"})
                return
            if len(parts) == 3:
                self._json(200, {**repo, "html_url": f"https://github.com/{full_name}",
                                 "visibility": "private" if repo.get("private") else "public",
                                 "permissions": gh.permissions(
                                     self.headers.get("Authorization"), full_name)})
                return
            section = parts[3]
            if section == "branches":
                self._json(200, gh.branches)
                return
            # /repos/{owner}/{name}/commits/{sha}/check-runs
            if section == "commits" and len(parts) == 6 and parts[5] == "check-runs":
                runs = gh.check_runs.get(parts[4], [])
                self._json(200, {"total_count": len(runs), "check_runs": runs})
                return
            if section == "commits":
                own = gh.history.get(full_name)
                self._json(200, [gh._commit(item) for item in reversed(own)]
                           if own is not None else gh.commits)
                return
            # /repos/{owner}/{name}/compare/{base}...{head}
            if section == "compare" and len(parts) == 5 and "..." in parts[4]:
                base, head = parts[4].split("...", 1)
                shas = [item["sha"] for item in gh.history.get(full_name, [])]

                def index(prefix: str) -> int | None:
                    found = [i for i, sha in enumerate(shas) if sha.startswith(prefix)]
                    return found[0] if found else None

                b, h = index(base), index(head)
                if b is None or h is None:
                    self._json(404, {"message": "Not Found"})
                    return
                between = gh.history[full_name][b + 1:h + 1]
                files: dict[str, dict] = {}
                for item in between:
                    for change in item.get("files") or []:
                        seen = files.setdefault(change["filename"], {
                            "filename": change["filename"],
                            "status": change.get("status") or "modified",
                            "additions": 0, "deletions": 0})
                        seen["additions"] += int(change.get("additions") or 0)
                        seen["deletions"] += int(change.get("deletions") or 0)
                self._json(200, {
                    "status": "identical" if b == h else "ahead" if b < h else "behind",
                    "ahead_by": max(0, h - b), "behind_by": max(0, b - h),
                    "total_commits": len(between),
                    "commits": [gh._commit(item) for item in between],
                    "files": list(files.values())})
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
