"""The configuration reconciler: desired vs effective, drift, and honest states.

Real PostgreSQL and the real ``OpenHandsAccountService``; only the OpenHands
*read* (``get_settings``) is stubbed, because the reconciler's whole job is the
comparison — stubbing the comparison would test nothing. The stub is the same
surface the live ``GET /api/v1/settings`` returns (``agent_settings.llm``).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudAuthError, CloudUnavailable  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.openhands_config_sync import (  # noqa: E402
    BLOCKED, DRIFT, PENDING, SYNCED, ConfigReconciler)
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:alice"
KEY = "sk-openhands-reconcile-secret"


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
def cloud():
    with FakeCloudServer() as server:
        server.cloud.settings = {
            "llm_api_key_set": True, "active_agent_profile_id": "p",
            "agent_settings": {"llm": {"model": "openhands/one", "base_url": None}},
            "registered_marketplaces": [], "inherited_marketplaces": [],
        }
        yield server


@pytest.fixture
def service(store, cloud, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "reconcile-test-secret-key")
    return OpenHandsAccountService(store)


def _account(service, cloud, *, model="openhands/one", llm_base_url=None):
    return service.add(PRINCIPAL, label="primary", api_key=KEY,
                       base_url=cloud.base_url, transport="direct",
                       default_model=model, llm_base_url=llm_base_url)


def test_matching_configuration_is_synced(store, cloud, service):
    _account(service, cloud, model="openhands/one")
    result = ConfigReconciler(store, service).reconcile(PRINCIPAL, _account(
        service, cloud, model="openhands/one")["id"])
    assert result.state == SYNCED
    with store._tx() as cur:
        cur.execute("SELECT config_sync_state FROM nexus_openhands_accounts")
        assert cur.fetchone()["config_sync_state"] == SYNCED


def test_a_different_model_is_drift_with_the_reason(store, cloud, service):
    account = _account(service, cloud, model="openhands/wanted")
    # OpenHands still reports "openhands/one".
    result = ConfigReconciler(store, service).reconcile(PRINCIPAL, account["id"])
    assert result.state == DRIFT
    assert "model" in result.detail and "openhands/wanted" in result.detail


def test_a_matching_llm_base_url_is_synced(store, cloud, service):
    cloud.cloud.settings["agent_settings"]["llm"]["base_url"] = "https://llm.test/v1"
    account = _account(service, cloud, model="openhands/one",
                       llm_base_url="https://llm.test/v1/")
    result = ConfigReconciler(store, service).reconcile(PRINCIPAL, account["id"])
    assert result.state == SYNCED  # trailing slash is normalised


def test_a_different_llm_base_url_is_drift(store, cloud, service):
    cloud.cloud.settings["agent_settings"]["llm"]["base_url"] = "https://other.test/v1"
    account = _account(service, cloud, model="openhands/one",
                       llm_base_url="https://llm.test/v1")
    result = ConfigReconciler(store, service).reconcile(PRINCIPAL, account["id"])
    assert result.state == DRIFT
    assert "base_url" in result.detail


def test_an_unset_base_url_is_never_drift(store, cloud, service):
    """No configured base URL means 'the provider default'; whatever the server
    reports there is not a mismatch NEXUS should complain about."""
    cloud.cloud.settings["agent_settings"]["llm"]["base_url"] = "https://whatever.test"
    account = _account(service, cloud, model="openhands/one", llm_base_url=None)
    result = ConfigReconciler(store, service).reconcile(PRINCIPAL, account["id"])
    assert result.state == SYNCED


def test_an_unreachable_openhands_is_pending_not_drift(store, cloud, service, monkeypatch):
    account = _account(service, cloud, model="openhands/one")
    rec = ConfigReconciler(store, service)

    def boom(*a, **k):
        raise CloudUnavailable("down")

    monkeypatch.setattr(rec, "effective", boom)
    result = rec.reconcile(PRINCIPAL, account["id"])
    assert result.state == PENDING


def test_a_rejected_key_is_blocked(store, cloud, service, monkeypatch):
    account = _account(service, cloud, model="openhands/one")
    rec = ConfigReconciler(store, service)

    def boom(*a, **k):
        raise CloudAuthError("401")

    monkeypatch.setattr(rec, "effective", boom)
    result = rec.reconcile(PRINCIPAL, account["id"])
    assert result.state == BLOCKED


def test_reconcile_all_covers_active_accounts(store, cloud, service):
    _account(service, cloud, model="openhands/one")
    results = ConfigReconciler(store, service).reconcile_all(PRINCIPAL)
    assert len(results) == 1 and results[0].state == SYNCED
