"""Durable autonomy: retry cycles, reports, cancellation and multi-executor.

Real PostgreSQL. The scheduler is driven with small in-process fakes (as in
``test_lab_loop``) so the *loop* is what is under test; the store, the retry
cycles, the report rows and the cancel signalling are real.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudRateLimited  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    LabScheduler,
    TaskReport,
    Verification,
)
from orchestration.nexus_lab_store import (  # noqa: E402
    DEFAULT_MAX_RETRY_CYCLES,
    LabStore,
    retry_backoff_seconds,
)
from orchestration.nexus_project_executors import ProjectExecutorService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_reports import ReportBuilder, ReportDeliverer  # noqa: E402
from orchestration.openhands_pool import AccountPool, PoolError  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class FakeExecutor:
    """Dispatches instantly and reports a scripted outcome per task key."""

    def __init__(self, outcomes: dict[str, str] | None = None):
        self.outcomes = outcomes or {}
        self.dispatched: list[str] = []

    def dispatch(self, task):
        self.dispatched.append(task["task_key"])
        return f"conv-{task['task_key']}-{len(self.dispatched)}"

    def poll(self, task, conversation_id):
        state = self.outcomes.get(task["task_key"], "finished")
        return TaskReport(state=state, final_text="did work", conversation_id=conversation_id)


class FakeVerifier:
    """Verdicts by task key, defaulting to verified."""

    def __init__(self, verdicts: dict[str, str] | None = None):
        self.verdicts = dict(verdicts or {})
        self.calls: list[str] = []

    def verify(self, task, report):
        self.calls.append(task["task_key"])
        if report.state != "finished":
            return Verification("failed", "not finished", {"state": report.state})
        v = self.verdicts.get(task["task_key"], "completed_verified")
        return Verification(v, f"fake verdict for {task['task_key']}", {"k": task["task_key"]})


@pytest.fixture
def env():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Lab")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    lab = LabStore(store)
    yield {"store": store, "svc": svc, "project": project, "goal": goal, "lab": lab}
    store.close()


def _run_with_tasks(env, tasks, **opts):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"], **opts)
    for t in tasks:
        env["lab"].add_task("user:alice", env["project"], run["id"], **t)
    return run


def _run(env, run):
    return env["lab"].get_run("user:alice", env["project"], run["id"])


def _states(env, run):
    return {t["task_key"]: t["state"]
            for t in env["lab"].list_tasks("user:alice", env["project"], run["id"])}


def _task(env, run, key):
    return next(t for t in env["lab"].list_tasks("user:alice", env["project"], run["id"])
                if t["task_key"] == key)


def _sql(env, statement, *params):
    with env["store"]._tx() as cur:
        cur.execute(statement, params)


# ------------------------------------------------------------------ retry cycles
def test_a_failed_verification_does_not_end_the_run_for_good(env):
    """The requirement: after the attempts are spent the run retries, it does
    not stop for good."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}])
    verifier = FakeVerifier({"a": "failed"})
    sched = LabScheduler(env["lab"], FakeExecutor(), verifier)
    sched.run_until_done(run["id"])

    stopped = _run(env, run)
    assert stopped["state"] == "retrying"
    assert stopped["retry_attempt"] == 0 and stopped["max_retry_cycles"] == DEFAULT_MAX_RETRY_CYCLES
    assert stopped["retry_at"] is not None and stopped["retry_reason"]


def test_the_backoff_grows_and_is_capped():
    assert retry_backoff_seconds(0) == 60.0
    assert retry_backoff_seconds(1) == 120.0
    assert retry_backoff_seconds(2) == 240.0
    assert retry_backoff_seconds(20) == 3600.0
    assert retry_backoff_seconds(-3) == 60.0


