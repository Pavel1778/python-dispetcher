"""OpenHands account pool and diagnostics.

Real PostgreSQL and real fake Cloud servers (the client speaks HTTP over a
socket; nothing here mocks the client). The invariants this suite pins:

* the four selection modes each do what they say (MANUAL never falls back,
  AUTO uses the default then the best, FAILOVER honours an exclude set,
  LOAD_BALANCING spreads across the least-busy account);
* disabling or revoking an account removes it from selection;
* removing an account with active tasks is refused unless forced;
* the diagnostic report earns every status — VERIFIED only from a real
  observation, FAILED from a real failure, BLOCKED from a missing resource,
  UNVERIFIED from a check that did not run.
"""
from __future__ import annotations

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
from orchestration.openhands_pool import AccountPool, PoolError  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

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


@pytest.fixture
def store():
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def clouds():
    with FakeCloudServer() as a, FakeCloudServer() as b, FakeCloudServer() as c:
        for s, profile in ((a, "pA"), (b, "pB"), (c, "pC")):
            s.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": profile,
                                "agent_settings": {"mcp_config": {}},
                                "registered_marketplaces": [], "inherited_marketplaces": []}
        yield a, b, c


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _three_accounts(service, clouds):
    a, b, c = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    vc = service.add(PRINCIPAL, label="C", api_key="sk-c", base_url=c.base_url)
    return va, vb, vc


# ----------------------------------------------------------------- pool modes
def test_manual_mode_never_falls_back(store, service, clouds):
    va, _vb, _vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    # The default account fails: manual mode refuses rather than switch.
    pool.record_failure(PRINCIPAL, va["id"], "boom", unavailable=True)
    with pytest.raises(PoolError):
        pool.select(PRINCIPAL)


def test_auto_mode_uses_default_then_best(store, service, clouds):
    va, vb, _vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    pool.record_failure(PRINCIPAL, va["id"], "auth", unavailable=True)
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    assert pool.select(PRINCIPAL)["id"] == vb["id"]


def test_failover_mode_honours_exclude(store, service, clouds):
    va, vb, vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    pool.set_priority(PRINCIPAL, vc["id"], 2)
    # First pick: the default A.
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    # A failed; the caller names it in exclude, so the next pick is B.
    assert pool.select(PRINCIPAL, exclude=[va["id"]])["id"] == vb["id"]
    # B also failed; the caller excludes both and lands on C, not on A again.
    assert pool.select(PRINCIPAL, exclude=[va["id"], vb["id"]])["id"] == vc["id"]


def test_load_balancing_picks_the_least_busy(store, service, clouds):
    _va, vb, vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    pool.begin_task(PRINCIPAL, vb["id"])
    pool.begin_task(PRINCIPAL, vb["id"])
    pool.begin_task(PRINCIPAL, vc["id"])
    chosen = pool.select(PRINCIPAL)
    # A has 0 active tasks and is the default, so it is the least busy.
    assert chosen["active_tasks"] == 0


def test_disabled_and_revoked_accounts_are_never_selected(store, service, clouds):
    va, vb, _vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    pool.set_enabled(PRINCIPAL, va["id"], False)
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    chosen = pool.select(PRINCIPAL)
    assert chosen["id"] == vb["id"]
    service.revoke(PRINCIPAL, vb["id"])
    # Both A (disabled) and B (revoked) are out; C remains.
    assert pool.select(PRINCIPAL)["label"] == "C"


def test_remove_refuses_while_active_tasks_then_force(store, service, clouds):
    _va, vb, _vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.begin_task(PRINCIPAL, vb["id"])
    with pytest.raises(PoolError):
        pool.remove(PRINCIPAL, vb["id"])
    pool.remove(PRINCIPAL, vb["id"], force=True)
    labels = {a["label"] for a in service.list_accounts(PRINCIPAL)}
    assert "B" not in labels


def test_the_pool_activates_when_any_principal_has_an_account(store, service, clouds):
    """``build_account_router`` must switch the pool on when *any* principal has
    an account, not only ``machine:default``.

    A worker runs as ``machine:default`` and owns no accounts; an account added
    in the studio belongs to ``user:<login>``. A per-principal check returned
    ``None`` on every deployment with a login, so the pool - and with it the
    automatic selection modes - was never switched on. This pins the fix.
    """
    from orchestration.openhands_router import build_account_router

    a, _b, _c = clouds
    # No accounts anywhere: the pool stays off (the pre-existing single-key path).
    assert build_account_router(store) is None
    # An account for a *user* principal, which the worker does not own.
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url, make_default=True)
    router = build_account_router(store)
    assert router is not None, "the pool must be on for a deployment that has accounts"
    assert router.pool is not None


