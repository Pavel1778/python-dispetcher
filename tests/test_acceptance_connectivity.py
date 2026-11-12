"""Acceptance section A — Connectivity.

Every case the task text lists, against a real HTTP server the client speaks to
over a socket:

* a working OpenHands endpoint connects and reports healthy;
* successful authorization (a good key is accepted, capabilities are read);
* a wrong key is a rejected authorization, not a transport failure;
* an unreachable endpoint is reported as such, and never as "connected";
* a timeout is a transport failure, not a success;
* a dropped connection fails that call and the *next* call reconnects.

The distinction the whole section turns on: *reachability* (did the endpoint
answer at all) and *authorization* (did it accept the key) are different
questions, and the code answers them separately (``CloudClient.ping`` vs
``CloudClient.health``/``account_capabilities``). A test that conflated them
would let "reachable but rejected" read as "connected".
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import (  # noqa: E402
    CloudAuthError,
    CloudError,
    CloudUnavailable,
    CloudClient,
)
from tests.acceptance_kit import (  # noqa: E402
    ConfigurableCloudServer,
    RedirectServer,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_SUPABASE_DB_URL"),
    reason="set TEST_SUPABASE_DB_URL (private targets are allowed by conftest)",
)


# ------------------------------------------------------------------ connects
def test_a_working_endpoint_connects_and_is_healthy():
    with ConfigurableCloudServer() as server:
        client = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        assert client.health() is True
        probe = client.ping()
        assert probe["reachable"] is True and probe["status"] == 200
        # The request really went out with the Bearer key, over the socket.
        assert server.cloud.requests
        method, path, auth = server.cloud.requests[-1]
        assert (method, path) == ("GET", "/api/v1/app-conversations/count")
        assert auth == "Bearer good-key"


def test_successful_authorization_reads_capabilities():
    with ConfigurableCloudServer() as server:
        server.cloud.required_key = "good-key"
        server.cloud.settings["active_agent_profile_id"] = "profile-xyz"
        client = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        caps = client.account_capabilities()
        assert caps["llm_api_key_set"] is True
        assert caps["active_agent_profile_id"] == "profile-xyz"
        # A key-free view: the capabilities carry no secret value.
        assert "good-key" not in str(caps)


# ---------------------------------------------------------------- bad key
def test_a_wrong_key_is_a_rejected_authorization():
    with ConfigurableCloudServer() as server:
        server.cloud.required_key = "good-key"
        client = CloudClient(server.base_url, api_key="wrong-key", timeout=5.0)
        with pytest.raises(CloudAuthError):
            client.account_capabilities()
        # health() answers "usable now?" - a rejected key is not usable.
        assert client.health() is False
        # But the endpoint *answered*: it is reachable, just refusing.
        probe = client.ping()
        assert probe["reachable"] is True
        assert "401" in (probe["detail"] or "")


def test_no_key_is_an_auth_error_without_a_request():
    with ConfigurableCloudServer() as server:
        client = CloudClient(server.base_url, api_key="", timeout=5.0)
        before = len(server.cloud.requests)
        with pytest.raises(CloudAuthError):
            client.account_capabilities()
        # Nothing was sent: the client refuses before it opens a socket.
        assert len(server.cloud.requests) == before


# ------------------------------------------------------------- unreachable
def test_an_unreachable_endpoint_is_unavailable_and_not_connected():
    # A closed port: connection refused, which is a transport failure.
    client = CloudClient("http://127.0.0.1:1", api_key="good-key", timeout=1.0)
    assert client.health() is False
    probe = client.ping()
    assert probe["reachable"] is False
    with pytest.raises(CloudUnavailable):
        client.account_capabilities()


def test_a_5xx_is_unavailable_not_healthy():
    with ConfigurableCloudServer() as server:
        server.cloud.status = 503
        client = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        assert client.health() is False
        with pytest.raises(CloudUnavailable):
            client.account_capabilities()


# ----------------------------------------------------------------- timeout
def test_a_timeout_is_a_transport_failure():
    with ConfigurableCloudServer() as server:
        server.cloud.delay = 1.0
        client = CloudClient(server.base_url, api_key="good-key", timeout=0.2)
        assert client.health() is False
        probe = client.ping()
        assert probe["reachable"] is False
        assert "TimeoutError" in (probe["detail"] or "") or "timed out" in (
            probe["detail"] or "").lower()
        with pytest.raises(CloudUnavailable):
            client.account_capabilities()


# --------------------------------------------------------------- reconnect
def test_a_dropped_connection_fails_one_call_then_reconnects():
    with ConfigurableCloudServer() as server:
        server.cloud.drop_times = 1
        client = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        # The dropped call fails...
        with pytest.raises(CloudUnavailable):
            client.account_capabilities()
        # ...and the very next call reconnects and succeeds.
        assert client.health() is True
        assert client.account_capabilities()["llm_api_key_set"] is True


def test_a_persistently_dropped_endpoint_stays_unreachable_then_recovers():
    with ConfigurableCloudServer() as server:
        server.cloud.drop = True
        client = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        assert client.health() is False
        assert client.ping()["reachable"] is False
        # The endpoint comes back: the same client object works again, because
        # each call opens its own connection (there is no stuck session).
        server.cloud.drop = False
        assert client.health() is True


# --------------------------------------------------------- reachability vs auth
def test_reachability_is_reported_separately_from_authorization():
    """An endpoint can be reachable while the key is rejected, and vice versa."""
    with ConfigurableCloudServer() as server:
        server.cloud.required_key = "good-key"
        rejected = CloudClient(server.base_url, api_key="wrong-key", timeout=5.0)
        assert rejected.ping()["reachable"] is True    # answered (said no)
        assert rejected.health() is False              # not usable
        # A rate limit is also an *answer*: the endpoint is up.
        server.cloud.status = 429
        limited = CloudClient(server.base_url, api_key="good-key", timeout=5.0)
        probe = limited.ping()
        assert probe["reachable"] is True and probe["status"] == 429


# ------------------------------------------------------------- base url rules
@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://host/x", "not-a-url"])
def test_a_non_http_base_url_is_refused_at_construction(bad):
    with pytest.raises(CloudError):
        CloudClient(bad, api_key="k", timeout=1.0)


def test_an_empty_base_url_falls_back_to_the_default(monkeypatch):
    # An empty value is "not configured", not a bad URL: the documented default
    # is used (and it must still be an http(s) URL).
    monkeypatch.delenv("OPENHANDS_CLOUD_URL", raising=False)
    client = CloudClient("", api_key="k", timeout=1.0)
    assert client.base_url.startswith("https://")


def test_a_redirect_is_refused_and_the_key_never_leaves():
    with RedirectServer() as redirect:
        client = CloudClient(redirect.base_url, api_key="SECRET-KEY", timeout=3.0)
        with pytest.raises(CloudError):
            client._request("GET", "/api/v1/app-conversations/count")
        # The key was not re-sent to the redirect target.
        assert redirect.seen == []
