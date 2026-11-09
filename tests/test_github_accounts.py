"""GitHub multi-account: encrypted storage, verified identity, project binding.

Real PostgreSQL and a real local fake GitHub server. The invariant that matters
most: the PAT never appears in any returned view, and a project never silently
falls back to a different account.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_github_accounts import (  # noqa: E402
    GitHubAccountService,
    SecretsError,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_github import FakeGitHubServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "ghp_supersecret_value_never_leaked"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def store():
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def gh_server():
    with FakeGitHubServer() as server:
        yield server


@pytest.fixture
def service(store, gh_server, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-for-accounts")
    return GitHubAccountService(
        store,
        client_factory=lambda token: GitHubClient(gh_server.base_url, token=token, timeout=5.0))


def test_add_verifies_identity_and_never_returns_the_token(service, store):
    view = service.add("user:alice", label="work", token=TOKEN)
    assert view["login"] == "Pavel1778" and view["state"] == "active"
    assert TOKEN not in str(view)
    # The stored bytes are ciphertext, not the token.
    with store._tx() as cur:
        cur.execute("SELECT token_encrypted FROM nexus_github_accounts")
        blob = bytes(cur.fetchone()["token_encrypted"])
    assert TOKEN.encode() not in blob
    # The list view is token-free too.
    assert TOKEN not in str(service.list_accounts("user:alice"))


def test_add_requires_a_secret_key(store, gh_server):
    svc = GitHubAccountService(
        store, client_factory=lambda token: GitHubClient(gh_server.base_url, token=token))
    if "NEXUS_SECRET_KEY" in os.environ:
        del os.environ["NEXUS_SECRET_KEY"]
    with pytest.raises(SecretsError):
        svc.add("user:alice", label="x", token=TOKEN)


def test_add_rejects_a_bad_token(service, gh_server):
    gh_server.gh.force_status = 401
    with pytest.raises(ValueError):
        service.add("user:alice", label="bad", token="nope")


def test_rotate_replaces_the_stored_token(service, store):
    view = service.add("user:alice", label="work", token=TOKEN)
    service.rotate("user:alice", view["id"], token="ghp_a_different_secret")
    # The new token decrypts for the project client path.
    with store._tx() as cur:
        cur.execute("SELECT token_encrypted FROM nexus_github_accounts WHERE id=%s", (view["id"],))
        assert b"ghp_a_different_secret" not in bytes(cur.fetchone()["token_encrypted"])


def test_revoke_marks_the_account_and_blocks_use(service):
    view = service.add("user:alice", label="work", token=TOKEN)
    service.revoke("user:alice", view["id"])
    with pytest.raises(ValueError):
        service._decrypt("user:alice", view["id"])


def test_verify_marks_a_rejected_account_invalid(service, gh_server):
    view = service.add("user:alice", label="work", token=TOKEN)
    gh_server.gh.force_status = 401
    updated = service.verify("user:alice", view["id"])
    assert updated["state"] == "invalid" and updated["last_error"]


def test_project_binding_and_client_resolution(service, store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    account = service.add("user:alice", label="work", token=TOKEN)
    bound = service.bind_project("user:alice", project, account["id"])
    assert str(bound["github_account_id"]) == account["id"]
    client = service.client_for_project("user:alice", project)
    assert client.current_user()["login"] == "Pavel1778"


def test_unbound_project_has_no_client(service, store):
    project = str(ProjectService(store).create("user:alice", "P")["id"])
    with pytest.raises(NotFound):
        service.client_for_project("user:alice", project)


def test_bind_refuses_a_foreign_or_inactive_account(service, store):
    project = str(ProjectService(store).create("user:alice", "P")["id"])
    account = service.add("user:alice", label="work", token=TOKEN)
    service.revoke("user:alice", account["id"])
    with pytest.raises(NotFound):
        service.bind_project("user:alice", project, account["id"])
    with pytest.raises(NotFound):
        service.bind_project("user:alice", project, "00000000-0000-0000-0000-000000000000")


def test_accounts_are_isolated_per_principal(service, store):
    service.add("user:alice", label="work", token=TOKEN)
    other = str(ProjectService(store).create("user:bob", "B")["id"])
    with pytest.raises(NotFound):
        service.bind_project("user:bob", other, "00000000-0000-0000-0000-000000000000")
    assert service.list_accounts("user:bob") == []


# ------------------------------------------------------------------- HTTP routes
def _http_app(gh_server, monkeypatch):
    from orchestration.web_auth import AuthService, hash_password
    from orchestration.web_server import App, WebServer

    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key-for-accounts")
    monkeypatch.setenv("OPENHANDS_API_KEY", "fake")
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("t"),
                           session_secret="test-session-secret-which-is-long-enough")
    app.github_accounts = GitHubAccountService(
        app.store,
        client_factory=lambda token: GitHubClient(gh_server.base_url, token=token, timeout=5.0))
    alice = app.auth._open_session("alice")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    return app, server, alice


def test_account_http_add_list_bind(store, gh_server, monkeypatch):
    from tests.test_nexus_projects import request

    _reset()
    store.close()
    app, server, alice = _http_app(gh_server, monkeypatch)
    base = f"http://127.0.0.1:{server.port}"
    try:
        status, payload = request(base, "/api/github/accounts", "POST",
                                  {"label": "work", "token": TOKEN}, session=alice)
        assert status == 201
        account = payload["account"]
        assert account["login"] == "Pavel1778"
        assert TOKEN not in str(payload)  # the token never comes back

        _, listing = request(base, "/api/github/accounts", session=alice)
        assert listing["accounts"][0]["label"] == "work"
        assert TOKEN not in str(listing)

        _, proj = request(base, "/api/v1/projects", "POST", {"name": "P"}, session=alice)
        pid = proj["project"]["id"]
        status, bound = request(base, f"/api/v1/projects/{pid}/github/account", "POST",
                                {"account_id": account["id"]}, session=alice)
        assert status == 200 and str(bound["project"]["github_account_id"]) == account["id"]
    finally:
        server.stop()
        app.store.close()


def test_account_routes_require_auth(store, gh_server, monkeypatch):
    from tests.test_nexus_projects import request

    _reset()
    store.close()
    app, server, alice = _http_app(gh_server, monkeypatch)
    base = f"http://127.0.0.1:{server.port}"
    try:
        assert request(base, "/api/github/accounts")[0] == 401
        assert request(base, "/api/github/accounts", "POST",
                       {"label": "x", "token": TOKEN})[0] == 401
    finally:
        server.stop()
        app.store.close()