def test_a_retry_cycle_restarts_the_tasks_and_the_budget(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}])
    verifier = FakeVerifier({"a": "failed"})
    sched = LabScheduler(env["lab"], FakeExecutor(), verifier)
    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "retrying"
    assert _task(env, run, "a")["state"] == "failed"

    # The wait elapsed; the next cycle restarts the task with a fresh attempt
    # budget and dispatches it in the same tick, the cause fixed.
    _sql(env, "UPDATE nexus_runs SET retry_at = now() - interval '1 second' WHERE id=%s",
         run["id"])
    verifier.verdicts.clear()
    result = sched.tick(run["id"])
    assert result.note == "retry cycle started"
    again = _task(env, run, "a")
    assert (again["state"], again["cycle"]) == ("running", 1)
    assert again["attempt"] == 1                     # the fresh cycle's first attempt
    # The attempt that is over stays on record with its conversation.
    assert any(e.get("requeued") == "retry-cycle" for e in again["evidence"])

    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "completed"


def test_cycles_are_bounded_and_then_the_run_blocks(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}],
                          max_retry_cycles=2)
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier({"a": "failed"}))
    # Cycle after cycle, until the cycles are spent.
    for _ in range(6):
        _sql(env, "UPDATE nexus_runs SET retry_at = now() - interval '1 second' "
                  "WHERE id=%s AND state='retrying'", run["id"])
        sched.tick(run["id"])
        sched.tick(run["id"])
    final = _run(env, run)
    assert final["state"] == "blocked"
    assert final["retry_attempt"] == 2 and final["max_retry_cycles"] == 2


def test_a_cancelled_run_is_never_retried(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}])
    env["lab"].cancel_run("user:alice", env["project"], run["id"])
    assert env["lab"].schedule_retry(run["id"], reason="x") is None


# ------------------------------------------------------------------- reports
def test_a_completed_run_writes_exactly_one_report(env):
    env["store"].add_repository("owner/repo", default_branch="main")
    ProjectRepositoryService(env["store"]).link("user:alice", env["project"], "owner/repo")
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A",
                                 "repository": "owner/repo", "branch": "main"}])
    reports = ReportBuilder(env["store"], env["lab"])
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier(), reports=reports)
    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "completed"

    first = env["lab"].report_for_run(run["id"])
    assert first is not None and first["status"] == "pending"
    assert "Отчёт по автономному прогону" in first["content"]
    assert "owner/repo" in first["content"]

    # A second write (a retried finish, two workers) is absorbed: still one.
    text, meta = reports.build(run["id"])
    env["lab"].save_report(run["id"], content=text, meta=meta)
    assert env["lab"].report_for_run(run["id"])["id"] == first["id"]
    with env["store"]._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM nexus_run_reports WHERE run_id=%s", (run["id"],))
        assert cur.fetchone()["n"] == 1


def test_a_report_is_delivered_to_the_source_chat_once(env):
    conversation = None
    with env["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
                    "VALUES (%s,'chat','autonomous','user:alice') RETURNING id",
                    (env["project"],))
        conversation = str(cur.fetchone()["id"])
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}],
                          source_conversation_id=conversation)
    reports = ReportBuilder(env["store"], env["lab"])
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier(), reports=reports)
    sched.run_until_done(run["id"])

    from orchestration.nexus_conversations import ConversationService
    deliverer = ReportDeliverer(env["store"], env["lab"], ConversationService(env["store"]))
    tally = deliverer.deliver_pending()
    assert tally == {"delivered": 1, "failed": 0}
    assert env["lab"].report_for_run(run["id"])["status"] == "sent"

    # A second pass delivers nothing: the report is never posted twice.
    assert deliverer.deliver_pending() == {"delivered": 0, "failed": 0}
    messages = ConversationService(env["store"]).list_messages(
        "user:alice", env["project"], conversation)
    reports_in_chat = [m for m in messages if (m.get("meta") or {}).get("kind") == "run_report"]
    assert len(reports_in_chat) == 1


