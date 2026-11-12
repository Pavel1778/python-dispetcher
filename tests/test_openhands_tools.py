"""OpenHands tool availability: the catalog, MCP configuration and NEXUS tools.

The request behind this suite asks two separate questions and forbids
conflating them:

* which tools the **agent inside a conversation** can call — read from the
  agent-server's own tool catalog, not assumed from configuration;
* which tools **NEXUS** exposes — its MCP server tools and the ``sr_`` client
  tools, neither of which is an agent-server built-in.

Every status is earned by an observation. The catalog is read over a real
socket (``tests/fake_runtime.FakeServer``), the account settings over the real
fake Cloud, and the NEXUS tool lists from the code that serves them.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.openhands_diagnostics import (  # noqa: E402
    BLOCKED,
    FAILED,
    UNVERIFIED,
    VERIFIED,
    OpenHandsDiagnostics,
)
from orchestration.openhands_pool import AccountPool  # noqa: E402
from orchestration.openhands_tools import (  # noqa: E402
    ToolCatalogUnsupported,
    capability_report,
    missing_capabilities,
    mcp_server_names,
    nexus_tool_names,
    normalize_catalog,
)
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:alice"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


# ------------------------------------------------------- the pure normaliser
def test_normalize_catalog_reads_a_list_of_descriptors():
    tools = normalize_catalog({"tools": [
        {"name": "terminal", "user_selectable": True, "in_default_set": True, "usable": True},
        {"name": "glob", "user_selectable": True, "in_default_set": False, "usable": True},
    ]})
    assert set(tools) == {"terminal", "glob"}
    assert tools["glob"]["in_default_set"] is False


def test_normalize_catalog_reads_server_info_usable_tools():
    """``/server_info`` reports ``usable_tools``: presence proves usability."""
    tools = normalize_catalog({"usable_tools": ["terminal", "file_editor"]})
    assert tools["terminal"]["usable"] is True


def test_normalize_catalog_accepts_a_mapping_and_a_bare_string():
    tools = normalize_catalog({"tools": {"terminal": {"usable": True}, "grep": True}})
    assert set(tools) == {"terminal", "grep"}


def test_normalize_catalog_of_an_unknown_shape_is_empty_not_an_error():
    assert normalize_catalog(None) == {}
    assert normalize_catalog({"unexpected": 1}) == {}


def test_capability_report_maps_git_and_testing_onto_the_terminal():
    """Git and testing have no built-in; the agent runs them via the terminal."""
    coverage = capability_report(normalize_catalog({"usable_tools": ["terminal"]}))
    assert coverage["terminal"]["available"] is True
    assert coverage["workspace_filesystem"]["available"] is False
    assert coverage["git"]["available"] is True
    assert coverage["testing"]["available"] is True
    assert coverage["git"]["proved_by"] == ["terminal"]


def test_missing_capabilities_names_the_required_gaps():
    coverage = capability_report(normalize_catalog({"usable_tools": ["browser_tool_set"]}))
    assert set(missing_capabilities(coverage)) == {
        "terminal", "workspace_filesystem", "git", "testing"}


def test_mcp_server_names_reads_keys_never_bodies():
    settings = {"agent_settings": {"mcp_config": {
        "playwright": {"token": "SECRET"}, "fetch": {"enabled": True}}}}
    assert mcp_server_names(settings) == ["fetch", "playwright"]
    # The secret value is not in the returned data.
    assert "SECRET" not in json.dumps(mcp_server_names(settings))


def test_nexus_tool_names_are_the_two_real_surfaces():
    names = nexus_tool_names()
    assert "list_projects" in names["mcp"]
    assert "sr_create_task" in names["client_tools"]
    # The two surfaces are disjoint: a client tool is not an MCP tool.
    assert set(names["mcp"]).isdisjoint(names["client_tools"])
    # Every Sr tool keeps its prefix (a collision with a built-in is a 422).
    assert all(n.startswith("sr_") for n in names["client_tools"])


# -------------------------------------------------- the transport read itself
def test_app_transport_reports_no_tool_catalog():
    """Cloud V1's app transport has no agent-server to ask: honest BLOCKED."""
    client = CloudClient("http://127.0.0.1:1", api_key="k")
    with pytest.raises(ToolCatalogUnsupported):
        client.tool_catalog()


