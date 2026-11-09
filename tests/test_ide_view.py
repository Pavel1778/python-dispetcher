"""The IDE view's read-only GitHub routes: file tree and one file's contents.

Real PostgreSQL, a real web server and a real fake GitHub server. The point is
that the tree and the file text come from the bound repository over a real
socket, and that a Cyrillic path segment is decoded (not mangled) end to end.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.web_auth import AuthService, hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_github import FakeGitHubServer  # noqa: E402
from tests.test_nexus_projects import request  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "ghp_ide_token_never_leaked"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def http_server(monkeypatch):
    _reset()
    monkeypatch.setenv("OPENHANDS_API_KEY", "fake")
    with FakeGitHubServer() as gh:
        app = App(DSN, token="machine-token")
        app.auth = AuthService(app.store, username="alice", password_hash=hash_password("t"),
                               session_secret="test-session-secret-which-is-long-enough")
        # Point the app's GitHub client at the fake server, with a token.
        app.github = GitHubClient(gh.base_url, token=TOKEN, timeout=5.0)
        alice = app.auth._open_session("alice")
        server = WebServer(app, "127.0.0.1", 0)
        server.start()
        try:
            yield f"http://127.0.0.1:{server.port}", alice, app, gh
        finally:
            server.stop()


def _project_with_repo(base, session, app):
    _, proj = request(base, "/api/v1/projects", "POST", {"name": "IDE"}, session=session)
    pid = proj["project"]["id"]
    app.store.add_repository("Pavel1778/python-dispetcher", allowed_branches=["main"])
    app.project_binding.set_repository("user:alice", pid,
                                       full_name="Pavel1778/python-dispetcher", branch="main")
    return pid


def test_tree_route_returns_real_files(http_server):
    base, alice, app, _gh = http_server
    pid = _project_with_repo(base, alice, app)
    st, out = request(base, f"/api/v1/projects/{pid}/github/repositories/"
                            "Pavel1778/python-dispetcher/tree?ref=main", session=alice)
    assert st == 200
    paths = {t["path"] for t in out["tree"]}
    assert "README.md" in paths and "src/app.py" in paths


def test_contents_route_returns_file_text(http_server):
    base, alice, app, _gh = http_server
    pid = _project_with_repo(base, alice, app)
    st, out = request(base, f"/api/v1/projects/{pid}/github/repositories/"
                            "Pavel1778/python-dispetcher/contents/src/app.py?ref=main",
                      session=alice)
    assert st == 200
    assert "def add" in out["content"]


def test_a_cyrillic_file_path_is_decoded(http_server):
    base, alice, app, gh = http_server
    gh.gh.contents["src/имя.py"] = "print('привет')\n"
    pid = _project_with_repo(base, alice, app)
    st, out = request(base, f"/api/v1/projects/{pid}/github/repositories/"
                            "Pavel1778/python-dispetcher/contents/src/%D0%B8%D0%BC%D1%8F.py"
                            "?ref=main", session=alice)
    assert st == 200
    assert out["content"] == "print('привет')\n"


def test_the_token_is_never_returned(http_server):
    base, alice, app, _gh = http_server
    pid = _project_with_repo(base, alice, app)
    st, out = request(base, f"/api/v1/projects/{pid}/github/repositories/"
                            "Pavel1778/python-dispetcher/branches", session=alice)
    assert st == 200
    assert TOKEN not in str(out)


def test_ide_routes_require_auth(http_server):
    base, alice, app, _gh = http_server
    pid = _project_with_repo(base, alice, app)
    path = f"/api/v1/projects/{pid}/github/repositories/Pavel1778/python-dispetcher/tree"
    assert request(base, path)[0] == 401
