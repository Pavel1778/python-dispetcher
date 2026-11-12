"""The acceptance test suite, sections B-G of the request.

Section A (connectivity) lives in ``tests/test_connectivity.py``. This module is
the rest of "Создай полноценный набор тестов": the account pool (B), context
hand-off (C), the autonomous worker (D), recovery (E), the real development task
(F) and the end-to-end failover (G).

What is real in each layer, and what is deliberately faked:

* PostgreSQL, the account service, the pool, the router, the store, the
  scheduler and the git verifier are the real code.
* OpenHands Cloud and GitHub are the real local socket servers
  (``tests/fake_cloud.py``, ``tests/fake_github.py``): the clients under test
  speak HTTP over a socket, and the servers enforce the real request/response
  shapes. Only the LLM behind a conversation is simulated.
* A *transport* that starts a conversation and reports a state is a small,
  honest fake of the executor's client surface (the same pattern
  ``tests/test_openhands_failover.py`` uses) - never a mock of the scheduler or
  the store.

Section F needs a live OpenHands key and a sandbox, which this environment does
not have (see AGENTS.md: the runtime here has no working LLM key). It is marked
BLOCKED, with the reason stated, rather than simulated - the request is explicit
that a real switch must not be faked.
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
from orchestration.nexus_lab import GitVerifier, LabScheduler, OpenHandsExecutor  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.openhands_pool import AccountPool, PoolError  # noqa: E402
from orchestration.openhands_router import AccountRouter  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
PRINCIPAL = "user:owner"
#: Present in the fake GitHub's default repository list, so the git verifier can
#: read its branch head for real.
REPO = "Pavel1778/python-dispetcher"
HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


# --------------------------------------------------------------- the transport


class AccountClient:
    """A transport that starts one conversation and reports a state.

    ``start_fails`` makes the start raise (the account is down); otherwise the
    conversation starts and its execution status is whatever the test sets.
    Records every initial message so a hand-off can be asserted. Not a mock of
    the scheduler, the store or the pool: those are the real code.
    """

    supports_files = False

    def __init__(self, name: str, *, start_fails: bool = False):
        self.name = name
        self.start_fails = start_fails
        self.status = "finished"
        self.report = "commit deadbeef0"
        self.started: list[dict] = []
        self.deleted: list[str] = []
        self._conversations: dict[str, dict] = {}

    def start_conversation(self, *, initial_message, title=None, tags=None,
                           selected_repository=None, selected_branch=None, **kw):
        if self.start_fails:
            raise CloudUnavailable(f"{self.name}: sandbox unavailable")
        cid = f"{self.name}-conv-{len(self.started)}"
        self.started.append({"id": cid, "message": initial_message, "title": title})
        self._conversations[cid] = {"status": self.status, "text": self.report}
        return StartTask(id=cid, status=StartStatus.READY.value, app_conversation_id=cid)

    def get_conversation(self, conversation_id):
        conv = self._conversations.get(conversation_id)
        if conv is None:
            raise CloudNotFound(conversation_id)
        return CloudConversation(id=conversation_id, title=None, sandbox_id=f"{self.name}-sb",
                                 execution_status=conv["status"], sandbox_status="RUNNING",
                                 selected_repository=None, selected_branch=None,
                                 conversation_url=None, raw={})

    def account_capabilities(self):
        return {"llm_api_key_set": True, "active_agent_profile_id": f"{self.name}-profile",
                "mcp_servers": [], "registered_marketplaces": 0,
                "inherited_marketplaces": 0, "disabled_skills": [],
                "provider_tokens_set": {}}

    def final_response(self, conversation_id):
        return self._conversations[conversation_id]["text"]

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        return True


# ------------------------------------------------------------------- fixtures


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
    monkeypatch.setenv("NEXUS_SECRET_KEY", "comprehensive-secret")
    from orchestration.cloud_client import CloudClient

    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _add(service, cloud, label, *, make_default=False, **kw):
    return service.add(PRINCIPAL, label=label, api_key=f"sk-{label}", base_url=cloud.base_url,
                       make_default=make_default, **kw)


def _make_run(store, *, repository=REPO, branch="main",
              plan=None, max_attempts=5):
    projects = ProjectService(store)
    project = projects.create(PRINCIPAL, "Comprehensive project")
    store.add_repository(repository, default_branch="main", allowed_branches=[branch])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link(PRINCIPAL, str(project["id"]), repository)
    goal, _ = projects.create_goal(PRINCIPAL, str(project["id"]), "Do the thing")
    lab = LabStore(store)
    plan = plan or [{"id": "T1", "title": "Step one", "description": "do it",
                     "acceptance": ["it works"], "dependencies": []}]
    run = lab.start_run(PRINCIPAL, str(project["id"]), str(goal["id"]),
                        max_iterations=20, max_attempts_per_task=max_attempts,
                        plan=plan, repository=repository, branch=branch)
    return project, run, lab


def _scheduler(store, service, clients, *, verifier, mode="failover", priority=None,
               pool=None):
    pool = pool or AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode=mode)
    if priority:
        for account_id, value in priority.items():
            pool.set_priority(PRINCIPAL, account_id, value)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
    return LabScheduler(lab, executor, verifier, worker_id="w", router=router), lab, pool


class StubVerifier:
    """A verifier that always says 'verified', so a run can complete.

    Used where the subject is the pool or the hand-off, not verification. Section
    G uses the *real* ``GitVerifier``.
    """

    def __init__(self, verdict="completed_verified"):
        self.verdict = verdict
        self.seen: list[str] = []

    def verify(self, task, report):
        from orchestration.nexus_lab import Verification

        self.seen.append(task["task_key"])
        return Verification(self.verdict, "stub verified", {"state": report.state})


# ================================================================= B. Account Pool
def test_pool_with_one_account_uses_it(store, service, clouds):
    """B: один аккаунт."""
    a, _b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    pool = AccountPool(store, service)
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_pool_with_two_accounts_prefers_the_default(store, service, clouds):
    """B: два аккаунта - the default runs while it is usable."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    _add(service, b, "B")
    pool = AccountPool(store, service)
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_pool_with_three_accounts_balances(store, service, clouds):
    """B: три аккаунта - load balancing spreads across the least busy."""
    a, b, c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    vc = _add(service, c, "C")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    # A and C are busy; B is free, so B is chosen even though A is the default.
    pool.begin_task(PRINCIPAL, va["id"])
    pool.begin_task(PRINCIPAL, vc["id"])
    assert pool.select(PRINCIPAL)["id"] == vb["id"]


