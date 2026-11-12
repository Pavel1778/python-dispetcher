"""OpenHands account administration: edit, connection, limits and masking.

Real PostgreSQL and real fake Cloud servers (the client speaks HTTP over a
socket; nothing mocks the client). These pin the account fields a pool must let
an operator set that the earlier suites did not cover:

* **edit** changes the non-secret fields only, and re-verifies a new base URL
  against the stored key rather than leaving an account pointing somewhere its
  key is rejected;
* **limits** are stored, shown masked-safe, and enforced: an account at its
  ``max_concurrent_tasks`` is skipped by automatic selection, while MANUAL still
  honours the operator's explicit choice;
* **connection** is a reachability probe, distinct from an authorization check;
* the **masked hint** shows which key is stored and is never the key itself.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.openhands_pool import AccountPool, PoolError  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:alice"
KEY_A = "sk-openhands-account-a-secret-value"
KEY_B = "sk-openhands-account-b-secret-value"


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
    with FakeCloudServer() as a, FakeCloudServer() as b:
        for s, profile in ((a, "pA"), (b, "pB")):
            s.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": profile,
                                "agent_settings": {"mcp_config": {}},
                                "registered_marketplaces": [], "inherited_marketplaces": []}
        yield a, b


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret-key")
    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _add(service, cloud, label="A", key=KEY_A, **kw):
    return service.add(PRINCIPAL, label=label, api_key=key, base_url=cloud.base_url, **kw)


# ------------------------------------------------------------------------ edit
def test_edit_changes_fields_without_touching_the_key(service, store, clouds):
    a, b = clouds
    view = _add(service, a, label="A", make_default=True)
    edited = service.edit(PRINCIPAL, view["id"], label="renamed", transport="app",
                          default_model="openhands/deepseek-v4.1-flash",
                          max_concurrent_tasks=3, daily_conversation_limit=20)
    assert edited["label"] == "renamed"
    assert edited["transport"] == "app"
    assert edited["default_model"] == "openhands/deepseek-v4.1-flash"
    assert edited["max_concurrent_tasks"] == 3
    assert edited["daily_conversation_limit"] == 20
    # The key is untouched and still decrypts to the original value.
    with store._tx() as cur:
        cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts WHERE id=%s",
                    (view["id"],))
        blob = bytes(cur.fetchone()["api_key_encrypted"])
    assert KEY_A.encode() not in blob
    assert service.client_for_account(PRINCIPAL, view["id"]).base_url == a.base_url


def test_edit_clears_a_limit_with_an_explicit_null(service, clouds):
    a, _b = clouds
    view = _add(service, a, max_concurrent_tasks=2)
    edited = service.edit(PRINCIPAL, view["id"], max_concurrent_tasks=None)
    assert edited["max_concurrent_tasks"] is None


def test_edit_leaves_unpassed_fields_alone(service, clouds):
    a, _b = clouds
    view = _add(service, a, label="A", max_concurrent_tasks=4)
    edited = service.edit(PRINCIPAL, view["id"], label="B")
    assert edited["label"] == "B"
    assert edited["max_concurrent_tasks"] == 4, "an unpassed limit must be preserved"
    assert edited["transport"] == "direct"


def test_edit_base_url_reverifies_the_stored_key(service, clouds):
    a, b = clouds
    view = _add(service, a, key=KEY_A)
    # The key is accepted by B's fake too (any bearer works there), so the move
    # succeeds and the account now talks to B.
    edited = service.edit(PRINCIPAL, view["id"], base_url=b.base_url)
    assert edited["base_url"] == b.base_url
    # A URL nothing answers on is refused, and the account keeps its old URL.
    with pytest.raises(ValueError):
        service.edit(PRINCIPAL, view["id"], base_url="http://127.0.0.1:1")
    assert service.list_accounts(PRINCIPAL)[0]["base_url"] == b.base_url


def test_edit_rejects_a_bad_transport_and_limit(service, clouds):
    a, _b = clouds
    view = _add(service, a)
    with pytest.raises(ValueError):
        service.edit(PRINCIPAL, view["id"], transport="carrier-pigeon")
    with pytest.raises(ValueError):
        service.edit(PRINCIPAL, view["id"], max_concurrent_tasks=0)
    with pytest.raises(ValueError):
        service.edit(PRINCIPAL, view["id"], daily_conversation_limit=-1)


def test_edit_is_scoped_to_the_principal(service, clouds):
    a, _b = clouds
    view = _add(service, a)
    with pytest.raises(NotFound):
        service.edit("user:bob", view["id"], label="hijack")


# -------------------------------------------------------------- masked values
def test_the_view_carries_a_masked_hint_never_the_key(service, clouds):
    a, _b = clouds
    view = _add(service, a, key=KEY_A)
    hint = view["api_key_hint"]
    assert hint and hint != KEY_A
    assert KEY_A not in str(view)
    # The hint shows the ends, not enough to reconstruct the key.
    assert KEY_A[:4] in hint and KEY_A[-4:] in hint
    assert KEY_A[4:-4] not in hint


def test_a_short_key_is_masked_whole(service, clouds):
    a, _b = clouds
    view = _add(service, a, key="sk-12345")
    assert view["api_key_hint"] == "•" * len("sk-12345")
    assert "12345" not in view["api_key_hint"]


def test_rotate_updates_the_hint_and_never_stores_plaintext(service, store, clouds):
    a, _b = clouds
    view = _add(service, a, key=KEY_A)
    rotated = service.rotate(PRINCIPAL, view["id"], api_key=KEY_B)
    assert rotated["api_key_hint"] == f"{KEY_B[:4]}…{KEY_B[-4:]}"
    assert KEY_B not in str(rotated)
    with store._tx() as cur:
        cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts WHERE id=%s",
                    (view["id"],))
        blob = bytes(cur.fetchone()["api_key_encrypted"])
    assert KEY_A.encode() not in blob and KEY_B.encode() not in blob


# ------------------------------------------------------------ connection check
def test_connection_check_records_reachability(service, clouds):
    a, _b = clouds
    view = _add(service, a)
    result = service.check_connection(PRINCIPAL, view["id"])
    assert result["reachable"] is True
    assert result["connection_ok"] is True
    assert result["last_connection_at"] is not None
    assert result["error"] is None


def test_connection_check_reports_an_unreachable_endpoint(service, store, clouds, monkeypatch):
    """A reachability probe fails cleanly when nothing answers.

    The account is stored against a working server, then the probe factory points
    at a closed port, so the check runs against a real refused connection.
    """
    a, _b = clouds
    view = _add(service, a)

    def refusing(base_url, api_key):
        return CloudClient(base_url="http://127.0.0.1:1", api_key=api_key, timeout=1.0)

    service._client_factory = refusing
    result = service.check_connection(PRINCIPAL, view["id"])
    assert result["reachable"] is False
    assert result["connection_ok"] is False
    assert result["error"]


def test_connection_check_is_separate_from_authorization(service, clouds, monkeypatch):
    """An endpoint can be reachable while the key is rejected: the two differ.

    The connection probe asks only whether the transport works, so a reachable
    endpoint with a rejected key is ``reachable=True`` here, while ``verify``
    marks the account invalid.
    """
    from orchestration.cloud_client import CloudAuthError

    a, _b = clouds
    view = _add(service, a)

    class ReachableButUnauthorized:
        def __init__(self, *_a, **_k):
            pass

        def ping(self):
            # Reachable: the endpoint answered (it just said "no").
            return {"reachable": True, "status": 401, "detail": "unauthorized"}

        def account_capabilities(self):
            raise CloudAuthError("bad key")

    service._client_factory = lambda u, k: ReachableButUnauthorized()
    conn = service.check_connection(PRINCIPAL, view["id"])
    assert conn["reachable"] is True
    verified = service.verify(PRINCIPAL, view["id"])
    assert verified["state"] == "invalid"


# --------------------------------------------------------- limits in the pool
def _seed_in_flight(store, account_id: str, count: int) -> None:
    """Create *count* in-flight tasks bound to *account_id* (observed load)."""
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


def test_automatic_selection_skips_an_account_at_its_limit(store, service, clouds):
    """An account at ``max_concurrent_tasks`` is not offered more work.

    The default account A is capped at 1 and has one task in flight, so AUTO
    must fall through to B rather than over-commit A.
    """
    a, b = clouds
    va = _add(service, a, label="A", key=KEY_A, make_default=True, max_concurrent_tasks=1)
    vb = _add(service, b, label="B", key=KEY_B)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    # A is idle: it is still the default and wins.
    assert pool.select(PRINCIPAL)["id"] == va["id"]
    # A now has one task in flight, exactly its limit: B takes over.
    _seed_in_flight(store, va["id"], 1)
    assert pool.select(PRINCIPAL)["id"] == vb["id"]


def test_manual_mode_honours_an_account_at_its_limit(store, service, clouds):
    """A limit steers automatic selection; MANUAL still obeys the operator."""
    a, b = clouds
    va = _add(service, a, label="A", key=KEY_A, make_default=True, max_concurrent_tasks=1)
    _add(service, b, label="B", key=KEY_B)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    _seed_in_flight(store, va["id"], 5)
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_a_full_pool_reports_capacity_not_a_generic_absence(store, service, clouds):
    """When every usable account is at its limit, the reason says so."""
    a, b = clouds
    va = _add(service, a, label="A", key=KEY_A, make_default=True, max_concurrent_tasks=1)
    vb = _add(service, b, label="B", key=KEY_B, max_concurrent_tasks=1)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    _seed_in_flight(store, va["id"], 1)
    _seed_in_flight(store, vb["id"], 1)
    with pytest.raises(PoolError) as exc:
        pool.select(PRINCIPAL)
    assert "concurrency limit" in str(exc.value)


def test_load_balancing_respects_limits(store, service, clouds):
    """A capped account is not chosen even when it looks the least busy."""
    a, b = clouds
    va = _add(service, a, label="A", key=KEY_A, max_concurrent_tasks=1)
    vb = _add(service, b, label="B", key=KEY_B, max_concurrent_tasks=5)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    _seed_in_flight(store, va["id"], 1)
    assert pool.select(PRINCIPAL)["id"] == vb["id"]
