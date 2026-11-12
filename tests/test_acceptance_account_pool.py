"""Acceptance section B — Account Pool.

The task text asks the pool to support an arbitrary number of accounts and to
behave correctly for: one account, two, three, an account switched off, the
primary failing, a move to the backup, a manual switch, and automatic
distribution.

Real PostgreSQL and real fake Cloud servers (the client speaks HTTP over a
socket; the account service verifies each key against the server). Nothing here
mocks the pool, the account service or the store. Each case asserts on the
account actually *selected* (an id from the store) and on what the pool's own
records say happened.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import (  # noqa: E402
    OpenHandsAccountService,
)
from orchestration.openhands_pool import AccountPool, PoolError  # noqa: E402
from tests.acceptance_kit import reset_schema  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

PRINCIPAL = "user:acceptance"


@pytest.fixture
def store():
    reset_schema(DSN)
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


def _configure(*servers) -> None:
    """Give every fake a distinct, key-free settings snapshot."""
    for index, server in enumerate(servers):
        server.cloud.settings = {
            "llm_api_key_set": True,
            "active_agent_profile_id": f"profile-{index}",
            "agent_settings": {"mcp_config": {}},
            "registered_marketplaces": [],
            "inherited_marketplaces": [],
            "disabled_skills": [],
            "provider_tokens_set": {},
        }


@pytest.fixture
def clouds():
    with FakeCloudServer() as a, FakeCloudServer() as b, FakeCloudServer() as c:
        _configure(a, b, c)
        yield a, b, c


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "acceptance-secret-key")
    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _add(service, server, label, key, **kw):
    return service.add(PRINCIPAL, label=label, api_key=key,
                       base_url=server.base_url, **kw)


# ------------------------------------------------------------------- counts
def test_one_account_is_selectable(store, service, clouds):
    a, _b, _c = clouds
    account = _add(service, a, "A", "sk-a", make_default=True)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    assert pool.select(PRINCIPAL)["id"] == account["id"]
    assert len(service.list_accounts(PRINCIPAL)) == 1


def test_two_accounts_are_both_supported_and_independently_verified(
        store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    assert len(service.list_accounts(PRINCIPAL)) == 2
    # Each account is verified against *its own* endpoint: the identities differ.
    assert va["identity"] != vb["identity"]
    # Re-verifying one touches only that account.
    verified = service.verify(PRINCIPAL, vb["id"])
    assert verified["state"] == "active"


def test_three_accounts_are_supported(store, service, clouds):
    a, b, c = clouds
    for server, label in ((a, "A"), (b, "B"), (c, "C")):
        _add(service, server, label, f"sk-{label.lower()}")
    assert len(service.list_accounts(PRINCIPAL)) == 3
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    chosen = {pool.select(PRINCIPAL)["id"] for _ in range(3)}
    # With no load anywhere the first is stable, but every id is a real account.
    assert chosen <= {a_["id"] for a_ in service.list_accounts(PRINCIPAL)}


def test_an_arbitrary_number_of_accounts_is_not_capped(store, service, clouds):
    a, b, c = clouds
    for index in range(12):
        server = (a, b, c)[index % 3]
        # A distinct label per account (label is the per-principal unique key).
        _add(service, server, f"acct-{index}", f"sk-{index}")
    assert len(service.list_accounts(PRINCIPAL)) == 12


# ----------------------------------------------------------------- disable
def test_a_disabled_account_is_never_selected(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    pool.set_enabled(PRINCIPAL, va["id"], False)
    # A is the default but disabled, so AUTO falls through to B.
    assert pool.select(PRINCIPAL)["id"] == vb["id"]
    # Re-enabling brings it back as the default.
    pool.set_enabled(PRINCIPAL, va["id"], True)
    assert pool.select(PRINCIPAL)["id"] == va["id"]


# ------------------------------------------------------ primary / backup / failover
def test_a_failed_primary_falls_over_to_the_backup(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    # The primary is selected first...
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    # ...then it fails, and the pool excludes it and picks the backup.
    pool.record_failure(PRINCIPAL, va["id"], "sandbox unavailable", unavailable=True)
    chosen = pool.select(PRINCIPAL, exclude={va["id"]})
    assert chosen["id"] == vb["id"]
    # The backup is now the default fallback even without an exclude set.
    assert pool.select(PRINCIPAL)["id"] == vb["id"]


def test_failover_reaches_the_third_account_when_two_are_down(store, service, clouds):
    a, b, c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    vc = _add(service, c, "C", "sk-c")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    pool.set_priority(PRINCIPAL, vc["id"], 2)
    chosen = pool.select(PRINCIPAL, exclude={va["id"], vb["id"]})
    assert chosen["id"] == vc["id"]


def test_manual_mode_refuses_rather_than_switching(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    pool.record_failure(PRINCIPAL, va["id"], "down", unavailable=True)
    with pytest.raises(PoolError):
        pool.select(PRINCIPAL)


# ------------------------------------------------------------ manual switch
def test_a_manual_switch_honours_the_named_account(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    # The operator makes B the default; manual mode then uses B.
    service.set_default(PRINCIPAL, vb["id"])
    assert pool.select(PRINCIPAL)["id"] == vb["id"]
    # And making A default again switches back.
    service.set_default(PRINCIPAL, va["id"])
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_fallback_order_is_stored_and_reported(store, service, clouds):
    a, b, c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    vc = _add(service, c, "C", "sk-c")
    pool = AccountPool(store, service)
    config = pool.set_config(PRINCIPAL, selection_mode="failover",
                             fallback_order=[vb["id"], vc["id"], va["id"]])
    assert config["fallback_order"] == [vb["id"], vc["id"], va["id"]]
    assert pool.get_config(PRINCIPAL)["fallback_order"] == [vb["id"], vc["id"], va["id"]]


# ------------------------------------------------------ automatic distribution
def test_automatic_distribution_spreads_across_least_busy(store, service, clouds):
    a, b, c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    vc = _add(service, c, "C", "sk-c")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    # With no observed load, priority breaks the tie: C (priority 1) is chosen.
    pool.set_priority(PRINCIPAL, vc["id"], 1)
    assert pool.select(PRINCIPAL)["id"] == vc["id"]
    # Give C some real in-flight load: the choice moves to the least busy.
    _seed_in_flight(store, vc["id"], 3)
    chosen = pool.select(PRINCIPAL)
    assert chosen["id"] in {va["id"], vb["id"]}


def test_load_balancing_ignores_an_account_at_its_limit(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True, max_concurrent_tasks=1)
    vb = _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    _seed_in_flight(store, va["id"], 1)     # A is now at its ceiling
    assert pool.select(PRINCIPAL)["id"] == vb["id"]


def test_priority_orders_automatic_selection(store, service, clouds):
    a, b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    vb = _add(service, b, "B", "sk-b")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    # A is the default and is chosen; when A is excluded, the lower priority
    # number wins.
    pool.set_priority(PRINCIPAL, va["id"], 5)
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    assert pool.select(PRINCIPAL, exclude={va["id"]})["id"] == vb["id"]


# -------------------------------------------------------------- remove rules
def test_removing_an_account_with_active_tasks_is_refused_then_forced(
        store, service, clouds):
    a, _b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    pool = AccountPool(store, service)
    # A task is in flight on A: removing it would orphan a running conversation.
    pool.begin_task(PRINCIPAL, va["id"])
    with pytest.raises(PoolError):
        pool.remove(PRINCIPAL, va["id"])
    pool.remove(PRINCIPAL, va["id"], force=True)
    assert service.list_accounts(PRINCIPAL) == []


def test_removing_an_idle_account_needs_no_force(store, service, clouds):
    a, _b, _c = clouds
    va = _add(service, a, "A", "sk-a", make_default=True)
    pool = AccountPool(store, service)
    pool.remove(PRINCIPAL, va["id"])
    assert service.list_accounts(PRINCIPAL) == []


# ------------------------------------------------------------------- helpers
def _seed_in_flight(store, account_id: str, count: int) -> None:
    """Create *count* in-flight tasks bound to *account_id* (observed load)."""
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    projects = ProjectService(store)
    lab = LabStore(store)
    project = projects.create(PRINCIPAL, f"load-{account_id[:6]}")
    pid = str(project["id"])
    goal, _ = projects.create_goal(PRINCIPAL, pid, "load probe")
    run = lab.start_run(PRINCIPAL, pid, str(goal["id"]), max_iterations=50,
                        plan=[{"id": f"T{i}", "title": f"t{i}", "description": "",
                               "acceptance": [], "dependencies": []}
                              for i in range(count)])
    for _ in range(count):
        task = lab.claim_task(str(run["id"]), "probe", lease_seconds=600)
        lab.mark_running(task["id"], conversation_id=f"c-{task['id']}",
                         account_id=account_id)