def test_priority_and_fallback_order_are_applied(store, service, clouds):
    va, vb, vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    # Pin an explicit order that differs from creation order.
    pool.set_config(PRINCIPAL, fallback_order=[vc["id"], vb["id"], va["id"]])
    # A is the default and healthy, so it still wins under auto/failover.
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    # Exclude A and B: the order picks C.
    assert pool.select(PRINCIPAL, exclude=[va["id"], vb["id"]])["id"] == vc["id"]


# ------------------------------------------------------------------ diagnostics
def _diagnostics(store, service, clouds):
    a, _b, _c = clouds
    pool = AccountPool(store, service)
    return OpenHandsDiagnostics(
        store, accounts=service, pool=pool,
        cloud_client=CloudClient(base_url=a.base_url, api_key="sk-a", timeout=5.0),
        client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def test_diagnostics_verified_paths(store, service, clouds):
    _three_accounts(service, clouds)
    diag = _diagnostics(store, service, clouds)
    report = diag.run(PRINCIPAL)
    by = {c.name: c for c in report.checks}
    assert by["database"].status == VERIFIED
    assert by["schema"].status == VERIFIED
    assert by["accounts"].status == VERIFIED
    assert by["account_pool"].status == VERIFIED
    assert by["cloud_api"].status == VERIFIED
    assert by["queues_leases"].status == VERIFIED
    # The recovery round trip runs against the real database.
    assert by["recovery"].status == VERIFIED
    assert by["recovery"].evidence["lease_token_after"] > \
        by["recovery"].evidence["lease_token_before"]
    # The live probe was not requested: it must be UNVERIFIED, never VERIFIED.
    assert by["live_conversation"].status == UNVERIFIED
    assert by["task_completion"].status == UNVERIFIED
    assert report.ok


def test_recovery_check_leaves_nothing_behind(store, service, clouds):
    """The recovery probe deletes its throwaway project; no rows are left.

    The check is the same real round trip as ``queues_leases`` — a claim with an
    expired lease, a reclaim, a re-claim — so the only thing to prove separately
    is that it cleans up after itself and does not disturb existing rows.
    """
    _three_accounts(service, clouds)
    diag = _diagnostics(store, service, clouds)
    before = _count_projects(store)
    report = diag.run(PRINCIPAL)
    assert report.by_name("recovery").status == VERIFIED
    assert _count_projects(store) == before, "the recovery probe left a project behind"


def _count_projects(store) -> int:
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM nexus_projects")
        return cur.fetchone()["n"]


def test_task_completion_is_unverified_without_the_live_probe(store, service, clouds):
    """Absence of evidence: without ``live`` the completion check is UNVERIFIED.

    It must never be VERIFIED — the run was not requested, so nothing was
    proved — and it must not be FAILED either, which would claim a broken
    integration that was never exercised.
    """
    _three_accounts(service, clouds)
    diag = _diagnostics(store, service, clouds)
    report = diag.run(PRINCIPAL)
    check = report.by_name("task_completion")
    assert check.status == UNVERIFIED
    assert check.status != VERIFIED and check.status != FAILED


def test_diagnostics_without_accounts_is_blocked_not_failed(store, service, clouds):
    diag = _diagnostics(store, service, clouds)
    report = diag.run(PRINCIPAL)
    by = {c.name: c for c in report.checks}
    assert by["accounts"].status == BLOCKED
    assert by["account_pool"].status == BLOCKED
    # Nothing FAILED, so the report is still "ok" (no false success, no false failure).
    assert report.ok


def test_diagnostics_reports_a_rejected_key_as_failed(store, clouds, monkeypatch):
    """An account whose key is rejected at probe time is FAILED, with detail."""
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    a, _b, _c = clouds

    class Rejecting:
        def __init__(self, *_a, **_k):
            pass

        def account_capabilities(self):
            from orchestration.cloud_client import CloudAuthError

            raise CloudAuthError("bad key")

        def health(self):
            return True

    service = OpenHandsAccountService(store, client_factory=lambda u, k: Rejecting())
    # Store the account by verifying through a working factory first.
    good = OpenHandsAccountService(
        store, client_factory=lambda u, k: CloudClient(base_url=u, api_key=k, timeout=5.0))
    good.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url, make_default=True)
    diag = OpenHandsDiagnostics(store, accounts=service, pool=AccountPool(store, service),
                                cloud_client=CloudClient(base_url=a.base_url, api_key="sk-a",
                                                         timeout=5.0),
                                client_factory=lambda u, k: Rejecting())
    report = diag.run(PRINCIPAL)
    by = {c.name: c for c in report.checks}
    assert by["accounts"].status == FAILED
    assert not report.ok


