"""OpenHands model and Base URL must persist and reach every new session.

These are the defects the mission brief calls critical: "OpenHands must stop
losing or forgetting its model and Base URL configuration". Two root causes were
found by reading the code, and each has a test here that fails if the fix is
undone:

1. **Re-adding a connection wiped its model.** ``OpenHandsAccountService.add``
   upserts on ``(principal, label)`` with ``default_model=EXCLUDED.default_model``.
   A reconnect or a re-add that did not repeat ``default_model`` wrote ``NULL``
   over the saved model, so the next session fell back to the sandbox default
   (whose key may be absent). A field the caller did not name must not be
   cleared.

2. **The ``code`` workflow path ignored the stored connection entirely.** Sr/Jr
   conversations were started on the process-wide ``build_cloud_client()``, so
   the account's base URL and model never reached them, whatever the operator
   saved. The workflow path now resolves the project's bound account.

Both are exercised over real PostgreSQL and the real fake Cloud server (the
client speaks HTTP over a socket; nothing mocks the client).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:alice"
KEY = "sk-openhands-persist-secret-value"


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
            "llm_api_key_set": True, "active_agent_profile_id": "profile-persist",
            "agent_settings": {"mcp_config": {}},
            "registered_marketplaces": [], "inherited_marketplaces": [],
        }
        yield server


@pytest.fixture
def service(store, cloud, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "persistence-test-secret-key")
    return OpenHandsAccountService(store)


# ---------------------------------------------------------------- defect 1
def test_readding_a_connection_without_a_model_keeps_the_saved_model(service, cloud):
    """A reconnect that omits ``default_model`` must not clear it.

    This is the "OpenHands reconnect must not overwrite the user's saved
    configuration with defaults" rule. Before the fix the upsert wrote
    ``default_model=EXCLUDED.default_model`` (NULL), and the next session
    silently lost the model.
    """
    first = service.add(PRINCIPAL, label="primary", api_key=KEY,
                        base_url=cloud.base_url, transport="direct",
                        default_model="openhands/deepseek-v4.1-flash")
    assert first["default_model"] == "openhands/deepseek-v4.1-flash"

    # Re-add the same connection (a reconnect / re-entry of the same form) with
    # no model named. The saved model must survive.
    again = service.add(PRINCIPAL, label="primary", api_key=KEY,
                        base_url=cloud.base_url, transport="direct")
    assert again["id"] == first["id"]
    assert again["default_model"] == "openhands/deepseek-v4.1-flash", (
        "re-adding a connection without a model wiped the saved model"
    )


def test_readding_with_an_explicit_new_model_updates_it(service, cloud):
    """The persistence must not freeze a model the operator *did* change."""
    service.add(PRINCIPAL, label="primary", api_key=KEY, base_url=cloud.base_url,
                default_model="openhands/one")
    updated = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, default_model="openhands/two")
    assert updated["default_model"] == "openhands/two"


def test_base_url_is_kept_when_a_readd_names_the_same_endpoint(service, cloud):
    """The base URL, too, survives a reconnect that names it again."""
    service.add(PRINCIPAL, label="primary", api_key=KEY, base_url=cloud.base_url,
                default_model="openhands/x")
    again = service.add(PRINCIPAL, label="primary", api_key=KEY,
                        base_url=cloud.base_url)
    assert again["base_url"] == cloud.base_url
    assert again["default_model"] == "openhands/x"


# ---------------------------------------------------------------- defect 2
def test_the_stored_connection_is_what_a_workflow_uses(store, cloud, service):
    """A project's ``code`` workflow resolves the stored account's config.

    The workflow path must not fall back to the process-wide client when a
    connection is stored: the base URL and model the operator saved are what a
    new session gets. ``execution_client_internal`` is the one builder.
    """
    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    client = service.execution_client_internal(account["id"])
    # The direct transport carries the stored model on every sandbox it starts.
    assert getattr(client, "llm_model", None) == "openhands/deepseek-v4.1-flash"
    # And it speaks to the stored base URL, not an environment default.
    assert cloud.base_url in getattr(client.cloud, "base_url", "") or \
        getattr(client.cloud, "base_url", "") == cloud.base_url


def test_a_new_project_binding_resolves_the_same_connection(store, cloud, service):
    """A newly bound project gets the connection's stored config automatically."""
    from orchestration.nexus_projects import ProjectService  # noqa: E402

    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    project = ProjectService(store).create(PRINCIPAL, name="demo")
    service.bind_project(PRINCIPAL, project["id"], account["id"])
    resolved = service.execution_client_internal(account["id"])
    assert getattr(resolved, "llm_model", None) == "openhands/deepseek-v4.1-flash"


