"""The Self-QA quality gate on the autonomous loop.

The Git verifier proves a commit landed. The quality gate is the one stage that
proves the project still works: it runs the project's own checks at the exact
commit that was accepted and can only make a verdict *weaker*. These cases drive
the real ``LabScheduler`` over real PostgreSQL with a small verifier/executor
fake, and a real ``SelfQa`` against a real git repository on disk, so the
downgrade is produced by real exit codes, not a mock.

The four claims under test:

* a passing gate leaves a verified task verified;
* a failing required check downgrades it to ``completed_unverified`` (rework);
* a skipped/blocked gate (no tooling) never turns a pass into a failure;
* with no gate configured the loop is exactly as before.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    LabScheduler,
    TaskReport,
    Verification,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_selfqa import SelfQa  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo_with_tests(tmp_path, *, tests_pass: bool):
    """A real git repository whose declared check (a python script) passes or fails."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "qa@nexus.test")
    _git(repo, "config", "user.name", "QA")
    (repo / "check.py").write_text(
        "import sys\nsys.exit(0 if %r else 1)\n" % tests_pass)
    (repo / ".nexus-qa.json").write_text(
        '{"checks": [{"name": "unit", "run": ["python", "check.py"], '
        '"required": true}]}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return str(repo)


class _Verifier:
    """A verifier that always returns the same verdict, with a commit in evidence.

    ``commit`` may be a real repository HEAD so the quality gate has a commit it
    can actually check out; the default is a fake sha for the cases that never
    reach the gate.
    """

    def __init__(self, verdict="completed_verified", commit="deadbeefcafe"):
        self.verdict = verdict
        self.commit = commit

    def verify(self, task, report):
        return Verification(self.verdict, "fake git verdict",
                            {"remote_sha": self.commit})


class _Executor:
    def dispatch(self, task):
        return f"conv-{task['task_key']}"

    def poll(self, task, conversation_id):
        return TaskReport(state="finished", final_text="did work",
                          conversation_id=conversation_id)


@pytest.fixture
def lab():
    _reset()
    store = PostgresStore(DSN)
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "Gate")["id"])
    goal = str(svc.create_goal("user:alice", project, "do it")[0]["id"])
    yield {"store": store, "lab": LabStore(store), "project": project, "goal": goal}
    store.close()


def _one_task_run(lab, *, repository, branch="main"):
    if repository:
        # A project-scoped task is dispatch-gated on the allowlist and the
        # project↔repo link, so a task with a repository must be authorized
        # before the loop will start it.
        lab["store"].add_repository(repository, default_branch="main",
                                    allowed_branches=["main"])
        from orchestration.nexus_binding import ProjectRepositoryService
        ProjectRepositoryService(lab["store"]).link(
            "user:alice", lab["project"], repository)
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                               policy="durable")
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="t1",
                        title="t1", repository=repository, branch=branch)
    return run


def _settled(lab, run):
    tasks = lab["lab"].list_tasks("user:alice", lab["project"], run["id"])
    return tasks[0]["state"]


def test_a_passing_gate_keeps_the_task_verified(lab, tmp_path):
    repo = _repo_with_tests(tmp_path, tests_pass=True)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task_run(lab, repository=repo)
    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(commit=head),
                         quality_gate=SelfQa())
    sched.run_until_done(str(run["id"]))
    assert _settled(lab, run) == "completed_verified"


def test_a_failing_required_check_downgrades_the_task(lab, tmp_path):
    repo = _repo_with_tests(tmp_path, tests_pass=False)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task_run(lab, repository=repo)
    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(commit=head),
                         quality_gate=SelfQa())
    sched.run_until_done(str(run["id"]))
    # Not verified: the branch moved, but the project's own check failed. The
    # task is reworked, so it never reaches completed_verified.
    assert _settled(lab, run) != "completed_verified"


def test_the_gate_records_its_verdict_as_evidence(lab, tmp_path):
    repo = _repo_with_tests(tmp_path, tests_pass=True)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task_run(lab, repository=repo)
    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(commit=head),
                         quality_gate=SelfQa())
    sched.run_until_done(str(run["id"]))
    task = lab["lab"].list_tasks("user:alice", lab["project"], run["id"])[0]
    entries = task.get("evidence") or []
    self_qa = [e["self_qa"] for e in entries if isinstance(e, dict) and "self_qa" in e]
    assert self_qa, entries
    assert self_qa[-1]["verdict"] in ("passed", "skipped", "blocked")


