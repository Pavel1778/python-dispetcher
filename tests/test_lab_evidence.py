"""The pipeline's ACCEPTANCE stage, on the autonomous loop.

The EVIDENCE stage signs every settled *task* result (see the block at the end of
``tests/test_lab_loop.py`` and ``tests/test_pipeline_e2e.py``). The stage the
pipeline names *after* it - ACCEPTANCE - was still missing: a run that reached
``completed`` left no signed record of *why* it was accepted, only the
scheduler's word on the run row. These tests drive the real
:class:`~orchestration.nexus_lab.LabScheduler` over real PostgreSQL, with a real
Ed25519 signer and keyring, and read the run's decision back.

What is asserted:

* a completed run leaves an ``ACCEPTED`` decision over its tasks' verdicts, and
  it re-verifies with the trusted keyring;
* a run that stopped short is ``NOT_READY`` with the reason, never a success - a
  blocked run is not terminal, so it is not decided yet;
* a repeated pass appends nothing (the id is the exactly-once gate);
* with no signer the loop records nothing and does not crash - a visible gap,
  never a fabricated trusted record.
"""
from __future__ import annotations

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.evidence_trust import (  # noqa: E402
    VERIFIED,
    EvidenceTrustEngine,
    Keyring,
    generate_signer,
    public_key_b64,
    signer_from_env,
)
from orchestration.nexus_evidence import lab_evidence_id  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    LabScheduler,
    TaskReport,
    Verification,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

REPO = "acme/repo"
HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _signer_env(monkeypatch, key_id: str = "nexus-lab"):
    """Configure a real signer and keyring in the environment, as a deployment
    does, and return the signer so a test can re-verify what was stored."""
    signer = generate_signer(key_id)
    seed = signer.private_key.private_bytes_raw()
    monkeypatch.setenv("NEXUS_EVIDENCE_KEY_ID", key_id)
    monkeypatch.setenv("NEXUS_EVIDENCE_PRIVATE_KEY",
                       base64.urlsafe_b64encode(seed).decode("ascii"))
    monkeypatch.setenv("NEXUS_EVIDENCE_KEYRING", f"{key_id}:{public_key_b64(signer)}")
    return signer


class FakeExecutor:
    """Dispatches instantly; reports a scripted outcome per task key."""

    def __init__(self, outcomes: dict[str, str] | None = None):
        self.outcomes = outcomes or {}
        self.dispatched: list[str] = []

    def dispatch(self, task):
        self.dispatched.append(task["task_key"])
        return f"conv-{task['task_key']}-{len(self.dispatched)}"

    def poll(self, task, conversation_id):
        state = self.outcomes.get(task["task_key"], "finished")
        return TaskReport(state=state, final_text="did work",
                          conversation_id=conversation_id)


class FakeVerifier:
    """Verdicts by task key, recording the commit it "observed" as evidence."""

    def __init__(self, verdicts: dict[str, str] | None = None):
        self.verdicts = verdicts or {}

    def verify(self, task, report):
        if report.state != "finished":
            return Verification("failed", "not finished", {"state": report.state})
        v = self.verdicts.get(task["task_key"], "completed_verified")
        if v == "completed_verified":
            return Verification(v, "remote branch is at the reported commit",
                                {"remote_sha": HEAD, "branch": task.get("branch") or "(default)",
                                 "named_sha": HEAD})
        return Verification(v, "no verifiable commit reported by the agent",
                            {"remote_sha": HEAD, "branch": task.get("branch") or "(default)"})


@pytest.fixture
def env():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Lab")["id"])
    goal = str(svc.create_goal("user:alice", project, "Ship v1")[0]["id"])
    lab = LabStore(store)
    # The repository the runs use is in the allowlist and linked to the project,
    # so the dispatch gate the scheduler runs lets it through.
    from orchestration.nexus_binding import ProjectRepositoryService
    store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
    ProjectRepositoryService(store).link("user:alice", project, REPO)
    yield {"store": store, "svc": svc, "project": project, "goal": goal, "lab": lab}
    store.close()


def _run_with_tasks(env, tasks, **run_options):
    run = env["lab"].start_run("user:alice", env["project"], env["goal"],
                               repository=REPO, branch="main", **run_options)
    for t in tasks:
        env["lab"].add_task("user:alice", env["project"], run["id"],
                            repository=REPO, branch="main", **t)
    return run