def test_a_code_workflow_runs_on_the_stored_connection(store, cloud, service):
    """A ``code`` turn's Sr is driven on the project's bound connection.

    The workflow path used the process-wide client, so the account's base URL
    and model never reached a Sr/Jr conversation. ``App._workflow_client`` now
    resolves the bound account; the resolver is what ``SrRuntime`` calls.
    """
    from orchestration.nexus_projects import ProjectService  # noqa: E402
    from orchestration.web_server import App  # noqa: E402

    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    project = ProjectService(store).create(PRINCIPAL, name="demo")
    service.bind_project(PRINCIPAL, project["id"], account["id"])

    app = App.__new__(App)
    app.store = store
    app.openhands_accounts = service
    from orchestration.nexus_projects import ProjectService as PS
    app.projects = PS(store)
    # A workflow with no project: the resolver reports "not found" so SrRuntime
    # falls back to the process client - never a silent switch to some account.
    from orchestration.cloud_store import NotFound  # noqa: E402
    with pytest.raises(NotFound):
        app._workflow_client("00000000-0000-0000-0000-000000000000")

    # A workflow in the bound project resolves to that account's client.
    workflow_id, _ = store.create_workflow_once("do a thing", project_id=project["id"])
    client = app._workflow_client(workflow_id)
    assert getattr(client, "llm_model", None) == "openhands/deepseek-v4.1-flash"



# ---------------------------------------------------------- restart survival
def test_the_model_and_base_url_survive_a_backend_restart(store, cloud):
    """The saved model and base URL survive a process restart.

    This is the mission's acceptance criterion: "no routine restart, reconnect
    or redeployment should require manual reconfiguration of the model or Base
    URL". The configuration lives in PostgreSQL, so a fresh process reads back
    exactly what was stored. A second ``PostgresStore`` on the same database
    stands in for the restarted backend; the account is resolved through a
    fresh service instance, not the one that wrote it.
    """
    import os as _os

    _os.environ["NEXUS_SECRET_KEY"] = "persistence-test-secret-key"
    first = OpenHandsAccountService(store)
    account = first.add(PRINCIPAL, label="primary", api_key=KEY,
                        base_url=cloud.base_url, transport="direct",
                        default_model="openhands/deepseek-v4.1-flash")

    # A new store + service = a restarted backend reading the same database.
    restarted_store = PostgresStore(DSN)
    try:
        restarted = OpenHandsAccountService(restarted_store)
        listed = restarted.list_accounts(PRINCIPAL)
        assert len(listed) == 1
        recovered = listed[0]
        assert recovered["id"] == account["id"]
        assert recovered["default_model"] == "openhands/deepseek-v4.1-flash"
        assert recovered["base_url"] == cloud.base_url
        # And the recovered connection builds a client that carries the model.
        client = restarted.execution_client_internal(account["id"])
        assert getattr(client, "llm_model", None) == "openhands/deepseek-v4.1-flash"
    finally:
        restarted_store.close()


def test_an_edit_that_rotates_the_key_keeps_the_model_and_url(store, cloud, service):
    """Rotating the API key does not lose the other configuration fields."""
    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    rotated = service.rotate(PRINCIPAL, account["id"], api_key=KEY + "-new")
    assert rotated["default_model"] == "openhands/deepseek-v4.1-flash"
    assert rotated["base_url"] == cloud.base_url
    # The hint shows a *different* key is stored, without revealing it.
    assert KEY not in rotated.get("api_key_hint", "")


def test_the_model_never_appears_as_a_secret_and_the_key_never_leaks(store, cloud, service):
    """The stored view carries the model but never the API key."""
    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    for view in (account, *service.list_accounts(PRINCIPAL)):
        assert view["default_model"] == "openhands/deepseek-v4.1-flash"
        assert KEY not in str(view)
        assert "api_key_encrypted" not in view