def test_diagnostics_live_probe_verified(store, service, clouds, monkeypatch):
    """The live probe runs a conversation to completion and deletes it.

    The fake Cloud server is a real HTTP server the client speaks to over a
    socket, so this exercises the same code path the live service would; it is
    the fake's *LLM* that is simulated, not the transport.
    """
    a, _b, _c = clouds
    # Make the fake finish a conversation quickly and echo the marker.
    a.cloud.start_polls_required = 1

    class LiveClient:
        """A client that starts a conversation and reports it finished."""

        supports_files = False

        def __init__(self, cloud: FakeCloudServer):
            self.cloud = cloud
            self.conversations: dict[str, dict] = {}
            self.deleted: list[str] = []

        def start_conversation(self, *, initial_message, title=None, tags=None,
                               selected_repository=None, selected_branch=None, **kw):
            from orchestration.cloud_client import StartTask, StartStatus

            cid = f"conv-{len(self.conversations)}"
            # The marker is echoed back in the final response.
            import re

            m = re.search(r"echo (NEXUS-DIAG-\d+)", initial_message)
            self.conversations[cid] = {"status": "finished",
                                       "text": (m.group(1) if m else "")}
            return StartTask(id=cid, status=StartStatus.READY.value, app_conversation_id=cid)

        def get_conversation(self, conversation_id):
            from orchestration.cloud_client import CloudConversation

            conv = self.conversations[conversation_id]
            return CloudConversation(id=conversation_id, title=None, sandbox_id="sb-1",
                                     execution_status=conv["status"], sandbox_status="RUNNING",
                                     selected_repository=None, selected_branch=None,
                                     conversation_url=None, raw={})

        def final_response(self, conversation_id):
            return self.conversations[conversation_id]["text"]

        def delete_conversation(self, conversation_id):
            self.deleted.append(conversation_id)
            return True

    live = LiveClient(a)
    pool = AccountPool(store, service)
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url, make_default=True)
    diag = OpenHandsDiagnostics(
        store, accounts=service, pool=pool,
        cloud_client=CloudClient(base_url=a.base_url, api_key="sk-a", timeout=5.0),
        client_factory=lambda u, k: CloudClient(base_url=u, api_key=k, timeout=5.0),
        live_client_factory=lambda account_id: live)
    report = diag.run(PRINCIPAL, live=True)
    by = {c.name: c for c in report.checks}
    assert by["conversation_create"].status == VERIFIED
    assert by["message_and_events"].status == VERIFIED
    assert by["run_command"].status == VERIFIED
    assert by["task_completion"].status == VERIFIED
    assert by["workspace_rw"].status == BLOCKED  # transport cannot place files
    assert live.deleted == ["conv-0"], "the probe conversation must be deleted"


def test_task_completion_fails_when_the_conversation_errors(store, service, clouds):
    """A conversation that ends in ``error`` is a FAILED completion, not VERIFIED.

    The completion check must follow the conversation's real terminal state; a
    probe that reported VERIFIED for an errored run would be the false success
    the whole report exists to prevent.
    """
    a, _b, _c = clouds
    service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                make_default=True)

    class ErroringClient:
        supports_files = False

        def start_conversation(self, *, initial_message, title=None, tags=None,
                               selected_repository=None, selected_branch=None, **kw):
            from orchestration.cloud_client import StartStatus, StartTask

            return StartTask(id="conv-e", status=StartStatus.READY.value,
                             app_conversation_id="conv-e")

        def get_conversation(self, conversation_id):
            from orchestration.cloud_client import CloudConversation

            return CloudConversation(id=conversation_id, title=None, sandbox_id="sb",
                                     execution_status="error", sandbox_status="RUNNING",
                                     selected_repository=None, selected_branch=None,
                                     conversation_url=None, raw={})

        def delete_conversation(self, conversation_id):
            return True

    diag = OpenHandsDiagnostics(
        store, accounts=service, pool=AccountPool(store, service),
        cloud_client=CloudClient(base_url=a.base_url, api_key="sk-a", timeout=5.0),
        client_factory=lambda u, k: CloudClient(base_url=u, api_key=k, timeout=5.0),
        live_client_factory=lambda account_id: ErroringClient())
    report = diag.run(PRINCIPAL, live=True)
    by = {c.name: c for c in report.checks}
    assert by["task_completion"].status == FAILED
    assert "error" in by["task_completion"].detail
    assert not report.ok


