"""Multi-account GitHub: isolation, encryption, per-account discovery, binding.

Real PostgreSQL and two real fake GitHub servers. The invariants that matter:
two accounts are fully isolated (an account's repositories come from *its* token,
never another's), the PAT is never returned, and a project's repository list is
driven by the account it is bound to — which is what makes the visual repository
picker multi-account rather than single-token.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_github_accounts import GitHubAccountService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN_A = "ghp_account_a_secret_never_leaked"
TOKEN_B = "ghp_account_b_secret_never_leaked"


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
def two_accounts():
    """Two real fake GitHub servers, each with its own repositories."""
    a = FakeGitHub(repos=[
        {"full_name": "Pavel1778/python-dispetcher", "default_branch": "main",
         "private": True, "archived": False, "pushed_at": None},
        {"full_name": "Pavel1778/Klyrshift", "default_branch": "main",
         "private": False, "archived": False, "pushed_at": None},
    ])
    a.login = "Pavel1778"
    b = FakeGitHub(repos=[
        {"full_name": "acme/rtk-crm", "default_branch": "main",
         "private": True, "archived": False, "pushed_at": None},
        {"full_name": "acme/gram-messenger", "default_branch": "develop",
         "private": False, "archived": False, "pushed_at": None},
    ])
    b.login = "acme"
    with FakeGitHubServer(a) as sa, FakeGitHubServer(b) as sb:
        yield sa, sb


@pytest.fixture
def service(store, two_accounts, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    sa, sb = two_accounts

    def factory(token):
        # Route each token to the server whose login it carries; both are real.
        base = sa.base_url if "account_a" in token else sb.base_url
        return GitHubClient(base, token=token, timeout=5.0)

    return GitHubAccountService(store, client_factory=factory)


def _project(store, principal="user:alice", name="P") -> str:
    return str(ProjectService(store).create(principal, name)["id"])


def test_three_accounts_are_all_supported(store, two_accounts, monkeypatch):
    """Nothing caps the number of accounts at two."""
    monkeypatch.setenv("NEXUS_SECRET_KEY", "k")
    sa, sb = two_accounts
    with FakeGitHubServer() as sc:
        sc.gh.login = "third"
        clients = {
            "one": GitHubClient(sa.base_url, token="t1"),
            "two": GitHubClient(sb.base_url, token="t2"),
            "three": GitHubClient(sc.base_url, token="t3"),
        }
        svc = GitHubAccountService(store, client_factory=lambda t: clients[t])
        for label in ("one", "two", "three"):
            svc.add("user:alice", label=label, token=label)
        accounts = svc.list_accounts("user:alice")
        assert len(accounts) == 3
        assert {a["label"] for a in accounts} == {"one", "two", "three"}


def test_each_account_discovers_only_its_own_repositories(service):
    service.add("user:alice", label="personal", token="ghp_account_a")
    service.add("user:alice", label="work", token="ghp_account_b")
    accounts = {a["label"]: a for a in service.list_accounts("user:alice")}
    personal = service.client_for_account("user:alice", accounts["personal"]["id"])
    work = service.client_for_account("user:alice", accounts["work"]["id"])
    personal_names = {r.full_name for r in personal.list_repositories()}
    work_names = {r.full_name for r in work.list_repositories()}
    assert personal_names == {"Pavel1778/python-dispetcher", "Pavel1778/Klyrshift"}
    assert work_names == {"acme/rtk-crm", "acme/gram-messenger"}
    # No leakage between the two identities.
    assert personal_names.isdisjoint(work_names)


def test_no_account_ever_returns_a_token(service, store):
    service.add("user:alice", label="personal", token=TOKEN_A)
    service.add("user:alice", label="work", token=TOKEN_B)
    listing = service.list_accounts("user:alice")
    assert TOKEN_A not in str(listing) and TOKEN_B not in str(listing)
    with store._tx() as cur:
        cur.execute("SELECT token_encrypted FROM nexus_github_accounts")
        blobs = [bytes(r["token_encrypted"]) for r in cur.fetchall()]
    for blob in blobs:
        assert TOKEN_A.encode() not in blob and TOKEN_B.encode() not in blob


def test_account_isolation_blocks_cross_principal_access(service):
    view = service.add("user:alice", label="personal", token="ghp_account_a")
    # A different principal cannot resolve alice's account client.
    with pytest.raises(NotFound):
        service.client_for_account("user:bob", view["id"])


def test_project_binds_to_one_account_and_never_falls_back(service, store):
    a = service.add("user:alice", label="personal", token="ghp_account_a")
    b = service.add("user:alice", label="work", token="ghp_account_b")
    pid = _project(store)
    service.bind_project("user:alice", pid, a["id"])
    client = service.client_for_project("user:alice", pid)
    names = {r.full_name for r in client.list_repositories()}
    assert "Pavel1778/Klyrshift" in names and "acme/rtk-crm" not in names
    # Rebinding to b switches identity explicitly, never silently.
    service.bind_project("user:alice", pid, b["id"])
    names_b = {r.full_name for r in service.client_for_project("user:alice", pid)
               .list_repositories()}
    assert "acme/rtk-crm" in names_b and "Pavel1778/Klyrshift" not in names_b


def test_a_project_without_an_account_raises(service, store):
    pid = _project(store)
    with pytest.raises(NotFound):
        service.client_for_project("user:alice", pid)


def test_binding_a_foreign_account_is_refused(service, store):
    view = service.add("user:alice", label="personal", token="ghp_account_a")
    pid = _project(store, name="B")
    with pytest.raises(NotFound):
        service.bind_project("user:bob", pid, view["id"])


def test_rotate_replaces_the_stored_token(service, store):
    view = service.add("user:alice", label="personal", token=TOKEN_A)
    service.rotate("user:alice", view["id"], token="ghp_account_a_v2")
    with store._tx() as cur:
        cur.execute("SELECT token_encrypted FROM nexus_github_accounts WHERE id=%s",
                    (view["id"],))
        blob = bytes(cur.fetchone()["token_encrypted"])
    assert TOKEN_A.encode() not in blob
    # The rotated token still resolves a client for the account.
    assert service.client_for_account("user:alice", view["id"]) is not None


def test_revoke_blocks_use(service):
    view = service.add("user:alice", label="personal", token="ghp_account_a")
    service.revoke("user:alice", view["id"])
    with pytest.raises(ValueError):
        service.client_for_account("user:alice", view["id"])
