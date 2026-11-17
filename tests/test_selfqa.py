"""Self-QA: a project's own checks run in an isolated checkout, honestly.

Every case here uses a **real git repository on disk** and real commands, so the
verdict is derived from real exit codes. The point of the module is that a
failure cannot be reported as a pass and that "could not run" is never "passed".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_selfqa import (  # noqa: E402
    DEFAULT_CHECKS,
    QaCheck,
    SelfQa,
    checks_for,
    run_check,
)

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("git"), reason="git is required")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(tmp_path, *, tests_pass=True, extra_file=None, qa_json=None):
    """A real git repository with one commit whose test either passes or fails."""
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "qa@nexus.test")
    _git(repo, "config", "user.name", "NEXUS QA")
    (repo / "test_sample.py").write_text(
        "def test_it_works():\n"
        + ("    assert True\n" if tests_pass else "    assert False\n"),
        encoding="utf-8")
    if extra_file:
        (repo / extra_file[0]).write_text(extra_file[1], encoding="utf-8")
    if qa_json is not None:
        (repo / ".nexus-qa.json").write_text(json.dumps(qa_json), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return str(repo), _git(repo, "rev-parse", "HEAD")


def test_a_passing_project_is_reported_passed(tmp_path):
    repo, head = _make_repo(tmp_path, tests_pass=True)
    report = SelfQa().run(
        repository=repo, commit=head,
        checks=[QaCheck("tests", (sys.executable, "-m", "pytest", "-q",
                                  "-p", "no:cacheprovider"))])
    assert report.verdict == "passed", report.as_dict()
    assert report.ok and report.checks[0].status == "passed"
    assert report.checks[0].exit_code == 0


def test_a_failing_required_check_fails_the_run(tmp_path):
    repo, head = _make_repo(tmp_path, tests_pass=False)
    report = SelfQa().run(
        repository=repo, commit=head,
        checks=[QaCheck("tests", (sys.executable, "-m", "pytest", "-q",
                                  "-p", "no:cacheprovider"), required=True)])
    assert report.verdict == "failed", report.as_dict()
    assert not report.ok
    assert report.checks[0].status == "failed"
    assert report.checks[0].exit_code != 0


def test_a_failing_optional_check_does_not_fail_the_run(tmp_path):
    repo, head = _make_repo(tmp_path, tests_pass=True)
    report = SelfQa().run(
        repository=repo, commit=head,
        checks=[
            QaCheck("tests", (sys.executable, "-c", "raise SystemExit(0)"), required=True),
            QaCheck("lint", (sys.executable, "-c", "raise SystemExit(3)"), required=False),
        ])
    assert report.verdict == "passed"
    assert "optional" in report.reason
    assert [c.status for c in report.checks] == ["passed", "failed"]


def test_a_missing_tool_is_skipped_not_passed(tmp_path):
    repo, head = _make_repo(tmp_path)
    report = SelfQa().run(
        repository=repo, commit=head,
        checks=[QaCheck("tests", ("definitely-not-a-real-tool-xyz", "run"))])
    assert report.verdict == "skipped", report.as_dict()
    assert report.checks[0].status == "skipped"
    assert not report.ok, "a check that could not run is never a pass"


def test_no_checks_at_all_is_skipped_not_passed(tmp_path):
    repo, head = _make_repo(tmp_path)
    report = SelfQa().run(repository=repo, commit=head, checks=[])
    assert report.verdict == "skipped"
    assert not report.ok


def test_a_timeout_is_a_failure_of_a_required_check(tmp_path):
    repo, head = _make_repo(tmp_path)
    report = SelfQa(workdir=tmp_path / "wt", check_timeout=1.0).run(
        repository=repo, commit=head,
        checks=[QaCheck("slow", (sys.executable, "-c", "import time; time.sleep(30)"),
                        required=True)])
    assert report.verdict == "failed", report.as_dict()
    assert report.checks[0].status == "timeout"


def test_an_unclonable_repository_is_blocked_not_failed(tmp_path):
    report = SelfQa(workdir=tmp_path / "wt").run(
        repository=str(tmp_path / "does-not-exist"), commit="deadbeef")
    assert report.verdict == "blocked", report.as_dict()
    assert not report.ok


def test_the_repository_declares_its_own_checks(tmp_path):
    qa = {"checks": [
        {"name": "custom", "run": [sys.executable, "-c", "raise SystemExit(0)"],
         "required": True, "note": "the project's own gate"}]}
    repo, head = _make_repo(tmp_path, qa_json=qa)
    report = SelfQa().run(repository=repo, commit=head)
    assert report.verdict == "passed"
    assert [c.name for c in report.checks] == ["custom"]


def test_checks_for_falls_back_to_defaults_without_a_declaration(tmp_path):
    repo, _ = _make_repo(tmp_path)
    names = [c.name for c in checks_for(__import__("pathlib").Path(repo))]
    assert names == [c.name for c in DEFAULT_CHECKS]


def test_the_checkout_is_isolated_from_the_source(tmp_path):
    """A check runs in a scratch clone, so a file it writes does not touch the
    source repository."""
    repo, head = _make_repo(tmp_path)
    SelfQa(workdir=tmp_path / "wt").run(
        repository=repo, commit=head,
        checks=[QaCheck("write", (sys.executable, "-c",
                                  "open('produced.txt','w').write('x')"), required=True)])
    assert not (__import__("pathlib").Path(repo) / "produced.txt").exists()


def test_output_is_redacted(tmp_path, monkeypatch):
    """A credential-looking string in a check's output never reaches the report."""
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setenv("OPENHANDS_API_KEY", secret)
    repo, head = _make_repo(tmp_path)
    report = SelfQa(workdir=tmp_path / "wt").run(
        repository=repo, commit=head,
        checks=[QaCheck("leak", (sys.executable, "-c",
                                 f"print('{secret}'); raise SystemExit(1)"), required=True)])
    # ``json.dumps`` escapes non-ASCII by default, so compare on the raw text.
    stdout = report.checks[0].stdout
    assert secret not in stdout, "the key must not appear in the QA output"
    assert "«redacted»" in stdout
    assert secret not in json.dumps(report.as_dict(), ensure_ascii=False)


def test_run_check_returns_a_result_and_never_raises(tmp_path):
    res = run_check(QaCheck("boom", ("definitely-not-real-xyz",)), tmp_path, timeout=5.0)
    assert res.status == "skipped"
    assert res.ok is False
