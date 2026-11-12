"""Section A of the acceptance: connectivity to OpenHands.

The request lists six things a connectivity suite must prove and this module
proves each one against a *real* socket - the local Cloud-shaped HTTP server in
``tests/fake_cloud.py`` (or, for the unreachable case, a port nothing answers
on). Nothing here mocks the client: ``CloudClient`` makes real requests.

* A1 подключение OpenHands      - the endpoint answers and ``health()`` is True.
* A2 успешная авторизация       - a good key is accepted; the account verifies.
* A3 неверный ключ              - a rejected key is 401 -> ``CloudAuthError``,
                                  never a silent success.
* A4 недоступный endpoint       - a refused connection is ``CloudUnavailable``.
* A5 тайм-аут                   - a slow server with a short timeout is a
                                  transport failure, not a wrong answer.
* A6 повторное подключение      - the client works again after a failure; the
                                  account's reachability probe recovers.

The first five are pure client behaviour and need no database; the last uses
the account service and is skipped cleanly when no PostgreSQL is configured.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import (  # noqa: E402
    CloudAuthError,
    CloudClient,
    CloudUnavailable,
)
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")


# ------------------------------------------------------- A1 connection / health
def test_health_is_true_when_the_endpoint_answers():
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="good-key", timeout=5.0)
        assert client.health() is True


def test_health_is_false_when_nothing_answers():
    # A closed port refuses the connection, which is a transport failure.
    client = CloudClient("http://127.0.0.1:1", api_key="good-key", timeout=1.0)
    assert client.health() is False


# ------------------------------------------------------- A2 successful authorization
def test_a_good_key_is_accepted():
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="good-key", timeout=5.0)
        caps = client.account_capabilities()
        assert caps["llm_api_key_set"] is True
        # The Authorization header carried the key, and only as a Bearer token.
        assert ("GET", "/api/v1/settings") in cloud.cloud.calls


def test_missing_key_is_auth_error_before_any_request():
    """No key configured is an auth error, not a mysterious 401 later."""
    with FakeCloudServer() as cloud:
        client = CloudClient(cloud.base_url, api_key="", timeout=5.0)
        with pytest.raises(CloudAuthError):
            client.account_capabilities()
        # Nothing was sent: the client refused locally.
        assert cloud.cloud.calls == []


# ------------------------------------------------------- A3 wrong key
def test_a_rejected_key_is_auth_error_not_a_silent_success():
    with FakeCloudServer() as cloud:
        cloud.cloud.rejected_keys = {"bad-key"}
        client = CloudClient(cloud.base_url, api_key="bad-key", timeout=5.0)
        with pytest.raises(CloudAuthError):
            client.account_capabilities()
        # The same server accepts a good key: the rejection is the key's fault.
        good = CloudClient(cloud.base_url, api_key="good-key", timeout=5.0)
        assert good.account_capabilities()["llm_api_key_set"] is True


def test_a_rejected_key_still_reads_as_reachable():
    """An endpoint that answers 401 is *reachable*; the key is the problem.

    This is the distinction the operator surface depends on: ``ping`` reports
    reachability, ``verify`` reports authorization.
    """
    with FakeCloudServer() as cloud:
        cloud.cloud.rejected_keys = {"bad-key"}
        client = CloudClient(cloud.base_url, api_key="bad-key", timeout=5.0)
        probe = client.ping()
        assert probe["reachable"] is True
        assert probe["status"] is None  # a CloudError, not an HTTP status
        assert client.health() is False


# ------------------------------------------------------- A4 unreachable endpoint
def test_an_unreachable_endpoint_is_unavailable():
    client = CloudClient("http://127.0.0.1:1", api_key="good-key", timeout=1.0)
    with pytest.raises(CloudUnavailable):
        client.account_capabilities()
    probe = client.ping()
    assert probe["reachable"] is False
    assert probe["status"] is None
    assert probe["detail"]


def test_an_unreachable_endpoint_never_raises_from_ping():
    """``ping`` reports, it does not raise: reachability is a fact, not a fault."""
    client = CloudClient("http://127.0.0.1:1", api_key="good-key", timeout=1.0)
    assert client.ping()["reachable"] is False


# ------------------------------------------------------- A5 timeout
def test_a_slow_endpoint_times_out_as_unavailable():
    with FakeCloudServer() as cloud:
        cloud.cloud.delay = 0.5
        client = CloudClient(cloud.base_url, api_key="good-key", timeout=0.15)
        with pytest.raises(CloudUnavailable):
            client.account_capabilities()
        assert client.health() is False


def test_a_timeout_never_reads_as_a_success():
    """A timeout must never be mistaken for a working connection."""
    with FakeCloudServer() as cloud:
        cloud.cloud.delay = 0.5
        client = CloudClient(cloud.base_url, api_key="good-key", timeout=0.15)
        probe = client.ping()
        assert probe["reachable"] is False


def test_the_same_client_succeeds_once_the_server_answers_in_time():
    """The timeout is the endpoint's latency, not the client being broken."""
    with FakeCloudServer() as cloud:
        cloud.cloud.delay = 0.4
        slow = CloudClient(cloud.base_url, api_key="good-key", timeout=0.1)
        assert slow.health() is False
        patient = CloudClient(cloud.base_url, api_key="good-key", timeout=5.0)
        assert patient.health() is True


# ------------------------------------------------------- A6 reconnect
def test_a_client_works_again_after_the_endpoint_comes_back():
    """A transport failure is not sticky: the next call succeeds."""
    client = CloudClient("http://127.0.0.1:1", api_key="good-key", timeout=1.0)
    assert client.health() is False
    with FakeCloudServer() as cloud:
        # The very same object, pointed at a live endpoint, works.
        client.base_url = cloud.base_url
        assert client.health() is True


@pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run account tests")
def test_connection_check_recovers_after_an_outage(monkeypatch):
    """A6 повторное подключение, through the account surface.

    The account is stored against a live server; a probe with the endpoint
    pointing at a closed port records ``connection_ok=False``; pointing back at
    the live server records ``connection_ok=True`` again. The stored key is
    never returned.
    """
    import psycopg

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_openhands_accounts import OpenHandsAccountService

    _MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "migrations")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())

    store = PostgresStore(DSN)
    monkeypatch.setenv("NEXUS_SECRET_KEY", "connectivity-secret")
    try:
        with FakeCloudServer() as cloud:
            live = CloudClient(cloud.base_url, api_key="sk-live", timeout=5.0)

            def factory(base_url, api_key):
                return CloudClient(base_url=base_url, api_key=api_key, timeout=1.0)

            service = OpenHandsAccountService(store, client_factory=factory)
            view = service.add("user:alice", label="A", api_key="sk-live",
                               base_url=cloud.base_url)

            # The endpoint goes away: the probe reports unreachable.
            service._client_factory = lambda base_url, api_key: CloudClient(
                base_url="http://127.0.0.1:1", api_key=api_key, timeout=1.0)
            down = service.check_connection("user:alice", view["id"])
            assert down["reachable"] is False and down["connection_ok"] is False
            assert down["error"]

            # It comes back: the next probe is reachable again.
            service._client_factory = lambda base_url, api_key: CloudClient(
                base_url=cloud.base_url, api_key=api_key, timeout=5.0)
            up = service.check_connection("user:alice", view["id"])
            assert up["reachable"] is True and up["connection_ok"] is True
            assert up["error"] is None
            assert up["last_connection_at"] is not None
            # The key never leaves.
            assert "sk-live" not in str(up)
            assert live.base_url == cloud.base_url
    finally:
        store.close()