def test_direct_transport_reads_the_catalog_over_a_socket():
    from orchestration.cloud_direct import CloudDirectClient

    _reset()
    store = PostgresStore(DSN)
    try:
        with FakeServer() as sandbox:
            class Cloud:
                def start_sandbox(self, *, sandbox_spec_id=None):
                    return {"id": "sbx-1", "status": "RUNNING",
                            "session_api_key": sandbox.runtime.expected_key,
                            "exposed_urls": [{"name": "AGENT_SERVER",
                                              "url": sandbox.base_url, "port": 60000}]}

                @staticmethod
                def agent_server_url(info):
                    return info["exposed_urls"][0]["url"]

                def wait_for_sandbox(self, sid, *, attempts=10, interval=0.01):
                    return {"id": sid, "status": "RUNNING",
                            "session_api_key": sandbox.runtime.expected_key,
                            "exposed_urls": [{"name": "AGENT_SERVER",
                                              "url": sandbox.base_url, "port": 60000}]}

            client = CloudDirectClient(Cloud(), store, wait_interval=0.01)
            catalog = client.tool_catalog()
            assert catalog["tools"]["terminal"]["usable"] is True
            assert "file_editor" in catalog["tools"]
    finally:
        store.close()


def test_direct_transport_reports_unsupported_on_a_404_catalog():
    from orchestration.cloud_direct import CloudDirectClient

    _reset()
    store = PostgresStore(DSN)
    try:
        with FakeServer() as sandbox:
            sandbox.runtime.tool_catalog_status = 404

            class Cloud:
                def start_sandbox(self, *, sandbox_spec_id=None):
                    return {"id": "sbx-1", "status": "RUNNING",
                            "session_api_key": sandbox.runtime.expected_key,
                            "exposed_urls": [{"name": "AGENT_SERVER",
                                              "url": sandbox.base_url, "port": 60000}]}

                @staticmethod
                def agent_server_url(info):
                    return info["exposed_urls"][0]["url"]

                def wait_for_sandbox(self, sid, *, attempts=10, interval=0.01):
                    return {"id": sid, "status": "RUNNING",
                            "session_api_key": sandbox.runtime.expected_key,
                            "exposed_urls": [{"name": "AGENT_SERVER",
                                              "url": sandbox.base_url, "port": 60000}]}

            client = CloudDirectClient(Cloud(), store, wait_interval=0.01)
            with pytest.raises(ToolCatalogUnsupported):
                client.tool_catalog()
    finally:
        store.close()


# ------------------------------------------------------- the diagnostics checks
@pytest.fixture
def store():
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def clouds():
    with FakeCloudServer() as a:
        a.cloud.settings = {
            "llm_api_key_set": True, "active_agent_profile_id": None,
            "agent_settings": {"mcp_config": {"playwright": {"enabled": True},
                                              "fetch": {"enabled": True}}},
            "registered_marketplaces": [], "inherited_marketplaces": []}
        yield a


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _diag(store, service):
    return OpenHandsDiagnostics(
        store, accounts=service, pool=AccountPool(store, service),
        cloud_client=CloudClient(base_url="http://127.0.0.1:1", api_key="sk", timeout=2.0),
        client_factory=lambda u, k: CloudClient(base_url=u, api_key=k, timeout=5.0))


class ToolClient:
    """A live client whose catalog and settings a test controls."""

    supports_files = True

    def __init__(self, catalog, settings):
        self._catalog = catalog
        self._settings = settings

    def tool_catalog(self):
        if isinstance(self._catalog, Exception):
            raise self._catalog
        return {"tools": self._catalog, "raw": {}}

    def get_settings(self):
        return self._settings