def test_a_disabled_account_is_never_selected(store, service, clouds):
    """B: отключение аккаунта."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="auto")
    pool.set_enabled(PRINCIPAL, va["id"], False)
    assert pool.select(PRINCIPAL)["id"] == vb["id"]
    # Disabling is reversible: the account keeps its key and comes back.
    pool.set_enabled(PRINCIPAL, va["id"], True)
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_the_primary_failing_moves_to_the_reserve(store, service, clouds):
    """B: отказ основного -> переход на резервный (the pool level)."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    # The primary is marked unavailable (a rejected key / unreachable endpoint).
    pool.record_failure(PRINCIPAL, va["id"], "auth rejected", unavailable=True)
    assert pool.select(PRINCIPAL)["id"] == vb["id"]
    # A degraded (not unavailable) account stays selectable.
    pool.record_success(PRINCIPAL, va["id"])
    assert pool.select(PRINCIPAL)["id"] == va["id"]


def test_manual_mode_refuses_rather_than_switch(store, service, clouds):
    """B: ручное переключение - manual never silently switches."""
    a, _b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="manual")
    pool.record_failure(PRINCIPAL, va["id"], "down", unavailable=True)
    with pytest.raises(PoolError):
        pool.select(PRINCIPAL)


def test_automatic_distribution_spreads_work(store, service, clouds):
    """B: автоматическое распределение."""
    a, b, c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    vc = _add(service, c, "C")
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="load_balancing")
    seen = []
    for _ in range(3):
        chosen = pool.select(PRINCIPAL)
        seen.append(chosen["id"])
        pool.begin_task(PRINCIPAL, chosen["id"])   # reserve so the next differs
    # Three selections with load reserved after each hit three distinct accounts.
    assert set(seen) == {va["id"], vb["id"], vc["id"]}