def test_a_report_left_pending_after_a_restart_is_delivered(env):
    """Delivery is durable: a report written but not sent is picked up later."""
    with env["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
                    "VALUES (%s,'chat','autonomous','user:alice') RETURNING id",
                    (env["project"],))
        conversation = str(cur.fetchone()["id"])
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}],
                          source_conversation_id=conversation)
    env["lab"].save_report(run["id"], content="Отчёт по автономному прогону", meta={})
    assert env["lab"].report_for_run(run["id"])["status"] == "pending"

    from orchestration.nexus_conversations import ConversationService
    deliverer = ReportDeliverer(env["store"], env["lab"], ConversationService(env["store"]))
    assert deliverer.deliver_pending()["delivered"] == 1
    assert env["lab"].report_for_run(run["id"])["status"] == "sent"


def test_a_failed_delivery_is_retried_with_a_backoff(env):
    with env["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
                    "VALUES (%s,'chat','autonomous','user:alice') RETURNING id",
                    (env["project"],))
        conversation = str(cur.fetchone()["id"])
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}],
                          source_conversation_id=conversation)
    env["lab"].save_report(run["id"], content="x", meta={})

    class Broken:
        def post_internal(self, *a, **k):
            raise RuntimeError("chat unavailable")

    deliverer = ReportDeliverer(env["store"], env["lab"], Broken())
    assert deliverer.deliver_pending()["failed"] == 1
    report = env["lab"].report_for_run(run["id"])
    assert report["status"] == "pending" and report["attempts"] == 1
    assert report["next_attempt_at"] is not None
    # Not due yet, so a second pass does not try.
    assert deliverer.deliver_pending() == {"delivered": 0, "failed": 0}


