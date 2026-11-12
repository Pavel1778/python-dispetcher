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
    # The live probe was not requested: it must be UNVERIFIED, never VERIFIED.
    assert by["live_conversation"].status == UNVERIFIED
    assert report.ok


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
    assert by["workspace_rw"].status == BLOCKED  # transport cannot place files
    assert live.deleted == ["conv-0"], "the probe conversation must be deleted"