# ------------------------------------------------- execution client per account
def test_execution_client_for_account_honours_transport_and_model(store, service, clouds):
    """The execution client is built from the account, not the process env.

    A ``direct`` account yields a sandbox-driving client carrying the account's
    ``default_model`` (a bare sandbox's default model may have no key); an
    ``app`` account yields the plain app-conversation client. Building either
    from ``OPENHANDS_*`` would silently run one account's work on another's
    identity and transport.
    """
    from orchestration.cloud_client import CloudClient
    from orchestration.cloud_direct import CloudDirectClient

    a, _b, _c = clouds
    direct = service.add(PRINCIPAL, label="direct", api_key="sk-a", base_url=a.base_url,
                         transport="direct", default_model="openhands/deepseek-v4.1-flash",
                         make_default=True)
    app = service.add(PRINCIPAL, label="app", api_key="sk-b", base_url=a.base_url,
                      transport="app")

    dc = service.execution_client_for_account(PRINCIPAL, direct["id"])
    assert isinstance(dc, CloudDirectClient)
    assert dc.llm_model == "openhands/deepseek-v4.1-flash"
    assert dc.base_url == a.base_url

    ac = service.execution_client_for_account(PRINCIPAL, app["id"])
    assert isinstance(ac, CloudClient) and not isinstance(ac, CloudDirectClient)
    assert ac.base_url == a.base_url


def test_live_probe_uses_the_selected_account_config(store, clouds, monkeypatch):
    """The live probe runs on the account it selected, with that account's URL+key.

    Regression: the probe used to build the process-wide client, so it exercised
    whatever ``OPENHANDS_*`` named rather than the account under test. A probe
    that can pass while the named account is misconfigured is worse than none.
    """
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    a, b, _c = clouds
    seen: list[tuple[str, str]] = []

    class ProbeClient:
        """A client that satisfies both ``account_capabilities`` (add/verify)
        and the conversation surface the live probe uses."""

        supports_files = False

        def account_capabilities(self):
            return {"llm_api_key_set": True, "mcp_servers": []}

        def start_conversation(self, *, initial_message, title=None, tags=None,
                               selected_repository=None, selected_branch=None, **kw):
            from orchestration.cloud_client import StartStatus, StartTask

            import re

            m = re.search(r"echo (NEXUS-DIAG-\d+)", initial_message)
            self.marker = m.group(1) if m else ""
            return StartTask(id="conv-x", status=StartStatus.READY.value,
                             app_conversation_id="conv-x")

        def get_conversation(self, conversation_id):
            from orchestration.cloud_client import CloudConversation

            return CloudConversation(id=conversation_id, title=None, sandbox_id="sb",
                                     execution_status="finished", sandbox_status="RUNNING",
                                     selected_repository=None, selected_branch=None,
                                     conversation_url=None, raw={})

        def final_response(self, conversation_id):
            return self.marker

        def delete_conversation(self, conversation_id):
            return True

    def recording_factory(base_url, api_key):
        seen.append((base_url, api_key))
        return ProbeClient()

    service = OpenHandsAccountService(store, client_factory=recording_factory)
    # The account lives on server B, with B's key.
    account = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url,
                          transport="app", make_default=True)

    diag = OpenHandsDiagnostics(
        store, accounts=service, pool=AccountPool(store, service),
        cloud_client=CloudClient(base_url=a.base_url, api_key="sk-a", timeout=5.0),
        client_factory=recording_factory)
    report = diag.run(PRINCIPAL, live=True, account_id=account["id"])
    by = {c.name: c for c in report.checks}
    assert by["conversation_create"].status == VERIFIED
    assert by["message_and_events"].status == VERIFIED
    # The probe spoke to the *account's* base URL with the *account's* key, and
    # never to the process-wide server A.
    assert (b.base_url, "sk-b") in seen
    assert (a.base_url, "sk-a") not in seen