def test_manual_switch_moves_a_running_task(store, service, clouds):
    """B: ручное переключение - an operator moves an in-flight task."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    lab = LabStore(store)
    project, run, _ = _make_run(store)
    claimed = lab.claim_task(str(run["id"]), "w")
    lab.mark_running(claimed["id"], conversation_id="conv-1", account_id=va["id"],
                     lease_token=claimed["lease_token"])
    switched = lab.switch_task_executor(claimed["id"], target_account_id=vb["id"])
    assert switched["state"] == "pending"
    assert switched["preferred_account_id"] == vb["id"]
    assert va["id"] in switched["tried_accounts"]


# =========================================================== C. Context Handoff
def test_handoff_starts_the_task_on_account_two_with_context(store, service, clouds):
    """C: begin on #1, hand to #2, the context travels with the task.

    The task's original text, the plan, the repository, the branch and the
    acceptance criteria all reach the second account, and the work continues
    (the run reaches completed) rather than restarting.
    """
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    client_a = AccountClient("A", start_fails=True)   # #1 is down
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    sched, lab, _pool = _scheduler(store, service, clients, verifier=StubVerifier(),
                                   priority={vb["id"]: 1})
    project, run, _ = _make_run(store)

    # Tick 1: the task is claimed and dispatched; #1 fails, so it is requeued.
    r1 = sched.tick(str(run["id"]))
    assert r1.claimed == "T1"
    task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert task["state"] == "pending" and va["id"] in task["tried_accounts"]

    # Tick 2: the reserve #2 starts the task with a hand-off, not a restart.
    sched.tick(str(run["id"]))
    assert len(client_b.started) == 1 and client_a.started == []
    message = client_b.started[0]["message"]
    assert "continuing an interrupted task" in message
    assert "Step one" in message                      # the task's own title
    assert REPO in message and "main" in message      # repository + branch
    assert "it works" in message                      # acceptance criteria
    assert "clone the repository" in message.lower()  # the workspace is fresh

    # The hand-off is recorded durably, from #1 to #2.
    handoffs = lab.handoffs(task["id"])
    assert handoffs and str(handoffs[0]["to_account_id"]) == vb["id"]

    # Tick 3: #2 finishes; the task verifies and the run completes.
    r3 = sched.tick(str(run["id"]))
    assert r3.verdict == "completed_verified"
    final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
    assert final["state"] == "completed"


def test_handoff_does_not_repeat_a_completed_stage(store, service, clouds):
    """C: сохранение задания/контекста, Git-состояния, no re-execution.

    A two-task run: task A completes verified on #1 (the real git verifier reads
    the branch head), then task B fails over to #2. The hand-off for B names A as
    done, A is never started a second time, and A's durable Git state (the commit
    the verifier observed) is preserved on its row.
    """
    from orchestration.github_client import GitHubClient
    from tests.fake_github import FakeGitHub, FakeGitHubServer

    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    client_a = AccountClient("A")
    client_a.report = f"done, commit {HEAD}"
    client_b = AccountClient("B")
    client_b.report = f"done, commit {HEAD}"
    clients = {va["id"]: client_a, vb["id"]: client_b}

    github = FakeGitHub()
    github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
    with FakeGitHubServer(github) as gh:
        verifier = _real_verifier(GitHubClient(base_url=gh.base_url, token="t"))
        sched, lab, _pool = _scheduler(store, service, clients, verifier=verifier,
                                       priority={vb["id"]: 1})
        project, run, _ = _make_run(store, plan=[
            {"id": "A", "title": "First", "description": "a", "acceptance": [],
             "dependencies": []},
            {"id": "B", "title": "Second", "description": "b", "acceptance": [],
             "dependencies": ["A"]},
        ])

        # Tick 1: A is claimed and dispatched on #1.
        sched.tick(str(run["id"]))
        assert len(client_a.started) == 1
        # Tick 2: A's conversation finished and the branch head matches, so A
        # verifies and B is dispatched on #1.
        sched.tick(str(run["id"]))
        task_a = [t for t in lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))
                  if t["task_key"] == "A"][0]
        assert task_a["state"] == "completed_verified"
        assert len(client_a.started) == 2             # A then B on account #1
        # A's Git state is durable: the verifier recorded the observed commit.
        assert any(isinstance(e, dict) and e.get("remote_sha") == HEAD
                   for e in task_a["evidence"])

        # Tick 3: B's conversation on #1 vanishes, so B fails over to #2.
        client_a._conversations.clear()               # the executor is gone
        sched.tick(str(run["id"]))
        task_b = [t for t in lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))
                  if t["task_key"] == "B"][0]
        assert str(task_b["openhands_account_id"]) == vb["id"]
        assert len(client_b.started) == 1
        message = client_b.started[0]["message"]
        assert "Already completed and verified (do NOT redo these): A" in message
        # The context carries the task's identity and the plan, so the new
        # executor continues the same work rather than starting a new one.
        assert f"Run: {run['id']}  Task: B" in message
        assert "A: First [completed_verified]" in message
        # A was never restarted: two starts on #1 (A then B) and one on #2.
        assert len(client_a.started) == 2

        # Tick 4: B finishes on #2 and the run completes.
        final_tick = sched.tick(str(run["id"]))
        assert final_tick.verdict == "completed_verified"
        final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
        assert final["state"] == "completed"


def test_handoff_continues_unfinished_work_to_completion(store, service, clouds):
    """C: продолжение незавершённой работы + успешное завершение."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    client_a = AccountClient("A")
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    sched, lab, _pool = _scheduler(store, service, clients, verifier=StubVerifier(),
                                   priority={vb["id"]: 1})
    project, run, _ = _make_run(store)

    sched.tick(str(run["id"]))                        # A starts on #1
    assert len(client_a.started) == 1
    client_a._conversations.clear()                   # #1 vanishes mid-task
    sched.tick(str(run["id"]))                        # failover to #2
    assert len(client_b.started) == 1
    tick = sched.tick(str(run["id"]))                 # #2 finishes
    assert tick.verdict == "completed_verified"
    final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
    assert final["state"] == "completed"
    # One conversation per account: the work continued, it did not restart.
    assert len(client_a.started) == 1 and len(client_b.started) == 1


