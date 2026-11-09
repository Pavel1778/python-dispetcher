"""Multi-account OpenHands: encryption, isolation, capabilities, binding.

Real PostgreSQL and two real fake Cloud servers. The invariants that matter:
the API key is encrypted at rest and never returned, two accounts are isolated
(an account's client talks to *its* base URL with *its* key), a project never
silently falls back to another account, and an arbitrary number of accounts is
supported.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
KEY_A = "sk-openhands-account-a-never-leaked"
KEY_B = "sk-openhands-account-b-never-leaked"


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
def clouds():
    with FakeCloudServer() as a, FakeCloudServer() as b:
        a.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": "pA",
                            "agent_settings": {"mcp_config": {"alpha": {}}},
                            "registered_marketplaces": [], "inherited_marketplaces": []}
        b.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": "pB",
                            "agent_settings": {"mcp_config": {"beta": {}}},
                            "registered_marketplaces": [], "inherited_marketplaces": []}
        yield a, b


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    a, b = clouds

    def factory(base_url, api_key):
        # Each account's base URL selects its server; both are real.
        return CloudClient(base_url=base_url, api_key=api_key, timeout=5.0)

    return OpenHandsAccountService(store, client_factory=factory)


def _project(store, principal="user:alice", name="P") -> str:
    return str(ProjectService(store).create(principal, name)["id"])


def test_add_verifies_and_never_returns_the_key(service, store, clouds):
    a, _b = clouds
    view = service.add("user:alice", label="personal", api_key=KEY_A,
                       base_url=a.base_url, transport="direct")
    assert view["state"] == "active" and view["transport"] == "direct"
    assert KEY_A not in str(view)
    with store._tx() as cur:
        cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts")
        blob = bytes(cur.fetchone()["api_key_encrypted"])
    assert KEY_A.encode() not in blob


def test_capabilities_are_discovered_per_account(service, clouds):
    a, b = clouds
    va = service.add("user:alice", label="personal", api_key=KEY_A, base_url=a.base_url)
    vb = service.add("user:alice", label="work", api_key=KEY_B, base_url=b.base_url)
    caps_a = va["capabilities"]
    caps_b = vb["capabilities"]
    assert caps_a["mcp_servers"] == ["alpha"]
    assert caps_b["mcp_servers"] == ["beta"]


def test_three_accounts_are_supported(service, store, clouds, monkeypatch):
    a, _b = clouds
    with FakeCloudServer() as c:
        c.cloud.settings = {"llm_api_key_set": True, "agent_settings": {"mcp_config": {}},
                            "registered_marketplaces": [], "inherited_marketplaces": []}
        urls = {"one": a.base_url, "two": a.base_url, "three": c.base_url}
        svc = OpenHandsAccountService(
            store, client_factory=lambda base_url, api_key:
            CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))
        for label in ("one", "two", "three"):
            svc.add("user:alice", label=label, api_key=f"sk-{label}",
                    base_url=urls[label])
        assert len(svc.list_accounts("user:alice")) == 3


def test_only_one_account_can_be_default(service, clouds):
    a, b = clouds
    service.add("user:alice", label="personal", api_key=KEY_A, base_url=a.base_url,
                make_default=True)
    service.add("user:alice", label="work", api_key=KEY_B, base_url=b.base_url)
    accounts = {x["label"]: x for x in service.list_accounts("user:alice")}
    assert accounts["personal"]["is_default"] is True
    assert accounts["work"]["is_default"] is False
    # Making another default demotes the first.
    service.set_default("user:alice", accounts["work"]["id"])
    accounts = {x["label"]: x for x in service.list_accounts("user:alice")}
    assert accounts["work"]["is_default"] is True
    assert accounts["personal"]["is_default"] is False


def test_account_isolation_blocks_cross_principal_access(service, clouds):
    a, _b = clouds
    view = service.add("user:alice", label="personal", api_key=KEY_A, base_url=a.base_url)
    with pytest.raises(NotFound):
        service.client_for_account("user:bob", view["id"])


def test_project_binds_to_one_account_and_never_falls_back(service, store, clouds):
    a, b = clouds
    va = service.add("user:alice", label="personal", api_key=KEY_A, base_url=a.base_url)
    service.add("user:alice", label="work", api_key=KEY_B, base_url=b.base_url)
    pid = _project(store)
    service.bind_project("user:alice", pid, va["id"])
    client = service.client_for_project("user:alice", pid)
    assert client.base_url == a.base_url
    # A project with no account raises; it never uses the process-wide key.
    pid2 = _project(store, name="Q")
    with pytest.raises(NotFound):
        service.client_for_project("user:alice", pid2)


def test_rotate_and_revoke(service, store, clouds):
    a, _b = clouds
    view = service.add("user:alice", label="personal", api_key=KEY_A, base_url=a.base_url)
    service.rotate("user:alice", view["id"], api_key="sk-rotated-value")
    with store._tx() as cur:
        cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts WHERE id=%s",
                    (view["id"],))
        assert KEY_A.encode() not in bytes(cur.fetchone()["api_key_encrypted"])
    service.revoke("user:alice", view["id"])
    with pytest.raises(ValueError):
        service.client_for_account("user:alice", view["id"])


def test_add_rejects_an_unreachable_account(service):
    # A base URL nothing answers on is a hard failure, not a silent success.
    with pytest.raises(ValueError):
        service.add("user:alice", label="bad", api_key="sk-nope",
                    base_url="http://127.0.0.1:1")


def test_add_requires_a_secret_key(store, clouds, monkeypatch):
    from orchestration.secrets import SecretsError

    a, _b = clouds
    monkeypatch.delenv("NEXUS_SECRET_KEY", raising=False)
    svc = OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))
    with pytest.raises(SecretsError):
        svc.add("user:alice", label="x", api_key="sk-x", base_url=a.base_url)
