"""End-to-end account failover with context hand-off.

This is the load-bearing test of the OpenHands account pool: a real task is
started on account #1, #1 fails, the scheduler switches to account #2, and #2
receives the context that says what is already done. The assertions are on real
rows in PostgreSQL and on the messages the clients actually received — no mock
of the scheduler or the store.

The clients are small, honest fakes of the *transport* (each one speaks the
executor's client surface and records what it was asked to do); the scheduler,
the pool, the store and the router are the real code.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import (  # noqa: E402
    CloudConversation,
    CloudNotFound,
    CloudUnavailable,
    StartStatus,
    StartTask,
)
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import LabScheduler, OpenHandsExecutor  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.openhands_pool import AccountPool  # noqa: E402
from orchestration.openhands_router import AccountRouter  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:owner"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class AccountClient:
    """A transport that starts one conversation and then reports a state.

    ``start_fails`` makes the start raise (the account is down); otherwise the
    conversation starts and its execution status is whatever the test sets.
    Records every initial message so the hand-off context can be asserted.
    """

    supports_files = False

    def __init__(self, name: str, *, start_fails: bool = False):
        self.name = name
        self.start_fails = start_fails
        self.status = "finished"
        self.started: list[dict] = []
        self.deleted: list[str] = []
        self._conversations: dict[str, dict] = {}

    def start_conversation(self, *, initial_message, title=None, tags=None,
                           selected_repository=None, selected_branch=None, **kw):
        if self.start_fails:
            raise CloudUnavailable(f"{self.name}: sandbox unavailable")
        cid = f"{self.name}-conv-{len(self.started)}"
        self.started.append({"id": cid, "message": initial_message, "title": title})
        self._conversations[cid] = {"status": self.status, "text": "commit deadbeef0"}
        return StartTask(id=cid, status=StartStatus.READY.value, app_conversation_id=cid)

    def get_conversation(self, conversation_id):
        conv = self._conversations.get(conversation_id)
        if conv is None:
            raise CloudNotFound(conversation_id)
        return CloudConversation(id=conversation_id, title=None, sandbox_id=f"{self.name}-sb",
                                 execution_status=conv["status"], sandbox_status="RUNNING",
                                 selected_repository=None, selected_branch=None,
                                 conversation_url=None, raw={})

    def final_response(self, conversation_id):
        return self._conversations[conversation_id]["text"]

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        return True


class StubVerifier:
    """A verifier that always says 'verified', so the run can complete.

    The point of this test is failover, not verification; a real verifier is
    exercised elsewhere. Returning ``completed_verified`` lets the run finish
    and proves the hand-off reached the end.
    """

    def __init__(self):
        self.seen: list[str] = []

    def verify(self, task, report):
        from orchestration.nexus_lab import Verification

        self.seen.append(task["task_key"])
        return Verification("completed_verified", "stub verified",
                            {"state": report.state})


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
    from orchestration.cloud_client import CloudClient

    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _make_run(store, *, repository="owner/repo", branch="main"):
    projects = ProjectService(store)
    project = projects.create(PRINCIPAL, "Failover project")
    # The dispatch gate re-checks the allowlist and the project link, so the
    # repository has to be allowed and linked or every dispatch is refused.
    store.add_repository(repository, default_branch="main", allowed_branches=[branch])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link(PRINCIPAL, str(project["id"]), repository)
    goal, _ = projects.create_goal(PRINCIPAL, str(project["id"]), "Do the thing")
    lab = LabStore(store)
    run = lab.start_run(PRINCIPAL, str(project["id"]), str(goal["id"]),
                        max_iterations=20, max_attempts_per_task=5,
                        plan=[{"id": "T1", "title": "Step one", "description": "do it",
                               "acceptance": ["it works"], "dependencies": []}],
                        repository=repository, branch=branch)
    return project, run, lab


def test_failover_moves_the_task_to_a_second_account_with_context(store, service, clouds):
    a, b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)

    # A is the default and fails to start; B succeeds.
    client_a = AccountClient("A", start_fails=True)
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}

    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)

    def client_factory(account_id, task=None):
        return clients[account_id]

    executor = OpenHandsExecutor(None, router=router, client_factory=client_factory)
    verifier = StubVerifier()
    sched = LabScheduler(lab, executor, verifier, worker_id="w", router=router)

    project, run, _ = _make_run(store)

    # Tick 1: the task is claimed and dispatched. A fails, so the dispatch fails,
    # the task is requeued, and A is recorded as tried.
    r1 = sched.tick(str(run["id"]))
    assert r1.claimed == "T1"
    task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert task["state"] == "pending", "a failed start must requeue the task"
    assert va["id"] in task["tried_accounts"]

    # Tick 2: failover. A is excluded (tried), so B is chosen and starts.
    r2 = sched.tick(str(run["id"]))
    assert r2.dispatched, "the second tick must dispatch on the failover account"
    assert len(client_b.started) == 1, "account B must have started the task"
    # A never started a conversation.
    assert client_a.started == []

    # The message B received is a hand-off: it names the plan, the task and the
    # rule that the workspace is fresh.
    message = client_b.started[0]["message"]
    assert "continuing an interrupted task" in message
    assert "Step one" in message
    assert "clone the repository" in message.lower()
    assert "owner/repo" in message

    # The hand-off is recorded durably.
    handoffs = lab.handoffs(task["id"])
    assert handoffs, "the executor switch must be recorded"
    assert str(handoffs[0]["to_account_id"]) == vb["id"]

    # Tick 3: B's conversation is finished, so the task verifies and the run
    # completes.
    r3 = sched.tick(str(run["id"]))
    assert r3.verdict == "completed_verified"
    final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
    assert final["state"] == "completed"


def test_a_missing_conversation_fails_over_instead_of_dead_lettering(store, service, clouds):
    """The executor vanishing mid-flight is a failover, not a dead task."""
    a, b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)

    client_a = AccountClient("A")
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
    verifier = StubVerifier()
    sched = LabScheduler(lab, executor, verifier, worker_id="w", router=router)
    project, run, _ = _make_run(store)

    # Tick 1: A starts the conversation.
    sched.tick(str(run["id"]))
    assert len(client_a.started) == 1
    assert len(client_b.started) == 0
    # A vanishes: its client can no longer see the conversation.
    client_a._conversations.clear()
    # Tick 2: the missing conversation is treated as the executor being gone, so
    # the task is requeued with A excluded and B takes over in the same tick.
    sched.tick(str(run["id"]))
    task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert va["id"] in task["tried_accounts"], "the gone account must be excluded"
    assert len(client_b.started) == 1, "B must take over the task"
    assert str(task["openhands_account_id"]) == vb["id"]


def test_no_usable_account_blocks_the_run_with_a_reason(store, service, clouds):
    """When every account is excluded, the run is blocked, never spun."""
    a, _b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 AccountClient("A"))
    verifier = StubVerifier()
    sched = LabScheduler(lab, executor, verifier, worker_id="w", router=router)
    project, run, _ = _make_run(store)
    # Mark the only account unusable before the first dispatch.
    pool.record_failure(PRINCIPAL, va["id"], "down", unavailable=True)
    sched.tick(str(run["id"]))
    final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
    assert final["state"] == "blocked"
    assert "no usable OpenHands account" in (final["last_error"] or "")


# -- the pool together with the rules of the run loop --------------------------
#
# The cases above are about an account that is down. These are about the two
# things that happen on a working account every day: a result that did not
# verify and is tried again, and a start the service refuses (429).

class _RefusingClient(AccountClient):
    """An account at its limit: every start is answered 429 until let through."""

    def __init__(self, name: str, *, code: str | None = "daily_conversation_limit_reached",
                 retry_after: float | None = None):
        super().__init__(name)
        self.refusing = True
        self.refusals = 0
        self._code, self._retry_after = code, retry_after

    def start_conversation(self, **kw):
        if self.refusing:
            from orchestration.cloud_client import CloudRateLimited

            self.refusals += 1
            raise CloudRateLimited(f"{self.name}: Daily conversation limit of 20 reached",
                                   code=self._code, retry_after=self._retry_after)
        return super().start_conversation(**kw)


class _SecondTimeVerifier:
    """Says 'not verified' the first time a task comes back, 'verified' after."""

    def __init__(self):
        self.seen: list[str] = []

    def verify(self, task, report):
        from orchestration.nexus_lab import Verification

        self.seen.append(task["task_key"])
        if self.seen.count(task["task_key"]) == 1:
            return Verification("completed_unverified",
                                "reported commit is not the branch head", {"state": report.state})
        return Verification("completed_verified", "verified", {"state": report.state})


def _stack(store, service, clients, *, verifier=None, mode="failover", **scheduler):
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode=mode)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None: clients[account_id])
    sched = LabScheduler(lab, executor, verifier or StubVerifier(), worker_id="w",
                         router=router, **scheduler)
    return pool, lab, sched


def _task(lab, project, run):
    return lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]


def _health(service, account_id):
    return next(a["health"] for a in service.list_accounts(PRINCIPAL)
                if str(a["id"]) == str(account_id))


def test_a_task_that_did_not_verify_is_started_again_on_the_only_account(store, service, clouds):
    """Resuming a run starts its unverified task again. With one account in the
    pool that has to be the account it ran on: there is no other."""
    a, _b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    client_a = AccountClient("A")
    _pool, lab, sched = _stack(store, service, {va["id"]: client_a},
                               verifier=_SecondTimeVerifier())
    project, run, _ = _make_run(store)

    sched.tick(str(run["id"]))                       # started on A
    assert sched.tick(str(run["id"])).verdict == "completed_unverified"
    sched.tick(str(run["id"]))                       # nothing left to start: the run stops
    assert lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["state"] == "blocked"

    lab.set_run_state(PRINCIPAL, str(project["id"]), str(run["id"]), "running")
    again = sched.tick(str(run["id"]))
    assert again.dispatched, f"the task was not started again: {again.note}"
    assert len(client_a.started) == 2
    task = _task(lab, project, run)
    assert task["attempt"] == 2 and task["tried_accounts"] == []
    # The same account, so nothing was handed over: the agent gets the task's
    # own instructions, not "the previous account is no longer available".
    assert "continuing an interrupted task" not in client_a.started[1]["message"]
    assert lab.handoffs(task["id"]) == []

    assert sched.tick(str(run["id"])).verdict == "completed_verified"
    sched.tick(str(run["id"]))
    assert lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["state"] == "completed"


def test_a_refused_start_moves_to_the_next_account_and_costs_nothing(store, service, clouds):
    """429 from one account: another one takes the task at once. Nothing was
    started, so no attempt is spent, and the refusing account is not written
    off - it is working, it has only said "not now"."""
    a, b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    client_a, client_b = _RefusingClient("A"), AccountClient("B")
    _pool, lab, sched = _stack(store, service, {va["id"]: client_a, vb["id"]: client_b})
    project, run, _ = _make_run(store)

    first = sched.tick(str(run["id"]))
    assert first.note == "start refused: rate limited"
    task = _task(lab, project, run)
    assert task["state"] == "pending" and task["attempt"] == 0
    assert task["tried_accounts"] == [va["id"]]
    assert "Daily conversation limit" in task["last_error"]
    assert _health(service, va["id"]) == "degraded", "a refusal is not an outage"
    assert lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["iteration"] == 0

    second = sched.tick(str(run["id"]))              # no wait: B is free to take it
    assert second.dispatched
    assert client_a.refusals == 1 and len(client_b.started) == 1
    task = _task(lab, project, run)
    assert task["attempt"] == 1 and str(task["openhands_account_id"]) == vb["id"]
    # B is told it continues, and where the work has to end up.
    message = client_b.started[0]["message"]
    assert "continuing an interrupted task" in message
    assert "push it to branch main of owner/repo" in message
    assert "never force-push" in message

    # The next task of anyone still goes to A first: it was not taken out.
    assert _health(service, va["id"]) in ("degraded", "healthy")


def test_a_refused_start_waits_when_no_other_account_is_left(store, service, clouds):
    """One account, at its daily limit. The run is not blocked and the task is
    not spent: it waits, and starts when the account takes it again."""
    a, _b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    client_a = _RefusingClient("A")
    waits: list[float] = []

    def wait(error):
        waits.append(1.0)
        return 0.0                                    # the test does not sleep

    _pool, lab, sched = _stack(store, service, {va["id"]: client_a}, start_wait=wait)
    project, run, _ = _make_run(store)

    for _ in range(4):                                # more refusals than attempts
        assert sched.tick(str(run["id"])).note == "start refused: rate limited"
    task = _task(lab, project, run)
    assert client_a.refusals == 4 and len(waits) == 4
    assert task["state"] == "pending" and task["attempt"] == 0
    assert task["tried_accounts"] == [], "the only account must stay eligible"
    assert "not counted as an attempt" in task["last_error"]
    assert lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["state"] == "running"
    assert _health(service, va["id"]) == "degraded"

    client_a.refusing = False                         # the limit is lifted
    assert sched.tick(str(run["id"])).dispatched
    assert len(client_a.started) == 1
    assert "continuing an interrupted task" not in client_a.started[0]["message"]
    assert _task(lab, project, run)["attempt"] == 1


def test_every_account_refusing_is_a_wait_not_a_dead_end(store, service, clouds):
    """Both accounts are at their limit. The task goes round them and then
    waits; when one of them takes work again, it starts there."""
    a, b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    client_a, client_b = _RefusingClient("A"), _RefusingClient("B")
    _pool, lab, sched = _stack(store, service, {va["id"]: client_a, vb["id"]: client_b},
                               start_wait=lambda error: 0.0)
    project, run, _ = _make_run(store)

    for _ in range(6):
        assert sched.tick(str(run["id"])).note == "start refused: rate limited"
    assert client_a.refusals >= 2 and client_b.refusals >= 2, "both are asked again"
    task = _task(lab, project, run)
    assert task["state"] == "pending" and task["attempt"] == 0
    assert lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["state"] == "running"

    client_a.refusing = False
    for _ in range(3):
        if sched.tick(str(run["id"])).dispatched:
            break
    assert len(client_a.started) == 1 and client_b.started == []
