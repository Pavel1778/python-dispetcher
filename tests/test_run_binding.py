"""A run must never work in a repository its project was not authorized for.

The incident this pins: run ``2ae6adc3-a243-4b9c-80d4-44b2254f3355`` - a run
for the NEXUS project - was directed into **ForgeCRM**, another project's
repository, because the lab path stored the repository from the request body
and started the conversation without re-checking the project↔repository link.
The global allowlist was enough to reach the repository; the project link was
not consulted at dispatch.

``LabScheduler._start`` now calls ``authorize_run_task`` immediately before the
conversation is created, exactly as the workflow dispatcher calls
``authorize_dispatch``. This module drives the real scheduler (over real
PostgreSQL, with a scripted executor) and asserts that a task for a repository
the project was not linked to is **never started**, and the run is blocked with
the reason - not silently redirected to a default repository.

ForgeCRM itself is never touched: the executor records what it was asked to
start, and no real GitHub or OpenHands call is made.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_lab import LabScheduler  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.test_durable_runs import Conversations  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

NEXUS = "Pavel1778/python-dispetcher"
FORGECRM = "Pavel1778/forgecrm"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def env():
    _reset()
    store = PostgresStore(DSN)
    projects = ProjectService(store)
    # Two projects, each with its own repository: the NEXUS project and the
    # ForgeCRM project. A task of the NEXUS project must never reach ForgeCRM.
    nexus = str(projects.create("user:alice", "NEXUS")["id"])
    forge = str(projects.create("user:alice", "ForgeCRM")["id"])
    store.add_repository(NEXUS, default_branch="main", allowed_branches=["main"])
    store.add_repository(FORGECRM, default_branch="main", allowed_branches=["main"])
    links = ProjectRepositoryService(store)
    links.link("user:alice", nexus, NEXUS)
    links.link("user:alice", forge, FORGECRM)
    goal = str(projects.create_goal("user:alice", nexus, "Починить NEXUS")[0]["id"])
    lab = LabStore(store)
    yield {"store": store, "lab": lab, "projects": projects, "nexus": nexus,
           "forge": forge, "goal": goal}
    store.close()


def _run_with_task(env, repository, *, policy="bounded"):
    run = env["lab"].start_run("user:alice", env["nexus"], env["goal"], policy=policy)
    env["lab"].add_task("user:alice", env["nexus"], run["id"], task_key="t1",
                        title="Починить", repository=repository, branch="main")
    return str(run["id"])


def test_a_task_for_another_projects_repository_is_never_started(env):
    """The incident: the run's project is NEXUS, the task names ForgeCRM."""
    run_id = _run_with_task(env, FORGECRM)
    executor = Conversations()
    sched = LabScheduler(env["lab"], executor, verifier=_NoVerifier(),
                         worker_id="w1", lease_seconds=900)
    sched.tick(run_id)

    # Nothing was started: no conversation for the ForgeCRM task.
    assert executor.started == [], "the task reached the executor"
    run = env["lab"].get_run("user:alice", env["nexus"], run_id)
    assert run["state"] == "blocked"
    task = env["lab"].list_tasks("user:alice", env["nexus"], run_id)[0]
    assert task["state"] == "blocked"
    reason = (task["verification"] or {}).get("reason", "")
    assert "not authorized" in reason and FORGECRM in reason


def test_a_task_for_the_projects_own_repository_is_started(env):
    """The same run, but the task names the repository the project is linked to:
    it is started. The gate refuses the wrong repository, not the right one."""
    run_id = _run_with_task(env, NEXUS)
    executor = Conversations()
    sched = LabScheduler(env["lab"], executor, verifier=_NoVerifier(),
                         worker_id="w1", lease_seconds=900)
    sched.tick(run_id)
    assert [key for key, _ in executor.started] == ["t1"]


def test_a_run_with_no_repository_is_not_blocked(env):
    """A repository-less task has nothing to authorize and still runs."""
    run_id = _run_with_task(env, "")
    executor = Conversations()
    sched = LabScheduler(env["lab"], executor, verifier=_NoVerifier(),
                         worker_id="w1", lease_seconds=900)
    sched.tick(run_id)
    assert [key for key, _ in executor.started] == ["t1"]


def test_removing_the_link_after_the_run_started_stops_the_task(env):
    """The link is re-checked at dispatch: a link removed between "the run was
    started" and "the task is dispatched" still stops the work."""
    run_id = _run_with_task(env, NEXUS)
    with env["store"]._tx() as cur:
        cur.execute("DELETE FROM nexus_project_repositories WHERE project_id=%s",
                    (env["nexus"],))
    executor = Conversations()
    sched = LabScheduler(env["lab"], executor, verifier=_NoVerifier(),
                         worker_id="w1", lease_seconds=900)
    sched.tick(run_id)
    assert executor.started == []
    assert env["lab"].get_run("user:alice", env["nexus"], run_id)["state"] == "blocked"


class _NoVerifier:
    """A verifier that is never reached: the gate refuses before any start."""

    def head(self, task):
        return None

    def verify(self, task, report):
        raise AssertionError("the verifier was reached for a task that must not start")