def test_no_gate_means_no_self_qa_evidence(lab, tmp_path):
    repo = _repo_with_tests(tmp_path, tests_pass=False)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task_run(lab, repository=repo)
    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(commit=head))  # no gate
    sched.run_until_done(str(run["id"]))
    assert _settled(lab, run) == "completed_verified"
    task = lab["lab"].list_tasks("user:alice", lab["project"], run["id"])[0]
    entries = task.get("evidence") or []
    assert not [e for e in entries if isinstance(e, dict) and "self_qa" in e]


def test_a_gate_that_raises_does_not_change_the_verdict(lab, tmp_path):
    repo = _repo_with_tests(tmp_path, tests_pass=True)
    run = _one_task_run(lab, repository=repo)

    class _Broken:
        def run(self, **kwargs):
            raise RuntimeError("checker exploded")

    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(), quality_gate=_Broken())
    sched.run_until_done(str(run["id"]))
    assert _settled(lab, run) == "completed_verified"


def test_the_gate_never_runs_for_a_task_without_a_repository(lab):
    run = _one_task_run(lab, repository="")

    class _Counting:
        calls = 0

        def run(self, **kwargs):
            _Counting.calls += 1
            raise AssertionError("must not run without a repository")

    sched = LabScheduler(lab["lab"], _Executor(), _Verifier(commit=""),
                         quality_gate=_Counting())
    sched.run_until_done(str(run["id"]))
    assert _Counting.calls == 0


def test_the_gate_is_per_project_and_the_env_switch_is_an_override(monkeypatch):
    """Without a store, the env switch alone still decides (single-project use);
    with a store, the per-project mode is the default and the env switch only
    overrides it."""
    from orchestration.lab_worker import build_quality_gate
    from orchestration.nexus_selfqa import ProjectQualityGate

    monkeypatch.delenv("NEXUS_SELFQA_GATE", raising=False)
    # No store: unchanged single-project behaviour.
    assert build_quality_gate() is None
    for on in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("NEXUS_SELFQA_GATE", on)
        assert isinstance(build_quality_gate(), SelfQa)
    monkeypatch.setenv("NEXUS_SELFQA_GATE", "0")
    assert build_quality_gate() is None

    # With a store and no env switch the gate is present and per project: it
    # reads each project's own mode, so it is neither always on nor always off.
    monkeypatch.delenv("NEXUS_SELFQA_GATE", raising=False)
    gate = build_quality_gate(store=object())
    assert isinstance(gate, ProjectQualityGate)
    # An explicit "off" turns it off everywhere, an explicit "on" forces it on
    # for every project - both are the operator's deployment-wide override.
    monkeypatch.setenv("NEXUS_SELFQA_GATE", "off")
    assert build_quality_gate(store=object()) is None
    monkeypatch.setenv("NEXUS_SELFQA_GATE", "1")
    assert isinstance(build_quality_gate(store=object()), ProjectQualityGate)


def test_the_check_timeouts_come_from_the_environment(monkeypatch):
    """The two limits are real settings; a junk value is the default, never 0."""
    from orchestration.lab_worker import build_quality_gate
    from orchestration.nexus_selfqa import DEFAULT_CHECK_TIMEOUT, DEFAULT_TOTAL_TIMEOUT

    monkeypatch.setenv("NEXUS_SELFQA_GATE", "1")
    gate = build_quality_gate()          # the no-store single-project engine
    assert gate.check_timeout == DEFAULT_CHECK_TIMEOUT
    assert gate.total_timeout == DEFAULT_TOTAL_TIMEOUT

    monkeypatch.setenv("NEXUS_SELFQA_CHECK_TIMEOUT", "12.5")
    monkeypatch.setenv("NEXUS_SELFQA_TOTAL_TIMEOUT", "99")
    gate = build_quality_gate()
    assert gate.check_timeout == 12.5
    assert gate.total_timeout == 99.0

    # A junk or non-positive value is the default: a zero timeout would fail
    # every check and blame the project.
    monkeypatch.setenv("NEXUS_SELFQA_CHECK_TIMEOUT", "not-a-number")
    monkeypatch.setenv("NEXUS_SELFQA_TOTAL_TIMEOUT", "0")
    gate = build_quality_gate()
    assert gate.check_timeout == DEFAULT_CHECK_TIMEOUT
    assert gate.total_timeout == DEFAULT_TOTAL_TIMEOUT
