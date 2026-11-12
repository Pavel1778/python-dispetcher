"""Several OpenHands connections, and which of them a project runs on.

Real PostgreSQL; each connection is a real socket server (``tests/fake_cloud``)
reached through the real client, the real executor, the real router and the
real scheduler. Nothing here is a mock of the code under test.

Shown here:

* the address guard: a URL added through the API cannot make this server talk
  to localhost, a private network or the metadata address - when it is saved
  and again at the moment of connecting;
* two connections are independent: each is checked with its own key, and one
  that is down or rejected says nothing about the other;
* a check reports the service's version and whether it has the API this code
  calls;
* credentials are changed only after the new ones were seen to work, and are
  never given back;
* a connection is removed only when nothing leans on it - or by force, and
  then the projects and the history stay consistent;
* a project with a list of executors runs on that list and nothing else; the
  primary first, a backup while it is down, the primary again when it is back;
  independent tasks may be spread over the list.
"""
from __future__ import annotations

import json
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import net_guard  # noqa: E402
from orchestration.cloud_client import CloudClient, CloudError  # noqa: E402
from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.net_guard import AddressPolicy, AddressRefused, check_url  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_executors import ProjectExecutorService  # noqa: E402
from orchestration.nexus_lab import LabScheduler, OpenHandsExecutor  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import (  # noqa: E402
    ConnectionInUse,
    OpenHandsAccountService,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.openhands_pool import AccountPool  # noqa: E402
from orchestration.openhands_router import AccountRouter, NoUsableAccount  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.test_durable_runs import DSN, reset_schema, sql  # noqa: E402
from tests.test_openhands_failover import StubVerifier  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

OWNER = "user:owner"
SETTINGS = {"llm_api_key_set": True, "active_agent_profile_id": "p",
            "agent_settings": {"mcp_config": {}},
            "registered_marketplaces": [], "inherited_marketplaces": []}


# ============================================================ the address guard
@pytest.mark.parametrize("address, word", [
    ("127.0.0.1", "loopback"), ("127.8.9.10", "loopback"), ("::1", "loopback"),
    ("10.0.0.5", "private"), ("172.16.3.4", "private"), ("192.168.1.1", "private"),
    ("169.254.169.254", "link-local"), ("fe80::1", "link-local"),
    ("100.64.0.1", "not a public"), ("fc00::1", "private"), ("fd12:3456::1", "private"),
    ("0.0.0.0", "unspecified"), ("::", "unspecified"), ("224.0.0.1", "multicast"),
    ("::ffff:127.0.0.1", "loopback"), ("::ffff:10.0.0.1", "private"),
    ("2002:7f00:0001::", "loopback"),                  # 6to4 wrapping 127.0.0.1
    ("240.0.0.1", "private"), ("not-an-address", "not an IP"),
])
def test_addresses_that_are_not_public_are_named_as_such(address, word):
    assert word in net_guard.private_reason(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "140.82.112.3",
                                     "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_pass(address):
    assert net_guard.private_reason(address) == ""


@pytest.mark.parametrize("url, why", [
    ("", "empty"), ("   ", "empty"), (None, "empty"),
    ("ftp://example.org", "https://"), ("file:///etc/passwd", "https://"),
    ("javascript:alert(1)", "https://"), ("//example.org", "https://"),
    ("https://user:secret@8.8.8.8", "user name or a password"),
    ("https://8.8.8.8/?next=http://127.0.0.1", "query"),
    ("https://8.8.8.8/#frag", "query or a fragment"),
    ("https://", "no host"),
    ("https://8.8.8.8/a b", "characters"), ("https://8.8.8.8/\r\nHost: x", "characters"),
    ("http://127.0.0.1:5432", "loopback"), ("http://localhost:8000", "loopback"),
    ("http://[::1]:8000", "loopback"), ("http://0.0.0.0", "unspecified"),
    ("http://169.254.169.254/latest/meta-data", "link-local"),
    ("http://10.1.2.3:3000", "private"), ("https://192.168.0.10", "private"),
    ("http://2130706433", "loopback"),                 # 127.0.0.1 as one number
    ("http://0x7f.0.0.1", "loopback"), ("http://127.1", "loopback"),
    ("http://[::ffff:127.0.0.1]", "loopback"),
])
def test_urls_a_connection_may_not_have(url, why):
    with pytest.raises(AddressRefused, match=why):
        check_url(url, policy=AddressPolicy())


def test_a_url_that_is_allowed_comes_back_tidy():
    policy = AddressPolicy()
    assert check_url("https://8.8.8.8/", policy=policy) == "https://8.8.8.8"
    assert check_url(" https://8.8.8.8:8443/api/ ", policy=policy) == "https://8.8.8.8:8443/api"
    # The operator's own decisions, made in the server's environment.
    assert check_url("http://127.0.0.1:3000", policy=AddressPolicy(allow_private=True)) == (
        "http://127.0.0.1:3000")
    named = AddressPolicy(allowed_hosts=frozenset({"127.0.0.1:3000", "openhands.internal"}))
    assert check_url("http://127.0.0.1:3000", policy=named) == "http://127.0.0.1:3000"
    with pytest.raises(AddressRefused):
        check_url("http://127.0.0.1:3001", policy=named)


def test_the_policy_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("NEXUS_OPENHANDS_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("NEXUS_OPENHANDS_ALLOWED_HOSTS", raising=False)
    assert AddressPolicy.from_env() == AddressPolicy()
    monkeypatch.setenv("NEXUS_OPENHANDS_ALLOWED_HOSTS", " OpenHands.Internal , 10.0.0.5:3000 ")
    policy = AddressPolicy.from_env()
    assert policy.exempt("openhands.internal", 443) and policy.exempt("10.0.0.5", 3000)
    assert not policy.exempt("10.0.0.5", 22) and not policy.allow_private
    monkeypatch.setenv("NEXUS_OPENHANDS_ALLOW_PRIVATE", "yes")
    assert AddressPolicy.from_env().allow_private


def test_a_name_that_changes_its_address_after_it_was_saved_is_refused_when_connecting(
        monkeypatch):
    """DNS rebinding. The name resolved to a public address when the
    connection was checked; a moment later it resolves to this machine. The
    socket is opened to the address that is checked - and that one is refused."""
    with FakeCloudServer() as inside:
        port = int(inside.base_url.rsplit(":", 1)[1])
        answers = iter(["8.8.8.8"])
        real = socket.getaddrinfo

        def resolver(host, *args, **kwargs):
            if host == "rebind.example":
                address = next(answers, "127.0.0.1")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
            return real(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)
        url = f"http://rebind.example:{port}"
        assert check_url(url, policy=AddressPolicy()) == url        # public at that moment
        client = CloudClient(base_url=url, api_key="k", timeout=3.0,
                             address_policy=AddressPolicy())
        with pytest.raises(CloudError, match="loopback"):
            client.account_capabilities()
        assert inside.cloud.calls == [], "the request reached a server on this machine"


def test_a_redirect_cannot_carry_a_request_inward():
    """A host that is allowed answers "go to http://127.0.0.1...". The client
    does not follow redirects at all, so the second server is never asked."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    with FakeCloudServer() as inside:
        class Bounce(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", inside.base_url + "/api/v1/settings")
                self.end_headers()

            def log_message(self, *args):
                pass

        outside = ThreadingHTTPServer(("127.0.0.1", 0), Bounce)
        threading.Thread(target=outside.serve_forever, daemon=True).start()
        try:
            policy = AddressPolicy(allowed_hosts=frozenset(
                {f"127.0.0.1:{outside.server_port}"}))
            client = CloudClient(base_url=f"http://127.0.0.1:{outside.server_port}",
                                 api_key="k", timeout=3.0, address_policy=policy)
            with pytest.raises(CloudError):
                client.account_capabilities()
            assert inside.cloud.calls == []
        finally:
            outside.shutdown()
            outside.server_close()


# ================================================================ connections
@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "connections-test-secret")
    reset_schema()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def clouds():
    with FakeCloudServer() as a, FakeCloudServer() as b, FakeCloudServer() as c:
        for server in (a, b, c):
            server.cloud.settings = dict(SETTINGS)
        yield a, b, c


@pytest.fixture
def service(store):
    return OpenHandsAccountService(store)


def test_a_connection_to_an_inner_address_is_refused_before_anything_is_sent(
        store, clouds, monkeypatch):
    monkeypatch.delenv("NEXUS_OPENHANDS_ALLOW_PRIVATE", raising=False)
    a, _b, _c = clouds
    service = OpenHandsAccountService(store)
    for url in (a.base_url, "http://169.254.169.254/latest/meta-data", "http://localhost:5432",
                "https://user:pw@8.8.8.8", "gopher://8.8.8.8"):
        with pytest.raises(ValueError, match="API URL is not allowed"):
            service.add(OWNER, label="inner", api_key="sk-secret-key-value", base_url=url)
    assert service.list_accounts(OWNER) == [] and a.cloud.calls == []

    # The operator names the host: now it is a connection like any other...
    monkeypatch.setenv("NEXUS_OPENHANDS_ALLOWED_HOSTS", a.base_url.split("//", 1)[1])
    added = service.add(OWNER, label="inner", api_key="sk-secret-key-value", base_url=a.base_url)
    assert added["state"] == "active" and a.cloud.calls
    # ...and it cannot be pointed anywhere else afterwards.
    _a, b, _c = clouds
    before = list(b.cloud.calls)
    with pytest.raises(ValueError, match="API URL is not allowed"):
        service.edit(OWNER, added["id"], base_url=b.base_url)
    assert b.cloud.calls == before
    assert service.get(OWNER, added["id"])["base_url"] == a.base_url


def test_two_connections_are_checked_each_with_its_own_key(service, clouds):
    a, b, _c = clouds
    first = service.add(OWNER, label="основной", api_key="sk-first-key-0001", base_url=a.base_url,
                        transport="app")
    second = service.add(OWNER, label="резервный", api_key="sk-second-key-0002",
                         base_url=b.base_url, transport="app")
    assert {first["state"], second["state"]} == {"active"}
    assert (first["health"], first["api_version"], first["compatibility"]) == (
        "healthy", "1.50.1", "compatible")
    assert first["api_key_hint"] == "sk-f…0001" and second["api_key_hint"] == "sk-s…0002"

    # The second service goes down: only the second connection says so.
    b.cloud.settings_status = 503
    checked = service.verify(OWNER, second["id"])
    assert (checked["state"], checked["health"]) == ("active", "unavailable")
    assert "unavailable" in checked["last_error"]
    assert service.verify(OWNER, first["id"])["health"] == "healthy"

    # Its key is rejected: it becomes invalid; the first is untouched.
    b.cloud.settings_status = 401
    checked = service.verify(OWNER, second["id"])
    assert (checked["state"], checked["health"]) == ("invalid", "unavailable")
    listed = {item["label"]: item for item in service.list_accounts(OWNER)}
    assert listed["основной"]["state"] == "active"
    assert listed["резервный"]["state"] == "invalid"

    # And it comes back when the service takes the key again.
    b.cloud.settings_status = 0
    with pytest.raises(ValueError, match="invalid"):
        service.verify(OWNER, second["id"])        # an invalid key is not used to ask
    restored = service.edit(OWNER, second["id"], api_key="sk-second-key-0003")
    assert (restored["state"], restored["health"]) == ("active", "healthy")

    # Nothing that is returned holds a key.
    text = json.dumps(service.list_accounts(OWNER), default=str)
    for secret in ("sk-first-key-0001", "sk-second-key-0002", "sk-second-key-0003"):
        assert secret not in text
    stored = sql({"store": service.store},
                 "SELECT api_key_encrypted FROM nexus_openhands_accounts")
    assert all(b"sk-" not in bytes(row["api_key_encrypted"]) for row in stored)


def test_a_check_reports_the_version_and_whether_the_api_is_there(service, clouds):
    a, b, c = clouds
    b.cloud.api_version = "0.9.0"
    b.cloud.api_paths_missing = {"/api/v1/app-conversations/start-tasks",
                                 "/api/v1/sandboxes/{sandbox_id}/pause"}
    c.cloud.api_version = None                         # publishes no description
    views = [service.add(OWNER, label=label, api_key=f"sk-key-for-{label}-0000",
                         base_url=server.base_url)
             for label, server in (("new", a), ("old", b), ("silent", c))]
    new, old, silent = views
    assert (new["api_version"], new["compatibility"]) == ("1.50.1", "compatible")
    assert (old["api_version"], old["compatibility"]) == ("0.9.0", "incompatible")
    assert "/api/v1/app-conversations/start-tasks" in old["compatibility_detail"]
    assert "/api/v1/sandboxes/{}/pause" in old["compatibility_detail"]
    assert old["state"] == "active", "what it lacks is said; whether to use it is a person's call"
    assert (silent["api_version"], silent["compatibility"]) == (None, "unknown")
    assert "does not publish" in silent["compatibility_detail"]

    # The service is upgraded: the next check sees it.
    b.cloud.api_version, b.cloud.api_paths_missing = "1.60.0", set()
    again = service.verify(OWNER, old["id"])
    assert (again["api_version"], again["compatibility"]) == ("1.60.0", "compatible")
    assert again["health_checked_at"] is not None


def test_credentials_change_only_when_the_new_ones_work_and_never_come_back(service, clouds):
    a, b, _c = clouds
    account = service.add(OWNER, label="main", api_key="sk-old-key-000001", base_url=a.base_url)

    # A new key the service rejects: nothing is saved.
    a.cloud.settings_status = 401
    with pytest.raises(ValueError, match="new key is rejected"):
        service.edit(OWNER, account["id"], api_key="sk-bad-key-000002")
    a.cloud.settings_status = 0
    assert service.get(OWNER, account["id"])["api_key_hint"] == "sk-o…0001"

    # Moved to another instance, which has another key: one change. Either
    # half alone would be refused - the old key by the new service, the new
    # key by the old one.
    b.cloud.settings = {**SETTINGS, "active_agent_profile_id": "other"}
    moved = service.edit(OWNER, account["id"], base_url=b.base_url,
                         api_key="sk-new-key-000003", label="moved")
    assert (moved["base_url"], moved["label"], moved["api_key_hint"]) == (
        b.base_url, "moved", "sk-n…0003")
    assert moved["state"] == "active" and moved["health"] == "healthy"
    assert "api_key" not in moved and "api_key_encrypted" not in moved
    assert "sk-new-key-000003" not in json.dumps(moved, default=str)
    # The client built for it afterwards signs with the new key, at the new URL.
    client = service.client_for_account(OWNER, account["id"])
    assert client.base_url == b.base_url
    client.account_capabilities()

    # A name another connection has is refused in words, not by the database.
    service.add(OWNER, label="second", api_key="sk-other-key-00004", base_url=a.base_url)
    with pytest.raises(ValueError, match="already named"):
        service.edit(OWNER, account["id"], label="second")
    # Somebody else's connection is not there to be edited.
    with pytest.raises(NotFound):
        service.edit("user:mallory", account["id"], label="mine now")
    assert service.edit(OWNER, account["id"])["label"] == "moved"      # nothing to change


def _project(store, name="Shop") -> str:
    return str(ProjectService(store).create(OWNER, name)["id"])


def test_a_connection_in_use_is_not_removed_until_nothing_leans_on_it(store, service, clouds):
    a, b, _c = clouds
    first = service.add(OWNER, label="A", api_key="sk-key-a-00000001", base_url=a.base_url)
    second = service.add(OWNER, label="B", api_key="sk-key-b-00000002", base_url=b.base_url)
    project = _project(store)
    executors = ProjectExecutorService(store, service)
    executors.set(OWNER, project, executors=[first["id"], second["id"]])
    assert service.dependencies(OWNER, first["id"]) == {
        "active_tasks": 0, "pending_stops": 0,
        "projects": [{"id": project, "name": "Shop", "role": "primary"}]}

    with pytest.raises(ConnectionInUse) as refused:
        service.remove(OWNER, first["id"])
    assert "executor of: Shop" in str(refused.value)
    assert refused.value.dependencies["projects"][0]["role"] == "primary"
    assert len(service.list_accounts(OWNER)) == 2

    # Taken off the project's list, it can go.
    executors.remove(OWNER, project, first["id"])
    assert [e["label"] for e in executors.get(OWNER, project)["executors"]] == ["B"]
    service.remove(OWNER, first["id"])
    assert [a["label"] for a in service.list_accounts(OWNER)] == ["B"]

    # By force: the project loses it, and its list stays consistent.
    removed = service.remove(OWNER, second["id"], force=True)
    assert removed["projects"][0]["name"] == "Shop"
    state = executors.get(OWNER, project)
    assert state["executors"] == [] and state["mode"] == "default"
    bound = sql({"store": store}, "SELECT openhands_account_id FROM nexus_projects "
                                  "WHERE id=%s", project)
    assert bound[0]["openhands_account_id"] is None


# ========================================================= a project's executors
@pytest.fixture
def lab(store, service, clouds):
    """A project with a repository, three connections (A, B, C) and what runs
    tasks on them: the real executor over HTTP, the real router."""
    a, b, c = clouds
    accounts = {label: service.add(OWNER, label=label, api_key=f"sk-key-{label}-00000000",
                                   base_url=server.base_url, transport="app")
                for label, server in (("A", a), ("B", b), ("C", c))}
    project = _project(store)
    store.add_repository("acme/shop", default_branch="main", allowed_branches=["main"])
    ProjectRepositoryService(store).link(OWNER, project, "acme/shop")
    lab_store = LabStore(store)
    router = AccountRouter(store, pool=AccountPool(store, service), lab=lab_store)
    executor = OpenHandsExecutor(
        None, router=router, poll_interval=0.01,
        client_factory=lambda account_id, task=None: service.execution_client_internal(
            account_id))
    scheduler = LabScheduler(lab_store, executor, StubVerifier(), router=router)
    return {"store": store, "service": service, "accounts": accounts, "project": project,
            "lab": lab_store, "router": router, "scheduler": scheduler,
            "executors": ProjectExecutorService(store, service),
            "clouds": {"A": a, "B": b, "C": c}}


def _run(lab, keys, *, project=None, dependencies=None) -> str:
    project = project or lab["project"]
    goal = ProjectService(lab["store"]).create_goal(OWNER, project, "Do it")[0]
    run = lab["lab"].start_run(OWNER, project, str(goal["id"]), policy="durable")
    for key in keys:
        lab["lab"].add_task(OWNER, project, run["id"], task_key=key, title=key,
                            dependencies=(dependencies or {}).get(key))
    return str(run["id"])


def _where(lab, run_id) -> dict[str, str]:
    """Which connection each task of the run is at work on, by label."""
    by_id = {a["id"]: label for label, a in lab["accounts"].items()}
    return {t["task_key"]: by_id.get(t["openhands_account_id"], "")
            for t in lab["lab"].tasks_internal(run_id)
            if t["state"] == "running" and t["openhands_account_id"]}


def _started_on(lab, label) -> int:
    return len(lab["clouds"][label].cloud.conversations)


def test_a_project_runs_only_on_the_executors_it_was_given(lab):
    other = _project(lab["store"], "Other")
    ids = {label: a["id"] for label, a in lab["accounts"].items()}
    state = lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], ids["B"]])
    assert [(e["label"], e["role"], e["position"]) for e in state["executors"]] == [
        ("A", "primary", 0), ("B", "fallback", 1)]
    assert [a["label"] for a in state["available"]] == ["C"]
    assert state["mode"] == "list" and state["can_edit"] is True
    lab["executors"].set(OWNER, other, executors=[ids["C"]])

    for _ in range(3):
        lab["scheduler"].tick(_run(lab, ["one"]))
    assert (_started_on(lab, "A"), _started_on(lab, "B"), _started_on(lab, "C")) == (3, 0, 0)
    lab["scheduler"].tick(_run(lab, ["one"], project=other))
    assert _started_on(lab, "C") == 1 and _started_on(lab, "A") == 3

    # Every executor of the project is down. The other project's connection is
    # healthy and idle - and is not touched: the task waits for its own.
    for label in ("A", "B"):
        lab["service"].store  # noqa: B018 - the accounts are marked through the pool
        lab["router"].pool.record_failure(OWNER, ids[label], "down", unavailable=True)
    run_id = _run(lab, ["stuck"])
    with pytest.raises(NoUsableAccount):
        lab["router"].select_for_task(lab["lab"].tasks_internal(run_id)[0])
    lab["scheduler"].tick(run_id)
    task = lab["lab"].tasks_internal(run_id)[0]
    assert task["state"] == "waiting" and "no usable OpenHands executor" in task["wait_reason"]
    assert lab["lab"].run_row(run_id)["state"] == "running"
    assert _started_on(lab, "C") == 1

    # Somebody else's connection cannot be put on the list, and only an owner
    # of the project changes it.
    theirs = lab["service"].add("user:mallory", label="M", api_key="sk-key-M-000000000",
                                base_url=lab["clouds"]["C"].base_url)
    with pytest.raises(NotFound, match="connection not found"):
        lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], theirs["id"]])
    with pytest.raises(NotFound, match="project not found"):
        lab["executors"].set("user:mallory", lab["project"], executors=[theirs["id"]])
    with pytest.raises(NotFound):
        lab["executors"].get("user:mallory", lab["project"])
    with pytest.raises(ValueError, match="only once"):
        lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], ids["A"]])


def test_a_backup_takes_over_while_the_primary_is_down_and_gives_work_back(lab):
    ids = {label: a["id"] for label, a in lab["accounts"].items()}
    lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], ids["B"]])
    a = lab["clouds"]["A"]

    # The primary cannot start anything; the task is handed to the backup.
    a.cloud.start_status, a.cloud.start_status_times = 503, 100
    run_id = _run(lab, ["first"])
    for _ in range(3):
        lab["scheduler"].tick(run_id)
        if _where(lab, run_id):
            break
    assert _where(lab, run_id) == {"first": "B"}
    assert _started_on(lab, "B") == 1
    history = lab["lab"].executions(run_id)
    assert [(e["executor_label"], e["kind"]) for e in history] == [("B", "handoff")]
    listed = {e["label"]: e for e in lab["executors"].get(OWNER, lab["project"])["executors"]}
    assert listed["A"]["usable"] is False and listed["A"]["health"] == "unavailable"
    assert listed["B"]["usable"] is True

    # While it is down, new work goes to the backup as well.
    second = _run(lab, ["second"])
    lab["scheduler"].tick(second)
    assert _where(lab, second) == {"second": "B"}

    # The primary is back. A check finds that out; the task that is running
    # stays where it is, and the next one starts on the primary again.
    a.cloud.start_status_times = 0
    checked = lab["executors"].check(OWNER, lab["project"])
    assert {e["label"]: e["usable"] for e in checked["executors"]} == {"A": True, "B": True}
    assert set(checked["checked"]) == {ids["A"], ids["B"]}
    lab["scheduler"].tick(run_id)
    assert _where(lab, run_id) == {"first": "B"}, "a conversation does not move"
    third = _run(lab, ["third"])
    lab["scheduler"].tick(third)
    assert _where(lab, third) == {"third": "A"}
    assert _started_on(lab, "A") == 1

    # The loop finds a recovered executor by itself, too.
    lab["router"].pool.record_failure(OWNER, ids["A"], "down again", unavailable=True)
    sql(lab, "UPDATE nexus_openhands_accounts SET health_checked_at = now() - interval '1 hour'")
    assert [r["healthy"] for r in lab["router"].reprobe()] == [True]
    assert lab["service"].get(OWNER, ids["A"])["health"] == "healthy"


def test_an_executor_switched_off_for_one_project_still_serves_another(lab):
    ids = {label: a["id"] for label, a in lab["accounts"].items()}
    other = _project(lab["store"], "Other")
    lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], ids["B"]])
    lab["executors"].set(OWNER, other, executors=[ids["A"]])
    state = lab["executors"].set_enabled(OWNER, lab["project"], ids["A"], False)
    assert [(e["label"], e["enabled"], e["usable"]) for e in state["executors"]] == [
        ("A", False, False), ("B", True, True)]

    run_id = _run(lab, ["one"])
    lab["scheduler"].tick(run_id)
    assert _where(lab, run_id) == {"one": "B"}
    elsewhere = _run(lab, ["one"], project=other)
    lab["scheduler"].tick(elsewhere)
    assert _where(lab, elsewhere) == {"one": "A"}

    # Order is the list's order; "make primary" keeps the others.
    state = lab["executors"].make_primary(OWNER, lab["project"], ids["B"])
    assert [(e["label"], e["role"]) for e in state["executors"]] == [
        ("B", "primary"), ("A", "fallback")]
    bound = sql(lab, "SELECT openhands_account_id FROM nexus_projects WHERE id=%s",
                lab["project"])
    assert str(bound[0]["openhands_account_id"]) == ids["B"], "the project names its primary"
    # A connection that is at work for the project is not taken off its list.
    with pytest.raises(ValueError, match="running a task"):
        lab["executors"].remove(OWNER, lab["project"], ids["B"])


def test_independent_tasks_are_spread_over_the_executors_when_asked(lab):
    ids = {label: a["id"] for label, a in lab["accounts"].items()}
    lab["executors"].set(OWNER, lab["project"], executors=[ids["A"], ids["B"], ids["C"]],
                         strategy="distribute")
    assert lab["executors"].get(OWNER, lab["project"])["strategy"] == "distribute"
    run_id = _run(lab, ["one", "two", "three", "four"], dependencies={"four": ["one"]})
    for _ in range(4):
        lab["scheduler"].tick(run_id)
    where = _where(lab, run_id)
    assert sorted(where.values()) == ["A", "B", "C"], where
    assert "four" not in where, "a task that depends on another still waits for it"
    assert (_started_on(lab, "A"), _started_on(lab, "B"), _started_on(lab, "C")) == (1, 1, 1)
    # Who ran what is kept, per task.
    history = {e["task_key"]: e["executor_label"] for e in lab["lab"].executions(run_id)}
    assert history == where
    # The same history, from the connection's side.
    used = lab["service"].usage(OWNER, ids["A"])
    assert [(u["task_key"], u["project_name"]) for u in used] == [
        (next(k for k, v in where.items() if v == "A"), "Shop")]
    with pytest.raises(ValueError, match="failover or distribute"):
        lab["executors"].set(OWNER, lab["project"], strategy="random")


def test_a_project_without_a_list_keeps_the_rule_it_had(lab):
    """No list: the owner's connections under the pool's own policy - what it
    was before lists existed."""
    state = lab["executors"].get(OWNER, lab["project"])
    assert state["executors"] == [] and state["mode"] == "default"
    assert sorted(a["label"] for a in state["available"]) == ["A", "B", "C"]
    lab["service"].set_default(OWNER, lab["accounts"]["B"]["id"])
    run_id = _run(lab, ["one"])
    lab["scheduler"].tick(run_id)
    assert _where(lab, run_id) == {"one": "B"}

    # Binding one connection the old way is the same as a list of one.
    lab["service"].bind_project(OWNER, lab["project"], lab["accounts"]["C"]["id"])
    state = lab["executors"].get(OWNER, lab["project"])
    assert [(e["label"], e["role"]) for e in state["executors"]] == [("C", "primary")]
    lab["service"].bind_project(OWNER, lab["project"], None)
    assert lab["executors"].get(OWNER, lab["project"])["mode"] == "default"
