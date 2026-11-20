"""The Self-QA gate as a *per-project default* (roadmap P1 #1).

Two things are proved here, both against real machinery:

* **The gate can actually check a real project.** A task's ``repository`` is
  ``Pavel1778/python-dispetcher`` - an ``owner/name`` string - not a directory.
  The gate used to run ``git clone owner/name``, which fails with "repository
  does not exist", so it reported ``blocked`` for every real project while every
  test passed on a local path. ``clone_url`` fixes that, and the first class of
  cases pins it, including that a Git credential never reaches argv or the URL.

* **The mode is per project.** ``auto`` (the default) runs the checks only when
  the repository declares ``.nexus-qa.json``; ``on`` always; ``off`` never. A
  declaration that cannot be read is "nothing established", never a silent run
  of the defaults. The scheduler turns a ``failed`` gate into a rework and only
  ever *weakens* a verdict.

The HTTP cases drive the real server over real PostgreSQL; the scheduler cases
drive the real ``LabScheduler`` over real PostgreSQL with a real ``SelfQa`` on a
real git repository, so the downgrade comes from a real exit code.
"""
from __future__ import annotations

import json
import os
import pathlib
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
from orchestration.nexus_selfqa import (  # noqa: E402
    GATE_MODES,
    ProjectQualityGate,
    SelfQa,
    clone_url,
    declares_checks,
    normalise_mode,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


# --------------------------------------------------------------- the clone fix
def test_an_owner_name_becomes_a_real_clone_url():
    assert clone_url("Pavel1778/python-dispetcher") == \
        "https://github.com/Pavel1778/python-dispetcher.git"
    # A URL, a path and an empty string are left alone.
    assert clone_url("https://github.com/a/b.git") == "https://github.com/a/b.git"
    assert clone_url("/tmp/some/repo") == "/tmp/some/repo"
    assert clone_url("") == ""
    # A name with more than one slash is not ``owner/name`` and is not rewritten.
    assert clone_url("git@github.com:a/b.git") == "git@github.com:a/b.git"


def test_an_auth_header_keeps_the_token_out_of_argv_and_the_url():
    """The token may reach git, but only through the environment.

    A token in the URL is written into ``.git/config``; a token passed with
    ``-c`` is visible in ``ps``. ``GIT_CONFIG_*`` is the one supported way to
    give git a value without either.
    """
    from orchestration.nexus_selfqa import _auth_env

    token = "ghp_example-secret-token-value"
    env = _auth_env(token, "https://github.com/o/n.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The clone URL carries no token, and no value outside the header does either.
    for key, value in env.items():
        if key != "GIT_CONFIG_VALUE_0":
            assert token not in value, (key, value)
    # A local path or an ssh remote gets no header: a token there is meaningless.
    assert _auth_env(token, "/tmp/repo") == {}
    assert _auth_env(token, "git@github.com:o/n.git") == {}
    # No token, no header.
    assert _auth_env("", "https://github.com/o/n.git") == {}


def test_the_engine_never_puts_its_token_in_the_report():
    """A report is data that is stored and shown; it may never carry a secret."""
    token = "ghp_never-in-a-report-1234567890"
    qa = SelfQa(token=token)
    report = qa.run(repository="", commit="")
    assert token not in json.dumps(report.as_dict())


# ------------------------------------------------------------- the mode policy
def test_the_modes_are_the_three_the_column_allows():
    assert set(GATE_MODES) == {"auto", "on", "off"}


@pytest.mark.parametrize("value,expected", [
    ("auto", "auto"), ("on", "on"), ("off", "off"),
    ("  ON ", "on"), ("AUTO", "auto"),
    (True, "on"), (False, "off"),
    (None, "auto"), ("nonsense", "auto"), (7, "auto"),
])
def test_normalise_mode_is_total(value, expected):
    assert normalise_mode(value) == expected


class _Declared:
    """A ``declares`` that answers what it is told, recording its calls."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, repository, *, branch=None, project_id=""):
        self.calls.append((repository, branch, project_id))
        return self.answer


class _Engine:
    """A ``SelfQa`` stand-in that records whether it was asked to run."""

    workdir = None
    check_timeout = 900.0
    total_timeout = 2400.0
    token = ""

    def __init__(self, verdict="passed"):
        self.verdict = verdict
        self.calls = []

    def run(self, *, repository, commit="", checks=None, project_id="", branch=None):
        self.calls.append({"repository": repository, "commit": commit,
                           "project_id": project_id, "branch": branch})
        from orchestration.nexus_selfqa import QaReport
        return QaReport(verdict=self.verdict, reason="ran", repository=repository,
                        commit=commit)


def _gate(mode, *, declared=False, engine=None):
    engine = engine or _Engine()
    return ProjectQualityGate(
        engine,
        mode_for=lambda project_id: mode,
        declares=_Declared(declared),
    ), engine


def test_off_never_runs_the_engine():
    gate, engine = _gate("off", declared=True)
    report = gate.run(repository="o/n", commit="abc", project_id="p1")
    assert report.verdict == "skipped"
    assert "off" in report.reason
    assert engine.calls == []


def test_auto_runs_only_when_the_repository_declares_its_checks():
    gate, engine = _gate("auto", declared=True)
    report = gate.run(repository="o/n", commit="abc", project_id="p1")
    assert report.verdict == "passed"
    assert engine.calls and engine.calls[0]["repository"] == "o/n"


def test_auto_skips_when_nothing_is_declared():
    gate, engine = _gate("auto", declared=False)
    report = gate.run(repository="o/n", commit="abc", project_id="p1")
    assert report.verdict == "skipped"
    assert "declares no checks" in report.reason
    assert engine.calls == []


def test_auto_does_not_run_when_the_declaration_cannot_be_read():
    """``None`` is 'not established', not 'nothing declared, so use the defaults'."""
    gate, engine = _gate("auto")
    gate._declares = _Declared(None)
    report = gate.run(repository="o/n", commit="abc", project_id="p1")
    assert report.verdict == "skipped"
    assert "could not be read" in report.reason
    assert engine.calls == []


def test_on_always_runs_the_engine():
    gate, engine = _gate("on", declared=False)
    report = gate.run(repository="o/n", commit="abc", project_id="p1")
    assert report.verdict == "passed"
    assert len(engine.calls) == 1


def test_the_wrapper_passes_the_project_and_branch_through():
    gate, engine = _gate("on")
    gate.run(repository="o/n", commit="abc", project_id="p-9", branch="feature")
    assert engine.calls[0]["project_id"] == "p-9"
    assert engine.calls[0]["branch"] == "feature"


def test_an_unreadable_mode_reads_as_auto():
    def boom(project_id):
        raise RuntimeError("database is down")

    gate = ProjectQualityGate(_Engine(), mode_for=boom, declares=_Declared(True))
    assert gate.mode("p1") == "auto"


def test_declares_checks_reads_the_file_through_the_github_client():
    class _Client:
        def __init__(self, found):
            self.found = found

        def read_text_file(self, full_name, path, *, branch=None):
            assert path == ".nexus-qa.json"
            return {"content": "{}"} if self.found else None

    assert declares_checks("o/n", client=_Client(True)) is True
    assert declares_checks("o/n", client=_Client(False)) is False
    # No client, no repository: nothing established.
    assert declares_checks("o/n", client=None) is None
    assert declares_checks("", client=_Client(True)) is None

    class _Broken:
        def read_text_file(self, *a, **k):
            raise RuntimeError("github is down")

    assert declares_checks("o/n", client=_Broken()) is None


# ------------------------------------------------- a real engine, real exit code
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, *, passes: bool, declare: bool = True) -> str:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "qa@nexus.test")
    _git(repo, "config", "user.name", "QA")
    (repo / "check.py").write_text(
        "import sys\nsys.exit(0 if %r else 1)\n" % passes)
    if declare:
        (repo / ".nexus-qa.json").write_text(
            '{"checks": [{"name": "unit", "run": ["python", "check.py"], '
            '"required": true}]}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return str(repo)


def test_the_engine_checks_out_the_commit_it_was_given(tmp_path):
    """The gate must check *that* commit, not the branch head a shallow clone
    happened to stop at. A second commit that breaks the check proves it."""
    repo = _repo(tmp_path, passes=True)
    good = _git(repo, "rev-parse", "HEAD")
    pathlib.Path(repo, "check.py").write_text("import sys\nsys.exit(1)\n")
    _git(repo, "commit", "-q", "-am", "break it")
    head = _git(repo, "rev-parse", "HEAD")
    assert good != head

    qa = SelfQa(workdir=tmp_path / "work")
    # At the good commit the declared check passes...
    assert qa.run(repository=repo, commit=good).verdict == "passed"
    # ...at the bad one it fails. Same repository, same check, only the commit
    # differs, so a gate that ignored the commit would answer the same twice.
    assert qa.run(repository=repo, commit=head).verdict == "failed"


def test_a_commit_that_cannot_be_reached_is_blocked_not_passed(tmp_path):
    repo = _repo(tmp_path, passes=True)
    qa = SelfQa(workdir=tmp_path / "work")
    report = qa.run(repository=repo, commit="0" * 40)
    assert report.verdict == "blocked"
    assert "could not make an isolated checkout" in report.reason


def test_the_same_engine_checks_two_commits_in_a_row(tmp_path):
    """One ``SelfQa`` serves the whole loop, so it is asked many times.

    A fixed ``workdir/checkout`` made the *second* run fail to clone ("already
    exists"), and a ``blocked`` report leaves a task verdict alone - so a
    failing project would have been accepted on every run after the first. The
    engine must give each run its own scratch directory.
    """
    repo = _repo(tmp_path, passes=True)
    good = _git(repo, "rev-parse", "HEAD")
    pathlib.Path(repo, "check.py").write_text("import sys\nsys.exit(1)\n")
    _git(repo, "commit", "-q", "-am", "break it")
    bad = _git(repo, "rev-parse", "HEAD")

    qa = SelfQa(workdir=tmp_path / "work")
    first = qa.run(repository=repo, commit=good)
    second = qa.run(repository=repo, commit=bad)
    third = qa.run(repository=repo, commit=good)
    # Each run really ran its own checks, not "blocked: the directory exists".
    assert first.verdict == "passed"
    assert second.verdict == "failed"
    assert third.verdict == "passed"
    for report in (first, second, third):
        assert [c.name for c in report.checks] == ["unit"], report.reason


# ---------------------------------------------------- the scheduler integration
def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class _Verifier:
    def __init__(self, commit):
        self.commit = commit

    def verify(self, task, report):
        return Verification("completed_verified", "fake git verdict",
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
    yield {"store": store, "svc": svc, "lab": LabStore(store),
           "project": project, "goal": goal}
    store.close()


def _one_task(lab, repository):
    lab["store"].add_repository(repository, default_branch="main",
                                allowed_branches=["main"])
    from orchestration.nexus_binding import ProjectRepositoryService
    ProjectRepositoryService(lab["store"]).link("user:alice", lab["project"], repository)
    run = lab["lab"].start_run("user:alice", lab["project"], lab["goal"],
                               policy="durable")
    lab["lab"].add_task("user:alice", lab["project"], run["id"], task_key="t1",
                        title="t1", repository=repository, branch="main")
    return run


def _settled(lab, run):
    return lab["lab"].list_tasks("user:alice", lab["project"], run["id"])[0]["state"]


def test_off_mode_leaves_the_verdict_alone_even_when_the_checks_would_fail(lab, tmp_path):
    repo = _repo(tmp_path, passes=False)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task(lab, repo)
    lab["svc"].set_quality_gate("user:alice", lab["project"], "off")
    gate = ProjectQualityGate(SelfQa(workdir=tmp_path / "work"),
                              mode_for=lambda p: "off")
    LabScheduler(lab["lab"], _Executor(), _Verifier(head),
                 quality_gate=gate).run_until_done(str(run["id"]))
    assert _settled(lab, run) == "completed_verified"


def test_on_mode_downgrades_a_failing_required_check(lab, tmp_path):
    repo = _repo(tmp_path, passes=False)
    head = _git(repo, "rev-parse", "HEAD")
    run = _one_task(lab, repo)
    gate = ProjectQualityGate(
        SelfQa(workdir=tmp_path / "work"),
        mode_for=lambda p: "on", declares=_Declared(False))
    LabScheduler(lab["lab"], _Executor(), _Verifier(head),
                 quality_gate=gate).run_until_done(str(run["id"]))
    assert _settled(lab, run) != "completed_verified"


def test_auto_mode_runs_only_when_the_project_declares_it(lab, tmp_path):
    """A declared repository is checked; the same failing check under ``off`` is not."""
    repo = _repo(tmp_path, passes=False, declare=True)
    head = _git(repo, "rev-parse", "HEAD")

    run_auto = _one_task(lab, repo)
    gate_auto = ProjectQualityGate(
        SelfQa(workdir=tmp_path / "w1"),
        mode_for=lambda p: "auto", declares=_Declared(True))
    LabScheduler(lab["lab"], _Executor(), _Verifier(head),
                 quality_gate=gate_auto).run_until_done(str(run_auto["id"]))
    assert _settled(lab, run_auto) != "completed_verified"

    # A second goal, so the second run is a fresh one (one active run per goal).
    goal2 = str(lab["svc"].create_goal("user:alice", lab["project"], "again")[0]["id"])
    run_off = lab["lab"].start_run("user:alice", lab["project"], goal2,
                                   policy="durable")
    lab["lab"].add_task("user:alice", lab["project"], run_off["id"], task_key="t2",
                        title="t2", repository=repo, branch="main")
    gate_off = ProjectQualityGate(
        SelfQa(workdir=tmp_path / "w2"), mode_for=lambda p: "off")
    LabScheduler(lab["lab"], _Executor(), _Verifier(head),
                 quality_gate=gate_off).run_until_done(str(run_off["id"]))
    assert _settled(lab, run_off) == "completed_verified"


def test_the_mode_is_read_from_the_project_row(lab):
    """The store is the source of truth the gate consults, not a default."""
    from orchestration.lab_worker import quality_gate_mode

    assert quality_gate_mode(lab["store"], lab["project"]) == "auto"
    lab["svc"].set_quality_gate("user:alice", lab["project"], "off")
    assert quality_gate_mode(lab["store"], lab["project"]) == "off"
    lab["svc"].set_quality_gate("user:alice", lab["project"], "on")
    assert quality_gate_mode(lab["store"], lab["project"]) == "on"
    # A project with no row, or a bad id, reads as the default.
    assert quality_gate_mode(lab["store"], "") == "auto"
    assert quality_gate_mode(
        lab["store"], "00000000-0000-4000-8000-000000000000") == "auto"


def test_the_project_service_validates_and_carries_the_mode(lab):
    p = lab["svc"].get("user:alice", lab["project"])
    assert p["quality_gate"] == "auto"
    for mode in ("on", "off", "auto"):
        updated = lab["svc"].set_quality_gate("user:alice", lab["project"], mode)
        assert updated["quality_gate"] == mode
        assert lab["svc"].get("user:alice", lab["project"])["quality_gate"] == mode
    with pytest.raises(ValueError):
        lab["svc"].set_quality_gate("user:alice", lab["project"], "sometimes")


# ------------------------------------------------------------- the HTTP surface
@pytest.fixture
def served(monkeypatch):
    """The real web server on real PostgreSQL and the fake GitHub, for the route."""
    from tests.test_studio_api import studio_stack

    with studio_stack(monkeypatch) as stack:
        yield stack


def test_the_route_reads_and_writes_the_mode(served):
    project = served.project("Gate")
    status, body = served.api.get(f"/api/v1/projects/{project}")
    assert status == 200
    assert body["project"]["quality_gate"] == "auto"

    status, body = served.api.patch(f"/api/v1/projects/{project}",
                                    {"quality_gate": "on"})
    assert status == 200, body
    assert body["project"]["quality_gate"] == "on"
    assert served.api.get(f"/api/v1/projects/{project}")[1]["project"]["quality_gate"] == "on"

    # A wrong value is a 400 naming the allowed set, not a 500 from the CHECK.
    status, body = served.api.patch(f"/api/v1/projects/{project}",
                                    {"quality_gate": "sometimes"})
    assert status == 400
    assert "auto" in body["error"] and "off" in body["error"]


def test_the_route_requires_authentication(served):
    project = served.project("Gate")
    from tests.test_studio_api import Api

    anonymous = Api(served.api.base, token=None)
    status, _ = anonymous.patch(f"/api/v1/projects/{project}", {"quality_gate": "off"})
    assert status == 401


def test_the_cli_reads_and_writes_the_mode(monkeypatch, capsys):
    """``nexus project quality-gate <id> [auto|on|off]`` over the real handler."""
    from orchestration import cli

    calls = []

    class _Stub:
        def get(self, path):
            calls.append(("GET", path))
            return {"project": {"quality_gate": "auto"}}

        def patch(self, path, body):
            calls.append(("PATCH", path, body))
            return {"project": {"quality_gate": body["quality_gate"]}}

    monkeypatch.setattr(cli, "_client", lambda args: _Stub())

    class _Args:
        project_id = "p1"
        url = None
        token = None
        json = False

    show = _Args()
    show.mode = None
    assert cli.cmd_project_quality_gate(show) == cli.EXIT["ok"]
    assert "auto" in capsys.readouterr().out

    change = _Args()
    change.mode = "on"
    assert cli.cmd_project_quality_gate(change) == cli.EXIT["ok"]
    assert calls == [("GET", "/api/v1/projects/p1"),
                     ("PATCH", "/api/v1/projects/p1", {"quality_gate": "on"})]


def test_a_change_only_an_owner_may_make_is_refused_for_a_member(served):
    """The mode is project admin, like the name: a plain member cannot set it.

    A non-owner is answered ``NotFound``, not a 403 - project isolation never
    discloses that the project exists (the same rule every other project
    resource follows).
    """
    import psycopg

    from orchestration.cloud_store import NotFound
    from orchestration.nexus_projects import ProjectService

    project = served.project("Gate")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                        "VALUES (%s, 'user:bob', 'member')", (project,))
    service = ProjectService(served.app.store)
    with pytest.raises(NotFound):
        service.set_quality_gate("user:bob", project, "off")
    # The member's refusal left the value untouched. The bearer is the owner
    # (``AUTH_USERNAME=owner`` in this stack) and still may change it.
    owner = f"user:{served.app.auth.username}"
    assert service.get(owner, project)["quality_gate"] == "auto"
    assert service.set_quality_gate(owner, project, "off")["quality_gate"] == "off"