# ------------------------------------------------------- project isolation
def test_the_workflow_resolver_cannot_reach_another_projects_account(store, cloud, service):
    """A workflow resolves only its *own* project's bound account.

    Security half of Defect 2: the per-workflow resolver must not let project B
    inherit project A's connection. Two projects, only A bound; a workflow in B
    gets NotFound (fall back to the process client), never A's account.
    """
    from orchestration.cloud_store import NotFound  # noqa: E402
    from orchestration.nexus_projects import ProjectService  # noqa: E402
    from orchestration.web_server import App  # noqa: E402

    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    projects = ProjectService(store)
    project_a = projects.create(PRINCIPAL, name="a")
    project_b = projects.create(PRINCIPAL, name="b")
    service.bind_project(PRINCIPAL, project_a["id"], account["id"])
    # B is deliberately left unbound.

    app = App.__new__(App)
    app.store = store
    app.openhands_accounts = service
    app.projects = projects
    wf_b, _ = store.create_workflow_once("work in b", project_id=project_b["id"])
    with pytest.raises(NotFound):
        app._workflow_client(wf_b)


# ------------------------------------------------ one bad connection != dead loop
def test_a_revoked_connection_does_not_stop_the_sr_tick(store, cloud, service):
    """A workflow whose connection is unusable is reported, not fatal to the tick.

    ``SrRuntime.tick`` drives every workflow's Sr. If one workflow's bound
    connection raises (revoked account -> ValueError, vanished -> NotFound), the
    loop must report that workflow unavailable and keep going, never abort the
    whole pass and leave every other workflow unserviced.
    """
    from orchestration.cloud_client import CloudClient  # noqa: E402
    from orchestration.nexus_projects import ProjectService  # noqa: E402
    from orchestration.sr_runtime import SrRuntime  # noqa: E402
    from orchestration.sr_tools import SrTools  # noqa: E402

    account = service.add(PRINCIPAL, label="primary", api_key=KEY,
                          base_url=cloud.base_url, transport="direct",
                          default_model="openhands/deepseek-v4.1-flash")
    project = ProjectService(store).create(PRINCIPAL, name="p")
    service.bind_project(PRINCIPAL, project["id"], account["id"])
    wf, _ = store.create_workflow_once("do", project_id=project["id"])
    store.bind_sr(wf, conversation_id="conv-x", status="running")
    service.revoke(PRINCIPAL, account["id"])

    # A resolver that raises the way a revoked account does.
    def boom(_wf):
        raise ValueError("account is revoked")

    rt = SrRuntime(store, CloudClient(cloud.base_url, api_key=KEY),
                   SrTools(store, None), client_for_workflow=boom)
    report = rt.tick()  # must not raise
    assert wf in report.unavailable


@pytest.mark.parametrize('managed', [False, True], ids=['poll-accepted-start', 'enforce-project-profile'])
def test_sr_create_and_poll_obey_the_selected_connections_profile(store, cloud, service, managed):
    from orchestration.cloud_client import CloudClient
    from orchestration.execution_profiles import ExecutionProfiles, ProfileBlocked
    from orchestration.nexus_projects import ProjectService
    from orchestration.sr_runtime import SrRuntime
    from orchestration.sr_tools import SrTools
    from tests.test_execution_profile_barriers import desired

    account = service.add(PRINCIPAL, label='bound', api_key=KEY, base_url=cloud.base_url, transport='app')
    project = str(ProjectService(store).create(PRINCIPAL, 'selected connection')['id'])
    wid, _ = store.create_workflow_once('inspect', project_id=project)
    selected = service.execution_client_for_account(PRINCIPAL, account['id'])
    if managed:
        ExecutionProfiles(store).save(PRINCIPAL, desired(connection_id=str(account['id'])), project_id=project)
    cloud.cloud.start_polls_required = 1  # READY must be read on this connection, not the process client
    with FakeCloudServer() as other:
        process_client = CloudClient(other.base_url, api_key='other')
        runtime = SrRuntime(store, process_client, SrTools(store, None), start_poll_interval=0.01,
                            client_for_workflow=lambda _: selected)
        if managed:
            # A managed App start is unsupported. Passing no project here would
            # incorrectly bypass the barrier and start an unmanaged session.
            with pytest.raises(ProfileBlocked):
                runtime.ensure_sr(wid)
            assert not cloud.cloud.start_bodies
        else:
            cid = runtime.ensure_sr(wid)
            assert cid == store.sr_for(wid)['sr_conversation_id'] and cid in cloud.cloud.conversations
        assert other.cloud.calls == []