def test_tools_check_is_unverified_unless_requested(store, service, clouds):
    diag = _diag(store, service)
    from orchestration.openhands_diagnostics import Report

    report = Report()
    diag.check_openhands_tools(report, PRINCIPAL, live=False, account_id=None,
                               project_id=None)
    assert report.by_name("openhands_tools").status == UNVERIFIED


def test_tools_check_verifies_a_full_catalog(store, service, clouds):
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    catalog = normalize_catalog({"tools": [
        {"name": "terminal", "usable": True},
        {"name": "file_editor", "usable": True},
        {"name": "browser_tool_set", "usable": True},
    ]})
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient(catalog, {})
    report = diag.run(PRINCIPAL, live=True)
    check = report.by_name("openhands_tools")
    assert check.status == VERIFIED
    assert check.evidence["capabilities"]["browser"] is True
    assert check.evidence["missing_required"] == []


def test_tools_check_fails_when_a_required_capability_is_missing(store, service, clouds):
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    # Only the browser tool: terminal, workspace, git and testing are absent.
    catalog = normalize_catalog({"tools": [{"name": "browser_tool_set", "usable": True}]})
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient(catalog, {})
    report = diag.run(PRINCIPAL, live=True)
    check = report.by_name("openhands_tools")
    assert check.status == FAILED
    assert set(check.evidence["missing_required"]) == {
        "terminal", "workspace_filesystem", "git", "testing"}
    assert not report.ok


def test_tools_check_is_blocked_when_the_catalog_is_unsupported(store, service, clouds):
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient(
        ToolCatalogUnsupported("no catalog"), {})
    report = diag.run(PRINCIPAL, live=True)
    check = report.by_name("openhands_tools")
    assert check.status == BLOCKED
    # The absent catalog is not a proven failure of the tools check itself.
    assert check.status != FAILED


def test_tools_check_is_blocked_without_an_account_and_no_transport(store, service, clouds):
    """No stored account and no process transport: BLOCKED, not FAILED."""
    diag = _diag(store, service)
    diag._cloud = ToolClient(ToolCatalogUnsupported("no catalog"), {})
    report = diag.run(PRINCIPAL, live=True)
    assert report.by_name("openhands_tools").status == BLOCKED


def test_mcp_configuration_verifies_configured_servers(store, service, clouds):
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient({}, clouds.cloud.settings)
    report = diag.run(PRINCIPAL)
    check = report.by_name("mcp_configuration")
    assert check.status == VERIFIED
    assert check.evidence["mcp_servers"] == ["fetch", "playwright"]


def test_mcp_configuration_is_blocked_without_servers(store, service, clouds):
    clouds.cloud.settings["agent_settings"] = {"mcp_config": {}}
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient({}, clouds.cloud.settings)
    report = diag.run(PRINCIPAL)
    assert report.by_name("mcp_configuration").status == BLOCKED


def test_nexus_tools_check_names_both_surfaces(store, service, clouds):
    diag = _diag(store, service)
    report = diag.run(PRINCIPAL)
    check = report.by_name("nexus_tools")
    assert check.status == VERIFIED
    assert "sr_create_task" in check.evidence["client_tools"]
    assert "list_projects" in check.evidence["mcp"]


def test_the_report_separates_agent_tools_from_nexus_tools(store, service, clouds):
    """Both checks are present and distinct — the whole point of the request."""
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=clouds.base_url,
                transport="app", make_default=True)
    diag = _diag(store, service)
    diag._live_factory = lambda account_id: ToolClient(
        normalize_catalog({"usable_tools": ["terminal", "file_editor"]}),
        clouds.cloud.settings)
    report = diag.run(PRINCIPAL, live=True)
    names = {c.name for c in report.checks}
    assert {"openhands_tools", "mcp_configuration", "nexus_tools"} <= names
    assert report.by_name("openhands_tools").evidence["account"] == "A"
