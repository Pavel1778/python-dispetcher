"""OpenHands inventory: the read-only report and the compatibility check.

Two layers are tested, both over real HTTP:

* the engine (``orchestration.openhands_inventory``) against a real fake Cloud
  server — the client speaks over a socket, nothing mocks it — including the
  *negative* cases: a service that never rejects a keyless request, a broken
  API whose ``openapi.json`` is missing the endpoints NEXUS uses, an
  unreachable host, and an agent-server whose surface is checked separately;
* the HTTP route ``GET /api/openhands/inventory`` and the ``nexus openhands
  inventory`` CLI, against the real web server and PostgreSQL, including that
  the API key never appears in the report.

Every status is earned: ``VERIFIED`` only from an observation, ``FAILED`` from a
real failure, ``BLOCKED``/``UNVERIFIED`` from a missing resource or a check that
did not run.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import cli  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.openhands_diagnostics import (  # noqa: E402
    BLOCKED,
    FAILED,
    UNVERIFIED,
    VERIFIED,
)
from orchestration.openhands_inventory import (  # noqa: E402
    AGENT_SERVER_ENDPOINTS,
    CLOUD_ENDPOINTS,
    Inventory,
)
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
KEY = "sk-openhands-inventory-never-leaked"
TOKEN = "test-token"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _status(result: dict, name: str) -> str:
    check = next(c for c in result["report"]["checks"] if c["name"] == name)
    return check["status"]


# ---------------------------------------------------------------- engine, live-ish
class TestEngineAgainstFakeCloud:
    @pytest.fixture
    def inv(self):
        with FakeCloudServer() as server:
            client = CloudClient(server.base_url, api_key=KEY, timeout=10.0)
            yield Inventory(cloud_client=client), server

    def test_api_version_and_endpoint_count(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["api"]["version"] == "1.50.1"
        assert result["api"]["openapi"] == "3.1.0"
        assert result["api"]["path_count"] == 14
        assert _status(result, "api") == VERIFIED

    def test_authorization_verified_when_keyless_is_refused(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["authorization"]["valid_key_health"] is True
        assert result["authorization"]["no_key_status"] == 401
        assert _status(result, "authorization") == VERIFIED

    def test_authorization_failed_when_keyless_is_accepted(self):
        # A server that answers 200 to a keyless request has no authorization;
        # the report must say FAILED, not VERIFIED.
        with FakeCloudServer() as server:
            server.cloud.ignore_auth = True
            client = CloudClient(server.base_url, api_key=KEY, timeout=10.0)
            result = Inventory(cloud_client=client).collect()
        assert result["authorization"]["no_key_status"] == 200
        assert _status(result, "authorization") == FAILED

    def test_limits_report_the_daily_quota(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["limits"]["quota"]["daily_limit"] == 20
        assert result["limits"]["quota"]["remaining"] == 17
        assert _status(result, "limits") == VERIFIED

    def test_tools_catalog(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert "terminal" in result["tools"]["all"]
        assert "file_editor" in result["tools"]["user_selectable"]
        assert result["tools"]["count"] == 4
        assert _status(result, "tools") == VERIFIED

    def test_compatibility_all_present(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["compatibility"]["cloud_missing"] == 0
        assert all(row["present"] for row in result["compatibility"]["cloud"])
        assert _status(result, "compatibility") == VERIFIED

    def test_compatibility_fails_when_an_endpoint_is_gone(self):
        # Remove one endpoint the client uses: the check must fail, not warn.
        with FakeCloudServer() as server:
            del server.cloud.openapi_spec["paths"]["/api/v1/app-conversations/count"]
            client = CloudClient(server.base_url, api_key=KEY, timeout=10.0)
            result = Inventory(cloud_client=client).collect()
        assert result["compatibility"]["cloud_missing"] >= 1
        assert _status(result, "compatibility") == FAILED
        missing = [r for r in result["compatibility"]["cloud"] if not r["present"]]
        assert any(r["path"] == "/api/v1/app-conversations/count" for r in missing)

    def test_availability_verified(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["availability"]["/alive"] == 200
        assert _status(result, "availability") == VERIFIED

    def test_agent_server_blocked_without_a_url(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert _status(result, "agent_server") == BLOCKED

    def test_errors_and_timeouts_section(self, inv):
        inventory, _ = inv
        result = inventory.collect()
        assert result["errors_and_timeouts"]["redirects_refused"] is True
        assert _status(result, "errors_and_timeouts") == VERIFIED

    def test_inventory_writes_nothing(self, inv):
        # It is read-only: only GETs, and no conversation is ever created.
        inventory, server = inv
        inventory.collect()
        methods = {m for m, _ in server.cloud.calls}
        assert methods == {"GET"}, methods
        assert server.cloud.conversations == {}
        assert server.cloud.start_tasks == {}


class TestEngineUnreachable:
    def test_unreachable_host_is_blocked_not_verified(self):
        client = CloudClient(base_url="http://127.0.0.1:9", api_key=KEY, timeout=2.0)
        result = Inventory(cloud_client=client).collect()
        # Nothing can be read, so nothing is claimed.
        assert _status(result, "api") == BLOCKED
        assert _status(result, "compatibility") == BLOCKED
        assert _status(result, "authorization") == BLOCKED

    def test_agent_server_checked_when_url_known(self):
        with FakeCloudServer() as cloud, FakeServer() as agent:
            client = CloudClient(cloud.base_url, api_key=KEY, timeout=10.0)
            result = Inventory(cloud_client=client,
                               agent_server_url=agent.base_url).collect()
        assert result["agent_server"]["version"] == "1.60.1"
        assert result["agent_server"]["missing"] == 0
        assert _status(result, "agent_server") == VERIFIED

    def test_agent_server_missing_endpoint_fails(self):
        with FakeCloudServer() as cloud, FakeServer():
            client = CloudClient(cloud.base_url, api_key=KEY, timeout=10.0)
            # Point at the cloud server as if it were an agent-server: its spec
            # has none of the agent-server routes, so the check must fail.
            result = Inventory(cloud_client=client,
                               agent_server_url=cloud.base_url).collect()
        assert result["agent_server"]["missing"] == len(AGENT_SERVER_ENDPOINTS)
        assert _status(result, "agent_server") == FAILED


# ------------------------------------------------------------------ HTTP route
@pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL for the HTTP route tests")
class TestHttpRoute:
    @pytest.fixture
    def base(self):
        _reset_schema()
        os.environ["OPENHANDS_API_KEY"] = KEY
        with FakeCloudServer() as server:
            app = App(DSN, poll_interval=0.2, token=TOKEN)
            app.client = CloudClient(server.base_url, api_key=KEY, timeout=10.0)
            ws = WebServer(app, "127.0.0.1", 0)
            ws.start()
            try:
                yield f"http://127.0.0.1:{ws.port}", app, server
            finally:
                ws.stop()
                app.store.close()

    def _get(self, base, path, token=TOKEN):
        req = urllib.request.Request(base + path)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")

    def test_route_requires_auth(self, base):
        url, _, _ = base
        status, _ = self._get(url, "/api/openhands/inventory", token="")
        assert status == 401

    def test_route_returns_the_report(self, base):
        url, _, server = base
        status, body = self._get(url, "/api/openhands/inventory")
        assert status == 200
        assert body["report"]["ok"] is True
        assert body["api"]["version"] == "1.50.1"
        assert body["compatibility"]["cloud_missing"] == 0
        # Read-only: only GETs reached the Cloud.
        assert {m for m, _ in server.cloud.calls} == {"GET"}

    def test_route_never_returns_the_key(self, base):
        url, _, _ = base
        _, body = self._get(url, "/api/openhands/inventory")
        assert KEY not in json.dumps(body)

    def test_cli_inventory_command(self, base, monkeypatch, capsys):
        url, _, _ = base
        monkeypatch.setenv("NEXUS_URL", url)
        monkeypatch.setenv("NEXUS_TOKEN", TOKEN)
        code = cli.main(["openhands", "inventory", "--json"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["report"]["ok"] is True
        assert out["api"]["version"] == "1.50.1"


def test_endpoint_lists_are_non_empty_and_unique():
    # Guard the contract lists themselves: a duplicate purpose would hide a
    # route from the compatibility check.
    for endpoints in (CLOUD_ENDPOINTS, AGENT_SERVER_ENDPOINTS):
        keys = [(e.method, e.path) for e in endpoints]
        assert len(keys) == len(set(keys))
        assert all(e.purpose and e.path.startswith("/") for e in endpoints)


def test_unverified_status_exists_for_an_unsupported_check():
    # ``UNVERIFIED`` must remain importable and distinct: a check that cannot be
    # run is not the same as one that failed.
    assert UNVERIFIED != FAILED
