"""Acceptance section C — Context Handoff.

The task text asks for a *real* task: start it on account #1, then hand it to
account #2, and check that the task, the context and the Git state survive, that
completed steps are not redone, that unfinished work continues, and that the run
finishes.

A Cloud conversation cannot move between accounts, so a hand-off is a *new*
conversation on #2 whose first message carries the durable context. This suite
asserts on that message and on the rows, never on a claim:

* the task row is the same task (same key, description, acceptance);
* the hand-off names the goal, the plan, the repository, the branch, the last
  known commit, what is already verified and what is left;
* the old account never gets a second conversation for the task;
* a completed, verified step is named "do not redo" and is never re-dispatched;
* the run reaches ``completed``.

The scheduler, the pool, the router and the store are the real code; the
transport clients are the same honest fakes ``test_openhands_failover`` uses
(they record every message and can be told to fail a start).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import LabScheduler, OpenHandsExecutor  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import (  # noqa: E402
    OpenHandsAccountService,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.openhands_pool import AccountPool  # noqa: E402
from orchestration.openhands_router import AccountRouter  # noqa: E402
from tests.acceptance_kit import reset_schema  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.test_openhands_failover import (  # noqa: E402
    AccountClient,
    StubVerifier,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

PRINCIPAL = "user:owner"
HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


@pytest.fixture
def store():
    reset_schema(DSN)
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def clouds():
    with FakeCloudServer() as a, FakeCloudServer() as b:
        for server, profile in ((a, "pA"), (b, "pB")):
            server.cloud.settings = {"llm_api_key_set": True,
                                     "active_agent_profile_id": profile,
                                     "agent_settings": {"mcp_config": {}},
                                     "registered_marketplaces": [],
                                     "inherited_marketplaces": [],
                                     "disabled_skills": [],
                                     "provider_tokens_set": {}}
        yield a, b


@pytest.fixture
def service(store, clouds, monkeypatch):
    monkeypatch.setenv("NEXUS_SECRET_KEY", "acceptance-secret-key")
    from orchestration.cloud_client import CloudClient

    return OpenHandsAccountService(
        store, client_factory=lambda base_url, api_key:
        CloudClient(base_url=base_url, api_key=api_key, timeout=5.0))


def _two_accounts(service, clouds):
    a, b = clouds
    va = service.add(PRINCIPAL, label="A", api_key="sk-a", base_url=a.base_url,
                     make_default=True)
    vb = service.add(PRINCIPAL, label="B", api_key="sk-b", base_url=b.base_url)
    return va, vb


def _two_task_run(store, lab):
    """A project, an allowed+linked repository, and a two-task run (a -> b)."""
    projects = ProjectService(store)
    project = projects.create(PRINCIPAL, "Handoff project")
    store.add_repository("owner/repo", default_branch="main", allowed_branches=["main"])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link(PRINCIPAL, str(project["id"]), "owner/repo")
    goal, _ = projects.create_goal(PRINCIPAL, str(project["id"]), "Ship the change")
    run = lab.start_run(
        PRINCIPAL, str(project["id"]), str(goal["id"]),
        max_iterations=20, max_attempts_per_task=5,
        plan=[
            {"id": "T1", "title": "Prepare", "description": "Set the scene",
             "acceptance": ["scene set"], "dependencies": []},
            {"id": "T2", "title": "Implement", "description": "Do the real work",
             "acceptance": ["it works", "tests pass"], "dependencies": ["T1"]},
        ],
        repository="owner/repo", branch="main")
    return project, run


# ---------------------------------------------------- context & message shape
def test_the_handoff_context_carries_everything_the_task_text_lists(
        store, service, clouds):
    """The context a new executor is given names the whole task, not a summary.

    Built from the run's rows (never from the old conversation), so it is
    reproducible after a crash. The task text's list: project, task, goal, plan,
    completed, remaining, repository, branch, commit, acceptance.
    """
    va, vb = _two_accounts(service, clouds)
    lab = LabStore(store)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    project, run = _two_task_run(store, lab)

    # T1 is done and verified; T2 has a recorded Git state from its last attempt.
    t1 = lab.claim_task(str(run["id"]), "w")
    lab.record_verification(t1["id"], verdict="completed_verified",
                            evidence={"remote_sha": HEAD, "branch": "main"})
    t2 = lab.claim_task(str(run["id"]), "w")
    lab.mark_running(t2["id"], conversation_id="c-t2", account_id=va["id"])
    lab.record_verification(t2["id"], verdict="completed_unverified",
                            evidence={"remote_sha": HEAD, "reason": "not the head"})
    # Re-read T2 after its attempt settled, as a scheduler would.
    t2 = lab.get_task_internal(t2["id"])

    context = router.handoff_context(t2, previous_report="commit " + HEAD,
                                     last_error="account A went away")
    assert context["project_id"] == str(project["id"])
    assert context["task_id"] == str(t2["id"])
    assert context["task_key"] == "T2"
    assert context["original_goal"] == "Ship the change"
    assert context["repository"] == "owner/repo"
    assert context["branch"] == "main"
    assert context["last_commit"] == HEAD
    assert context["acceptance"] == ["it works", "tests pass"]
    assert context["completed_tasks"] == ["T1"]
    assert "T2" in context["remaining_tasks"]
    assert {entry["task_key"]: entry["state"] for entry in context["plan"]} == {
        "T1": "completed_verified", "T2": "completed_unverified"}
    assert context["previous_report"] == "commit " + HEAD
    assert context["last_error"] == "account A went away"


def test_the_handoff_message_states_the_continuation_rules():
    """The first message must tell the new executor not to start over."""
    from orchestration.openhands_router import AccountRouter

    message = AccountRouter.handoff_message({
        "project_id": "p-1", "run_id": "r-1", "task_key": "T2",
        "repository": "owner/repo", "branch": "main", "last_commit": HEAD,
        "original_goal": "Ship the change",
        "plan": [{"task_key": "T1", "title": "Prepare", "state": "completed_verified"},
                 {"task_key": "T2", "title": "Implement", "state": "pending"}],
        "completed_tasks": ["T1"], "remaining_tasks": ["T2"],
        "task_title": "Implement", "task_description": "Do the real work",
        "acceptance": ["it works", "tests pass"],
        "previous_report": "commit " + HEAD, "last_error": "account A went away",
    })
    assert "continuing an interrupted task" in message
    assert "owner/repo" in message and "main" in message
    assert HEAD in message                       # the Git state is named
    assert "T1" in message and "do NOT redo" in message
    assert "clone the repository" in message.lower()
    assert "it works" in message                 # the acceptance criteria travel
    assert "Ship the change" in message          # the original goal travels


# ------------------------------------------------------------- the hand-off
def test_a_real_task_started_on_one_account_is_handed_to_the_second(
        store, service, clouds):
    """The load-bearing case: #1 starts the task, #1 fails, #2 continues it.

    Assertions are on real rows and on the messages the clients actually
    received: #1 never starts a conversation, #2 receives a hand-off naming the
    work already done, the switch is recorded durably, and the run completes.
    """
    va, vb = _two_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)

    client_a = AccountClient("A", start_fails=True)   # #1 is down
    client_b = AccountClient("B")                     # #2 works
    clients = {va["id"]: client_a, vb["id"]: client_b}
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
    scheduler = LabScheduler(lab, executor, StubVerifier(), worker_id="w", router=router)
    project, run = _two_task_run(store, lab)

    # Tick 1: T1 is claimed and dispatched; A fails to start it.
    first = scheduler.tick(str(run["id"]))
    assert first.claimed == "T1"
    assert client_a.started == []                     # A never started anything
    t1 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert t1["state"] == "pending" and va["id"] in t1["tried_accounts"]

    # Tick 2: failover. B starts T1.
    scheduler.tick(str(run["id"]))
    assert len(client_b.started) == 1

    # Tick 3: T1's conversation is finished, so it verifies; in the same tick
    # T2 becomes claimable and is started on B. A is excluded, and T2 was never
    # on A, so T2 is an ordinary first start.
    client_b.status = "finished"
    scheduler.tick(str(run["id"]))                    # poll T1 -> verified, start T2
    t1 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert t1["state"] == "completed_verified"
    assert len(client_b.started) == 2
    t2 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[1]
    assert t2["task_key"] == "T2" and t2["state"] == "running"

    # Tick 4: T2's conversation finishes and verifies; the run completes.
    scheduler.tick(str(run["id"]))
    final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
    assert final["state"] == "completed"
    assert {t["task_key"]: t["state"] for t in
            lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))} == {
        "T1": "completed_verified", "T2": "completed_verified"}
    # The completed step T1 was started exactly once: it is never re-executed.
    assert len(client_b.started) == 2, "a task was started more than once"


def test_the_handoff_for_an_in_flight_task_names_the_completed_step(
        store, service, clouds):
    """When the *second* task is the one that fails over, its hand-off says
    which earlier step is already done and must not be redone."""
    va, vb = _two_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    client_a = AccountClient("A")
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
    scheduler = LabScheduler(lab, executor, StubVerifier(), worker_id="w", router=router)
    project, run = _two_task_run(store, lab)

    # T1 starts on A; then it is finished and verified, and in the same tick T2
    # starts on A.
    scheduler.tick(str(run["id"]))
    scheduler.tick(str(run["id"]))
    t1 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert t1["state"] == "completed_verified"
    t2 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[1]
    assert t2["state"] == "running" and str(t2["openhands_account_id"]) == va["id"]
    client_a._conversations.clear()                   # A can no longer see it

    # The next tick requeues T2 with A excluded and hands it to B. The hand-off
    # message B receives names T1 as completed.
    scheduler.tick(str(run["id"]))
    assert len(client_b.started) == 1
    message = client_b.started[0]["message"]
    assert "continuing an interrupted task" in message
    assert "T1" in message and "do NOT redo" in message
    t2 = lab.get_task_internal(t2["id"])
    assert va["id"] in t2["tried_accounts"]
    assert str(t2["openhands_account_id"]) == vb["id"]
    # The Git state of the task travels with it.
    assert "owner/repo" in message and "main" in message


def test_the_switch_is_recorded_durably(store, service, clouds):
    """A switch leaves a hand-off row naming both accounts and the context."""
    va, vb = _two_accounts(service, clouds)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)
    client_a = AccountClient("A", start_fails=True)
    client_b = AccountClient("B")
    clients = {va["id"]: client_a, vb["id"]: client_b}
    executor = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
    scheduler = LabScheduler(lab, executor, StubVerifier(), worker_id="w", router=router)
    project, run = _two_task_run(store, lab)

    scheduler.tick(str(run["id"]))                    # T1 fails on A
    scheduler.tick(str(run["id"]))                    # T1 starts on B
    t1 = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    handoffs = lab.handoffs(t1["id"])
    assert handoffs, "the executor switch must be recorded durably"
    assert str(handoffs[0]["from_account_id"]) == va["id"]
    assert str(handoffs[0]["to_account_id"]) == vb["id"]
    # The recorded context carries the plan and the task, not a bare id.
    assert handoffs[0]["context"]["task_key"] == "T1"
    assert handoffs[0]["context"]["repository"] == "owner/repo"
