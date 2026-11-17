"""The Git credential a task's sandbox is given (and when it is not).

A task that must push to GitHub cannot without a credential in its sandbox: the
push is refused and the task can never verify. ``git_credential_for`` resolves
the token from the same identity the repository is read with, and the executor
hands it to a transport that supports it (the direct one) as the agent-server's
``secrets`` mapping - where the server injects it and masks it in the agent's
output.

These are the invariants: the credential is the *bound account's* token (never
another's, never fabricated), a task with no credential runs unchanged, and a
transport that does not support secrets is left exactly as it was.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_github_accounts import GitHubAccountService  # noqa: E402
from orchestration.nexus_lab import OpenHandsExecutor, git_credential_for  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:owner"
REPO = "Pavel1778/python-dispetcher"
TOKEN = "ghp_bound_account_secret_never_leaked"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


def _project_with_bound_account(store):
    """A project whose repository is linked to a stored GitHub account."""
    gh = FakeGitHub(repos=[{"full_name": REPO, "default_branch": "main",
                            "private": True, "archived": False, "pushed_at": None}])
    with FakeGitHubServer(gh) as server:
        svc = GitHubAccountService(
            store, client_factory=lambda t: GitHubClient(base_url=server.base_url, token=t))
        account = svc.add(PRINCIPAL, label="bound", token=TOKEN)
        projects = ProjectService(store)
        project = str(projects.create(PRINCIPAL, "Cred")["id"])
        store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
        links = ProjectRepositoryService(store)
        links.link(PRINCIPAL, project, REPO, account_id=account["id"])
    return project, account


def test_the_bound_accounts_token_is_handed_to_the_sandbox(store):
    project, _ = _project_with_bound_account(store)
    resolve = git_credential_for(store)
    secrets = resolve({"project_id": project, "repository": REPO})
    assert secrets is not None
    assert secrets["GITHUB_TOKEN"]["kind"] == "StaticSecret"
    assert secrets["GITHUB_TOKEN"]["value"] == TOKEN


def test_a_task_with_no_repository_gets_no_credential(store):
    resolve = git_credential_for(store)
    assert resolve({"project_id": "00000000-0000-0000-0000-000000000000",
                    "repository": ""}) is None


def test_a_revoked_account_yields_no_credential_not_another(store):
    project, account = _project_with_bound_account(store)
    # Mark the account invalid: the resolver must not fall back to a different
    # identity, it returns None (the task runs with no credential).
    with store._tx() as cur:
        cur.execute("UPDATE nexus_github_accounts SET state='invalid' WHERE id=%s",
                    (account["id"],))
    resolve = git_credential_for(store, fallback_token="server-token-xyz")
    # A link that names an account that cannot be decrypted is not a reason to
    # use the server token for *that* repository: the resolver returns None.
    assert resolve({"project_id": project, "repository": REPO}) is None


def test_the_server_token_is_the_fallback_only_when_no_account_is_bound(store):
    projects = ProjectService(store)
    project = str(projects.create(PRINCIPAL, "NoAccount")["id"])
    store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
    ProjectRepositoryService(store).link(PRINCIPAL, project, REPO)  # no account
    resolve = git_credential_for(store, fallback_token="server-token-xyz")
    secrets = resolve({"project_id": project, "repository": REPO})
    assert secrets["GITHUB_TOKEN"]["value"] == "server-token-xyz"


# --------------------------------------------------------------- the executor
class _RecordingClient:
    """A transport that records what it was asked to start."""

    supports_files = False
    supports_git_secrets = True

    def __init__(self):
        self.secrets: dict | None = None

    def start_conversation(self, **kw):
        self.secrets = kw.get("secrets")
        from orchestration.cloud_client import StartStatus, StartTask
        return StartTask(id="c1", status=StartStatus.READY.value, app_conversation_id="c1")


class _PlainClient(_RecordingClient):
    """A transport that does not support secrets (e.g. the app transport)."""
    supports_git_secrets = False


def _task():
    return {"task_key": "t1", "title": "t", "run_id": "r1", "project_id": "p1",
            "repository": REPO, "branch": "main"}


def test_the_executor_passes_the_credential_to_a_supporting_transport(store):
    client = _RecordingClient()
    executor = OpenHandsExecutor(client)
    executor.git_secrets_for = lambda task: {"GITHUB_TOKEN": {"kind": "StaticSecret",
                                                              "value": "tok"}}
    executor._dispatch_with(client, None, _task())
    assert client.secrets == {"GITHUB_TOKEN": {"kind": "StaticSecret", "value": "tok"}}


def test_the_executor_leaves_an_unsupporting_transport_alone(store):
    client = _PlainClient()
    executor = OpenHandsExecutor(client)
    executor.git_secrets_for = lambda task: {"GITHUB_TOKEN": {"kind": "StaticSecret",
                                                              "value": "tok"}}
    executor._dispatch_with(client, None, _task())
    assert client.secrets is None, "a transport without support must not receive secrets"


def test_no_resolver_means_no_secrets(store):
    client = _RecordingClient()
    executor = OpenHandsExecutor(client)  # git_secrets_for is None by default
    executor._dispatch_with(client, None, _task())
    assert client.secrets is None