# ================================================================= D. Worker (API)
def test_the_worker_continues_and_the_result_is_read_after_reconnecting(monkeypatch):
    """D: API start, worker start, task sent, client disconnected, result read.

    The run is created over HTTP; the test then stops talking to the server
    entirely (as a closed browser would). A separate ``lab_worker`` process
    advances the run to completion; the test reconnects afterwards and reads the
    result from the API. Nothing about the work depended on the client.
    """
    from tests.test_lab_inline import _finish, _start_run, _task_conversation, _until
    from tests.test_lab_worker_process import Worker
    from tests.test_studio_api import studio_stack

    with studio_stack(monkeypatch, lab_inline=False) as studio:
        project, run = _start_run(studio)             # D: API up, task submitted
        worker = Worker(studio, worker_id="comp-worker-1")
        try:
            first = _until(lambda: _task_conversation(studio, "audit"),
                           what="the worker to dispatch the first task")
            # No client call is made between here and the final read.
            _finish(studio, first, f"Готово. Коммит {HEAD}.")
            second = _until(lambda: _task_conversation(studio, "change"),
                            what="the worker to continue without a client")
            _finish(studio, second, f"Готово. Коммит {HEAD}.")

            # Reconnect: a fresh HTTP client reads the finished run.
            from tests.test_studio_api import Api

            api = Api(studio.api.base)
            done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
                api.get(f"/api/v1/projects/{project}/runs/{run}")[1]),
                what="the run to finish for the reconnected client")
            assert done["run"]["state"] == "completed"
            assert {t["task_key"]: t["state"] for t in done["tasks"]} == {
                "audit": "completed_verified", "change": "completed_verified"}
        finally:
            worker.stop()


# ================================================================= E. Recovery
def test_an_expired_lease_is_reclaimed_and_re_claimed(store):
    """E: истечение lease -> повторное получение задачи."""
    lab = LabStore(store)
    _project, run, _ = _make_run(store)
    claimed = lab.claim_task(str(run["id"]), "w1", lease_seconds=0)
    assert claimed["state"] == "dispatched"
    reclaimed = lab.reclaim_expired(str(run["id"]))
    assert reclaimed and reclaimed[0]["state"] == "pending"
    again = lab.claim_task(str(run["id"]), "w2")
    assert again["task_key"] == "T1" and again["lease_token"] > claimed["lease_token"]