def test_a_report_with_no_conversation_is_settled(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    env["lab"].save_report(run["id"], content="x", meta={})
    from orchestration.nexus_conversations import ConversationService
    deliverer = ReportDeliverer(env["store"], env["lab"], ConversationService(env["store"]))
    deliverer.deliver_pending()
    assert env["lab"].report_for_run(run["id"])["status"] == "sent"


# ------------------------------------------------------------------ cancel/hide
def test_a_cancelled_run_is_hidden_from_the_list_but_kept(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    cancelled = env["lab"].cancel_run("user:alice", env["project"], run["id"])
    assert cancelled["state"] == "cancelled" and cancelled["hidden_at"] is not None
    assert env["lab"].list_runs("user:alice", env["project"]) == []
    # Kept: readable by id, with the history.
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "cancelled"
    # ...and listed when asked for explicitly.
    assert len(env["lab"].list_runs("user:alice", env["project"], include_hidden=True)) == 1


def test_cancelling_is_idempotent(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    first = env["lab"].cancel_run("user:alice", env["project"], run["id"])
    second = env["lab"].cancel_run("user:alice", env["project"], run["id"])
    assert first["state"] == second["state"] == "cancelled"
    assert second["hidden_at"] == first["hidden_at"]


def test_a_cancelled_run_starts_nothing_and_settles_its_tasks(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    executor = FakeExecutor()
    sched = LabScheduler(env["lab"], executor, FakeVerifier())
    env["lab"].cancel_run("user:alice", env["project"], run["id"])
    sched.tick(run["id"])
    assert executor.dispatched == []
    assert _states(env, run) == {"a": "blocked"}


def test_a_cancelled_run_signals_its_in_flight_conversation(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    task = env["lab"].claim_task(run["id"], "w1")
    env["lab"].mark_running(task["id"], conversation_id="conv-a")
    env["lab"].cancel_run("user:alice", env["project"], run["id"])
    needing = env["lab"].conversations_needing_stop(run["id"])
    assert [t["conversation_id"] for t in needing] == ["conv-a"]
    env["lab"].mark_conversation_stopped(task["id"], conversation_id="conv-a")
    assert env["lab"].conversations_needing_stop(run["id"]) == []


# ------------------------------------------------------------ permanent failure
def test_a_permanent_failure_blocks_the_run_instead_of_retrying(env):
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}])
    task = env["lab"].claim_task(run["id"], "w1")
    env["lab"].release_task(task["id"], error="DeliveryError: no", requeue=False,
                            permanent=True)
    assert _task(env, run, "a")["state"] == "failed"
    assert env["lab"].has_retryable_failure(run["id"]) is False
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier())
    sched.tick(run["id"])
    assert _run(env, run)["state"] == "blocked"


# ------------------------------------------------------- project executors
def _account(store, principal="user:alice", label="A"):
    from orchestration.nexus_openhands_accounts import OpenHandsAccountService
    os.environ.setdefault("NEXUS_SECRET_KEY", "test-secret-key-for-encryption")
    service = OpenHandsAccountService(store)
    return service


def test_executor_assignment_is_project_scoped(env):
    service = ProjectExecutorService(env["store"])
    # Two accounts owned by the principal.
    with env["store"]._tx() as cur:
        ids = []
        for label in ("A", "B"):
            cur.execute("INSERT INTO nexus_openhands_accounts"
                        "(principal, label, base_url, api_key_encrypted) "
                        "VALUES ('user:alice',%s,'https://x', '\\x00'::bytea) RETURNING id",
                        (label,))
            ids.append(str(cur.fetchone()["id"]))
    service.replace_all("user:alice", env["project"], primary=ids[0], fallbacks=[ids[1]])
    assigned = service.assigned_account_ids(env["project"])
    assert assigned == [ids[0], ids[1]]
    # A second project has no assignment: nothing of the first leaks to it.
    other = str(env["svc"].create("user:alice", "Other")["id"])
    assert service.assigned_account_ids(other) == []


def test_the_pool_is_restricted_to_a_projects_assigned_executors(env):
    with env["store"]._tx() as cur:
        ids = []
        for label in ("A", "B"):
            cur.execute("INSERT INTO nexus_openhands_accounts"
                        "(principal, label, base_url, api_key_encrypted, is_default) "
                        "VALUES ('user:alice',%s,'https://x','\\x00'::bytea,%s) "
                        "RETURNING id", (label, label == "A"))
            ids.append(str(cur.fetchone()["id"]))
    executors = ProjectExecutorService(env["store"])
    from orchestration.nexus_openhands_accounts import OpenHandsAccountService
    pool = AccountPool(env["store"], OpenHandsAccountService(env["store"]),
                       assignments=executors)
    # Only B is assigned to the project, though A is the default.
    executors.replace_all("user:alice", env["project"], primary=None, fallbacks=[ids[1]])
    chosen = pool.select("user:alice", project_id=env["project"])
    assert str(chosen["id"]) == ids[1]
    # With nothing assigned, the default (A) is used, as before.
    other = str(env["svc"].create("user:alice", "Other")["id"])
    assert str(pool.select("user:alice", project_id=other)["id"]) == ids[0]


def test_a_project_with_only_an_unusable_assigned_executor_refuses(env):
    with env["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_openhands_accounts"
                    "(principal, label, base_url, api_key_encrypted, enabled) "
                    "VALUES ('user:alice','A','https://x','\\x00'::bytea,false) RETURNING id")
        account = str(cur.fetchone()["id"])
    executors = ProjectExecutorService(env["store"])
    from orchestration.nexus_openhands_accounts import OpenHandsAccountService
    pool = AccountPool(env["store"], OpenHandsAccountService(env["store"]),
                       assignments=executors)
    executors.replace_all("user:alice", env["project"], primary=account, fallbacks=[])
    with pytest.raises(PoolError):
        pool.select("user:alice", project_id=env["project"])


# ------------------------------------------------------------- multi-repository
def test_a_project_links_several_repositories_from_different_accounts(env):
    repos = ProjectRepositoryService(env["store"])
    with env["store"]._tx() as cur:
        accounts = []
        for label, login in (("A", "acme"), ("B", "other")):
            cur.execute("INSERT INTO nexus_github_accounts"
                        "(principal, label, login, token_encrypted) "
                        "VALUES ('user:alice',%s,%s,'\\x00'::bytea) RETURNING id",
                        (label, login))
            accounts.append(str(cur.fetchone()["id"]))
    env["store"].add_repository("acme/frontend", default_branch="main")
    env["store"].add_repository("other/backend", default_branch="main")
    repos.link("user:alice", env["project"], "acme/frontend",
               account_id=accounts[0], make_primary=True)
    repos.link("user:alice", env["project"], "other/backend", account_id=accounts[1])

    listed = repos.list("user:alice", env["project"])
    assert [r["full_name"] for r in listed] == ["acme/frontend", "other/backend"]
    assert listed[0]["is_primary"] and listed[0]["account_login"] == "acme"
    assert listed[1]["account_login"] == "other"
    # The identity that reads each repository is per repository.
    assert repos.account_for_repository(env["project"], "acme/frontend") == (
        "user:alice", accounts[0])
    assert repos.account_for_repository(env["project"], "other/backend") == (
        "user:alice", accounts[1])


def test_a_projects_repository_account_must_belong_to_the_same_principal(env):
    repos = ProjectRepositoryService(env["store"])
    with env["store"]._tx() as cur:
        cur.execute("INSERT INTO nexus_github_accounts"
                    "(principal, label, login, token_encrypted) "
                    "VALUES ('user:bob','B','bob','\\x00'::bytea) RETURNING id")
        bob = str(cur.fetchone()["id"])
    env["store"].add_repository("bob/thing", default_branch="main")
    from orchestration.cloud_store import NotFound as _NotFound
    with pytest.raises(_NotFound):
        repos.link("user:alice", env["project"], "bob/thing", account_id=bob)


def test_only_one_primary_repository_per_project(env):
    repos = ProjectRepositoryService(env["store"])
    env["store"].add_repository("acme/a", default_branch="main")
    env["store"].add_repository("acme/b", default_branch="main")
    repos.link("user:alice", env["project"], "acme/a", make_primary=True)
    repos.link("user:alice", env["project"], "acme/b", make_primary=True)
    listed = repos.list("user:alice", env["project"])
    primaries = [r["full_name"] for r in listed if r["is_primary"]]
    assert primaries == ["acme/b"]
    # set_primary moves it back.
    repos.set_primary("user:alice", env["project"], "acme/a")
    listed = repos.list("user:alice", env["project"])
    assert [r["full_name"] for r in listed if r["is_primary"]] == ["acme/a"]


# ----------------------------------------------------------- idempotent recovery
def test_two_workers_do_not_start_a_retry_cycle_twice(env):
    from concurrent.futures import ThreadPoolExecutor
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A", "max_attempts": 1}],
                          max_retry_cycles=5)
    sched = LabScheduler(env["lab"], FakeExecutor(), FakeVerifier({"a": "failed"}))
    sched.run_until_done(run["id"])
    assert _run(env, run)["state"] == "retrying"
    _sql(env, "UPDATE nexus_runs SET retry_at = now() - interval '1 second' WHERE id=%s",
         run["id"])

    def start():
        return env["lab"].start_retry_cycle(run["id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: start(), range(4)))
    assert sum(1 for r in results if r is not None) == 1
    assert _run(env, run)["retry_attempt"] == 1
    assert _task(env, run, "a")["cycle"] == 1


def test_a_refused_start_is_not_an_attempt_even_across_cycles(env):
    """A 429 gives the attempt and the iteration back; nothing is spent."""
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    task = env["lab"].claim_task(run["id"], "w1")
    env["lab"].postpone_task(task["id"], error=str(CloudRateLimited("429")), seconds=30)
    back = _task(env, run, "a")
    assert (back["state"], back["attempt"]) == ("pending", 0)
    assert _run(env, run)["iteration"] == 0


def test_a_run_that_names_no_cycles_gets_the_default(env):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"])
    assert run["max_retry_cycles"] == DEFAULT_MAX_RETRY_CYCLES
    assert run["total_iterations"] == 0