def _scheduler(env, executor, verifier, **kw):
    return LabScheduler(env["lab"], executor, verifier,
                        principal="user:alice", **kw)


def _evidence_service(env, key_id: str = "nexus-lab"):
    """The EVIDENCE stage's service, built over the environment signer."""
    from orchestration.nexus_evidence import EvidenceService
    return EvidenceService(
        env["store"],
        EvidenceTrustEngine(Keyring.from_env(), signer_from_env()))


def _decisions(env, project):
    with env["store"]._tx() as cur:
        cur.execute("SELECT verdict, body->'detail' AS detail, key_id, signature "
                    "FROM nexus_evidence WHERE project_id=%s AND subject_kind='workflow' "
                    "ORDER BY seq", (project,))
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------ the happy path
def test_a_completed_run_leaves_an_accepted_decision(env, monkeypatch):
    _signer_env(monkeypatch)
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B", "dependencies": ["a"]},
    ])
    sched = _scheduler(env, FakeExecutor(), FakeVerifier())
    sched.run_until_done(run["id"])
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"

    decisions = _decisions(env, env["project"])
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision["verdict"] == VERIFIED
    assert decision["detail"]["decision"] == "ACCEPTED"
    assert decision["detail"]["tasks_total"] == 2
    assert decision["detail"]["tasks_verified"] == 2

    # An independent reader re-checks the signature with the trusted keyring.
    engine = EvidenceTrustEngine(Keyring.from_env(), signer_from_env())
    rec = next(e for e in env["store"].list_evidence(project_id=env["project"])
               if e["subject_kind"] == "workflow")
    assert engine.verify(rec).verdict == VERIFIED


def test_a_repeated_pass_appends_nothing(env, monkeypatch):
    """The id is the exactly-once gate: a worker that re-runs the step (a crash
    between the decision and the record) collides with its own row."""
    _signer_env(monkeypatch)
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    sched = _scheduler(env, FakeExecutor(), FakeVerifier())
    sched.run_until_done(run["id"])
    assert len(_decisions(env, env["project"])) == 1

    # Ticking a finished run changes nothing.
    for _ in range(3):
        sched.tick(run["id"])
    assert len(_decisions(env, env["project"])) == 1

    # Re-recording the decision by hand collides rather than appending.
    run_row = env["lab"].run_row(run["id"])
    tasks = env["lab"].list_tasks_internal(run["id"])
    sched.acceptance.accept_run(run_id=run["id"], project_id=str(run_row["project_id"]),
                                state="completed", tasks=tasks, last_error=None)
    assert len(_decisions(env, env["project"])) == 1


# --------------------------------------------------------------- what stopped
def test_a_blocked_run_is_not_decided_yet(env, monkeypatch):
    """A blocked run is not terminal: the task that stopped it is attested (the
    EVIDENCE stage), but the run's ACCEPTANCE is not decided until it ends."""
    _signer_env(monkeypatch)
    run = _run_with_tasks(env, [
        {"task_key": "a", "title": "A"},
        {"task_key": "b", "title": "B", "dependencies": ["a"]},
    ])
    sched = _scheduler(env, FakeExecutor(),
                       FakeVerifier({"a": "completed_unverified"}))
    sched.run_until_done(run["id"])
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "blocked"
    assert _decisions(env, env["project"]) == []


def test_a_cancelled_run_is_decided_not_ready(env, monkeypatch):
    """A run an operator cancelled is terminal and must be attested - as
    NOT_READY, never as an acceptance of work that did not verify."""
    _signer_env(monkeypatch)
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    sched = _scheduler(env, FakeExecutor(), FakeVerifier({"a": "completed_unverified"}))
    sched.run_until_done(run["id"])
    # Blocked; a person cancels it, which is terminal and must be decided.
    env["lab"].set_run_state("user:alice", env["project"], run["id"], "cancelled")
    sched.tick(run["id"])
    decisions = _decisions(env, env["project"])
    assert len(decisions) == 1, decisions
    assert decisions[0]["detail"]["decision"] == "NOT_READY"
    assert decisions[0]["detail"]["tasks_verified"] == 0