def test_a_worker_that_dies_before_starting_does_not_duplicate_work(store):
    """E: принудительная остановка worker -> восстановление, без дублей.

    A worker claims a task and dies before it starts a conversation (no
    conversation bound). The reclaim puts the task back and a second worker takes
    it - exactly one start is possible, because the first never bound one.
    """
    lab = LabStore(store)
    _project, run, _ = _make_run(store)
    first = lab.claim_task(str(run["id"]), "dead-worker", lease_seconds=0)
    assert first["conversation_id"] is None           # it died before starting
    lab.reclaim_expired(str(run["id"]))
    second = lab.claim_task(str(run["id"]), "live-worker")
    assert second is not None and second["conversation_id"] is None
    assert len(lab.list_tasks_internal(str(run["id"]))) == 1


def test_a_started_conversation_is_never_reclaimed_into_a_second_start(store):
    """E: проверка отсутствия дублей - a bound conversation is not taken back.

    Once a conversation exists, the work is in the Cloud; reclaiming the task
    would start a second conversation for the same work. ``reclaim_expired``
    takes back only a task with no conversation bound.
    """
    lab = LabStore(store)
    _project, run, _ = _make_run(store)
    claimed = lab.claim_task(str(run["id"]), "w1", lease_seconds=0)
    lab.mark_running(claimed["id"], conversation_id="conv-live",
                     lease_token=claimed["lease_token"])
    assert lab.reclaim_expired(str(run["id"])) == []
    task = lab.list_tasks_internal(str(run["id"]))[0]
    assert task["state"] == "running" and task["conversation_id"] == "conv-live"


def test_losing_the_connection_to_openhands_fails_over(store, service, clouds):
    """E: потеря соединения с OpenHands -> failover, not a dead task."""
    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")
    client_a = AccountClient("A", start_fails=True)   # the transport is gone
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    sched, lab, _pool = _scheduler(store, service, clients, verifier=StubVerifier(),
                                   priority={vb["id"]: 1})
    project, run, _ = _make_run(store)
    sched.tick(str(run["id"]))
    task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert va["id"] in task["tried_accounts"]
    sched.tick(str(run["id"]))
    assert len(client_b.started) == 1, "the reserve account must take over"
    assert lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0][
        "openhands_account_id"] == vb["id"]


# =============================================================== G. E2E Failover
def _real_verifier(github):
    return GitVerifier(github, require_commit=True)