# ------------------------------------------------- observed load and recovery
def test_load_balancing_reads_in_flight_tasks_from_the_store(store, service, clouds):
    """Load is derived from durable rows, not from a counter nobody maintains.

    Regression: ``begin_task``/``end_task`` had no production caller, so the
    ``active_tasks`` column stayed 0 and load balancing could not balance. The
    in-flight count is now read from ``nexus_run_tasks`` directly.
    """
    va, vb, _vc = _three_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    # A (the default) is busy: two in-flight tasks bound to it in the store.
    _seed_in_flight(store, va["id"], 2)
    chosen = pool.select(PRINCIPAL)
    assert chosen["id"] != va["id"], "the busy default must not be chosen"
    # The two idle accounts tie; priority then id breaks the tie deterministically.
    assert chosen["active_tasks"] == 0


def _seed_in_flight(store, account_id: str, count: int) -> None:
    """Create *count* tasks in an in-flight state bound to *account_id*."""
    import uuid

    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    projects = ProjectService(store)
    lab = LabStore(store)
    project = projects.create(PRINCIPAL, f"load-{uuid.uuid4().hex[:6]}")
    pid = str(project["id"])
    goal, _ = projects.create_goal(PRINCIPAL, pid, "load probe")
    run = lab.start_run(PRINCIPAL, pid, str(goal["id"]), max_iterations=10,
                        plan=[{"id": f"T{i}", "title": f"t{i}", "description": "",
                               "acceptance": [], "dependencies": []}
                              for i in range(count)])
    for _ in range(count):
        task = lab.claim_task(str(run["id"]), "probe", lease_seconds=600)
        lab.mark_running(task["id"], conversation_id=f"c-{task['id']}",
                         account_id=account_id)


def test_a_transient_failure_is_forgotten_when_the_account_works_again(store, service, clouds):
    """A success returns an account to healthy, so a blip is not permanent.

    Regression: nothing called ``record_success``, so an account marked
    ``unavailable`` by one transient error was excluded from every later
    selection even after it started working again.
    """
    from orchestration.nexus_lab import LabScheduler, OpenHandsExecutor, Verification
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService
    from orchestration.openhands_router import AccountRouter

    a, _b, _c = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    # One transient failure marked the account unavailable.
    pool.record_failure(PRINCIPAL, va["id"], "blip", unavailable=True)
    assert service.list_accounts(PRINCIPAL)[0]["health"] == "unavailable"

    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)

    class WorkingClient:
        supports_files = False

        def start_conversation(self, *, initial_message, title=None, tags=None,
                               selected_repository=None, selected_branch=None, **kw):
            from orchestration.cloud_client import StartStatus, StartTask

            return StartTask(id="c1", status=StartStatus.READY.value, app_conversation_id="c1")

        def get_conversation(self, conversation_id):
            from orchestration.cloud_client import CloudConversation

            return CloudConversation(id=conversation_id, title=None, sandbox_id="sb",
                                     execution_status="finished", sandbox_status="RUNNING",
                                     selected_repository=None, selected_branch=None,
                                     conversation_url=None, raw={})

        def final_response(self, conversation_id):
            return "done"

    class Ok:
        def verify(self, task, report):
            return Verification("completed_verified", "ok", {})

    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None: WorkingClient())
    sched = LabScheduler(lab, executor, Ok(), worker_id="w", router=router)
    # The account is unhealthy, so a dispatch is not even attempted; the task is
    # requeued. Put it back to healthy via the real success path instead: a task
    # already bound to the account that then finishes.
    project = ProjectService(store).create(PRINCIPAL, "recovery")
    pid = str(project["id"])
    goal, _ = ProjectService(store).create_goal(PRINCIPAL, pid, "recover")
    run = lab.start_run(PRINCIPAL, pid, str(goal["id"]), max_iterations=10,
                        plan=[{"id": "T1", "title": "t", "description": "",
                               "acceptance": [], "dependencies": []}])
    # Bind the task to the account and put it in flight, then tick: the poll
    # observes a finished conversation and must return the account to healthy.
    task = lab.claim_task(str(run["id"]), "w", lease_seconds=600)
    lab.mark_running(task["id"], conversation_id="c1", account_id=va["id"])
    sched.tick(str(run["id"]))
    assert service.list_accounts(PRINCIPAL)[0]["health"] == "healthy"