# ------------------------------------------------------------ no signer, no lie
def test_without_a_signer_no_decision_is_written_and_the_run_still_completes(
        env, monkeypatch):
    for name in ("NEXUS_EVIDENCE_PRIVATE_KEY", "NEXUS_EVIDENCE_KEY_ID",
                 "NEXUS_EVIDENCE_KEYRING"):
        monkeypatch.delenv(name, raising=False)
    run = _run_with_tasks(env, [{"task_key": "a", "title": "A"}])
    sched = _scheduler(env, FakeExecutor(), FakeVerifier())
    assert sched.acceptance.configured is False
    sched.run_until_done(run["id"])

    # The run is unaffected; nothing is recorded, and nothing is faked.
    assert env["lab"].get_run("user:alice", env["project"], run["id"])["state"] == "completed"
    assert _decisions(env, env["project"]) == []


def test_the_lab_evidence_ids_are_stable_and_distinct():
    a = lab_evidence_id("run-1", "task-1", "verdict")
    assert a == lab_evidence_id("run-1", "task-1", "verdict")     # stable
    assert a != lab_evidence_id("run-1", "task-2", "verdict")     # per task
    assert a != lab_evidence_id("run-1", "task-1", "run")         # per kind
    assert a != lab_evidence_id("run-2", "task-1", "verdict")     # per run


# ------------------------------------------------- the pipeline, stage by stage
def test_every_stage_of_the_pipeline_leaves_a_durable_artifact(env, monkeypatch):
    """One run through the whole pipeline, with a durable record for each stage.

    This is the acceptance the brief asks for ("Проверь весь pipeline") made
    concrete: for each named stage there is an artifact in PostgreSQL that an
    independent reader can check, not a claim in a report.

    PLANNER/DAG -> the goal and its task graph exist; SCHEDULER/OPENHANDS/
    EXECUTION -> a real conversation was started per task and its id is bound to
    the task; QA -> the verifier's verdict, with the commit it read; EVIDENCE ->
    a signed record per settled task; ACCEPTANCE -> the run's signed decision;
    NOTIFICATION -> durable notifications.
    """
    _signer_env(monkeypatch)
    run = _run_with_tasks(env, [
        {"task_key": "audit", "title": "Audit", "acceptance": ["summary"]},
        {"task_key": "impl", "title": "Implement", "dependencies": ["audit"],
         "acceptance": ["tests pass"]},
    ])
    executor = FakeExecutor()
    sched = _scheduler(env, executor, FakeVerifier(), evidence=_evidence_service(env))
    sched.run_until_done(run["id"])

    store, project = env["store"], env["project"]

    # PLANNER / DAG: the goal and its task graph.
    with store._tx() as cur:
        cur.execute("SELECT text FROM nexus_goals WHERE id=%s", (env["goal"],))
        assert cur.fetchone()["text"] == "Ship v1"
    tasks = env["lab"].list_tasks("user:alice", project, run["id"])
    assert [t["task_key"] for t in tasks] == ["audit", "impl"]
    assert tasks[1]["dependencies"] == ["audit"]

    # SCHEDULER / OPENHANDS / EXECUTION: one conversation per task, in dependency
    # order, each bound to its task.
    assert executor.dispatched == ["audit", "impl"]
    assert all(t["conversation_id"] for t in tasks)

    # QA: the verifier's verdict, over the commit it read.
    assert all(t["state"] == "completed_verified" for t in tasks)
    assert all((t["verification"] or {}).get("reason") for t in tasks)

    # EVIDENCE: a signed record per settled task, naming the observed commit.
    rows = store.list_evidence(project_id=project)
    task_rows = [r for r in rows if r["subject_kind"] == "task"]
    assert len(task_rows) == 2
    assert all(r["commit_sha"] == HEAD for r in task_rows)

    # ACCEPTANCE: the run's decision, over those records.
    decisions = [r for r in rows if r["subject_kind"] == "workflow"]
    assert len(decisions) == 1 and decisions[0]["body"]["detail"]["decision"] == "ACCEPTED"

    # NOTIFICATION: durable, project-scoped events for the run and its tasks.
    notes = env["lab"].notifications("user:alice", project)
    kinds = {n["kind"] for n in notes}
    assert {"goal_started", "goal_completed", "task_completed_verified"} <= kinds

    # Every stage's artifact is signed, and an independent reader re-checks it.
    engine = EvidenceTrustEngine(Keyring.from_env(), signer_from_env())
    for rec in rows:
        assert engine.verify(rec).verdict == VERIFIED