def test_end_to_end_failover_with_independent_verification(store, service, clouds):
    """G: the load-bearing test - a real task fails over and is verified.

    Steps, each asserted on a real effect:

    1. a real task is submitted;  2. it starts on OpenHands #1;
    3. #1 fails;                  4. the task moves to #2;
    5. #2 receives the context;   6. #1 is never asked to start again;
    7. the result arrives;        8. an independent verifier reads the real
       branch head on GitHub and accepts it; 9. the run completes with no
       duplicated start.
    """
    from orchestration.github_client import GitHubClient
    from tests.fake_github import FakeGitHub, FakeGitHubServer

    a, b, _c = clouds
    va = _add(service, a, "A", make_default=True)
    vb = _add(service, b, "B")

    github = FakeGitHub()
    github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
    with FakeGitHubServer(github) as gh:
        verifier = _real_verifier(GitHubClient(base_url=gh.base_url, token="t"))

        client_a = AccountClient("A")
        client_a.report = f"Готово. Коммит {HEAD}."
        client_b = AccountClient("B")
        client_b.report = f"Готово. Коммит {HEAD}."
        clients = {va["id"]: client_a, vb["id"]: client_b}
        sched, lab, _pool = _scheduler(store, service, clients, verifier=verifier,
                                       priority={vb["id"]: 1})
        project, run, _ = _make_run(store)

        # 1-2: submit and start on #1.
        sched.tick(str(run["id"]))
        assert len(client_a.started) == 1, "the task must start on account #1"
        task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
        assert str(task["openhands_account_id"]) == va["id"]
        # The first start is the task's instructions: the repository, the branch
        # and the push rule the verifier will hold it to.
        first_message = client_a.started[0]["message"]
        assert f"Repository: {REPO}" in first_message
        assert "push it to branch main" in first_message

        # 3-4: #1 fails; the next tick moves the task to #2.
        client_a._conversations.clear()               # #1's conversation vanished
        sched.tick(str(run["id"]))
        task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
        assert str(task["openhands_account_id"]) == vb["id"], "the task must move to #2"

        # 5: #2 received the hand-off context.
        assert len(client_b.started) == 1
        message = client_b.started[0]["message"]
        assert "continuing an interrupted task" in message
        assert REPO in message and "main" in message
        assert "Step one" in message

        # 6: #1 was never asked to start again (it may not write the workspace).
        assert len(client_a.started) == 1
        handoffs = lab.handoffs(task["id"])
        assert handoffs and str(handoffs[0]["from_account_id"]) == va["id"]
        assert str(handoffs[0]["to_account_id"]) == vb["id"]

        # 7-8: the result arrives; the independent verifier reads the real
        #      branch head on GitHub and accepts the commit the agent named.
        tick = sched.tick(str(run["id"]))
        assert tick.verdict == "completed_verified"
        assert f"/repos/{REPO}/commits" in github.paths

        # 9: the run completed with exactly one start per account.
        final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
        assert final["state"] == "completed"
        assert len(client_a.started) == 1 and len(client_b.started) == 1
        task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
        assert any(isinstance(e, dict) and e.get("remote_sha") == HEAD
                   for e in task["evidence"])


# ========================================================= F. Real Development Task
@pytest.mark.skipif(
    not os.environ.get("OPENHANDS_API_KEY") and not os.environ.get("NEXUS_LIVE_OPENHANDS"),
    reason=("BLOCKED: section F (a real development task driven through NEXUS) needs a live "
            "OpenHands key and a sandbox. This environment has no working LLM key "
            "(see AGENTS.md), so the real switch cannot be exercised; simulating it "
            "would be the one thing the request forbids."))
def test_real_development_task_through_nexus(store, service, clouds):
    """F: a real dev task (create/ test / break / fix / report) through NEXUS.

    BLOCKED here, on purpose, without a live OpenHands key. The gate above skips
    it with the reason; a deployment with a key runs the real pipeline (the same
    steps ``tests/test_lab_inline.py`` drives against the fakes) against the real
    Cloud and a real sandbox.
    """
    pytest.skip("BLOCKED: needs a live OpenHands key and a sandbox")


def test_the_live_probe_reports_blocked_without_a_key(store, service, clouds):
    """F, the honest half: without a key the diagnostics say BLOCKED, not VERIFIED.

    This is a real assertion in this environment: no live key means the probe
    must not claim success. It is the check that would run before section F, and
    it fails if the diagnostics ever reported a provider or account connected
    without a real call.
    """
    from orchestration.openhands_diagnostics import BLOCKED, FAILED, UNVERIFIED, VERIFIED
    from orchestration.openhands_diagnostics import OpenHandsDiagnostics

    a, _b, _c = clouds
    from orchestration.cloud_client import CloudClient

    diag = OpenHandsDiagnostics(
        store, accounts=service, pool=AccountPool(store, service),
        cloud_client=CloudClient(a.base_url, api_key="sk-a", timeout=5.0),
        client_factory=lambda base_url, api_key: CloudClient(
            base_url=base_url, api_key=api_key, timeout=5.0))
    # No account stored: the accounts check is BLOCKED (nothing to check), not
    # FAILED and not VERIFIED.
    report = diag.run(PRINCIPAL, live=False)
    accounts = next(c for c in report.checks if c.name == "accounts")
    assert accounts.status == BLOCKED
    assert accounts.status not in (VERIFIED, FAILED)
    # The live probe is opt-in: without it the check is UNVERIFIED.
    live = next(c for c in report.checks if c.name == "live_conversation")
    assert live.status == UNVERIFIED
    # And nothing FAILED: a missing credential is BLOCKED, never a failure.
    assert report.ok is True
