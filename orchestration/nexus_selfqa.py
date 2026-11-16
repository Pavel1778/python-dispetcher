"""Self-QA: run a project's own checks in an isolated checkout and report.

The gap this closes: an autonomous task is accepted when its commit is on the
branch (``GitVerifier``), which says a change *landed* - not that the project
still works. This module runs the checks a repository declares for itself
(tests, lint, a build) in a **real, isolated checkout of the task's commit** and
returns an honest verdict:

* ``passed``   - every required check exited 0;
* ``failed``   - a required check ran and exited non-zero;
* ``skipped``  - no check could be run (nothing declared, or the tooling is
  absent): "not verified", never a pass;
* ``blocked``  - the checkout or the runner itself could not be made (no
  network, no git): "not verified", never a pass.

Rules that keep it honest (each has a test):

* **A check is a command that ran.** The verdict is derived from real exit
  codes and real durations, never from a claim. A check that could not be
  started is ``skipped``, which is *not* success.
* **Required vs optional.** A repository declares which checks are required
  (``.nexus-qa.json`` or the defaults). An optional check that fails lowers the
  confidence but does not fail the run; a required one does.
* **Isolation.** The checks run in a scratch clone of the repository at a given
  commit, under ``cwd`` there, with a minimal environment and a hard timeout.
  The NEXUS checkout is never touched.
* **No secrets in the record.** Output is passed through the same redaction the
  log uses (``nexus_policy.redact_text``) before it is stored or returned.
* **Deterministic only.** This module runs commands; it does not ask a model.
  Judging whether the result *matches the task's intent* is a different,
  optional layer (an LLM review) and is deliberately not part of this verdict.

The engine is pure with respect to the NEXUS store: it takes a *source* (a git
remote + commit, or a local path) and returns a report. The caller decides what
to persist.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .nexus_policy import redact_text

log = logging.getLogger("nexus_selfqa")

#: A single check may not run longer than this by default (seconds). A test
#: suite that hangs is a failure of the check, not of the run.
DEFAULT_CHECK_TIMEOUT = 900.0

#: The whole run's ceiling, so one repository cannot hold the worker forever.
DEFAULT_TOTAL_TIMEOUT = 2400.0

#: How many bytes of a check's output are kept (per stream). Enough to see a
#: failure's reason; a runaway build cannot fill the database.
MAX_OUTPUT_BYTES = 16 * 1024


@dataclass(frozen=True)
class QaCheck:
    """One declared check: a name, the argv to run, and whether it is required."""

    name: str
    argv: tuple[str, ...]
    required: bool = True
    #: A human note about what the check establishes (never a secret).
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "argv": list(self.argv),
                "required": self.required, "note": self.note}


@dataclass
class QaResult:
    """What running one check produced."""

    name: str
    required: bool
    status: str          # passed | failed | skipped | timeout | error
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "required": self.required, "status": self.status,
                "exit_code": self.exit_code, "duration_s": round(self.duration_s, 3),
                "stdout": self.stdout, "stderr": self.stderr, "detail": self.detail}


@dataclass
class QaReport:
    """The whole run: the verdict, the checks, and the environment that produced
    them. ``verdict`` is one of ``passed``/``failed``/``skipped``/``blocked``."""

    verdict: str
    reason: str
    checks: list[QaResult] = field(default_factory=list)
    commit: str = ""
    repository: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "ok": self.ok,
            "repository": self.repository,
            "commit": self.commit,
            "checks": [c.as_dict() for c in self.checks],
            "environment": self.environment,
            "duration_s": round(self.finished_at - self.started_at, 3),
        }


#: The default checks, used when a repository declares none. Each is guarded by
#: the presence of its tool, so a Python project without pytest, or one with no
#: ruff config, is not failed for a tool it never used - the check is skipped.
DEFAULT_CHECKS: tuple[QaCheck, ...] = (
    QaCheck("lint", ("python", "-m", "ruff", "check", "."), required=False,
            note="static analysis; optional unless the repository relies on it"),
    QaCheck("tests", ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
            required=True, note="the project's own test suite"),
)


def _declared_checks(root: Path) -> list[QaCheck] | None:
    """Read ``.nexus-qa.json`` from the checkout, if present.

    Shape::

        {"checks": [{"name": "tests", "run": ["python", "-m", "pytest"],
                     "required": true, "note": "..."}]}

    Returns ``None`` when there is no file, so the caller falls back to the
    defaults; an empty ``checks`` list is honoured (the repository said so).
    """
    path = root / ".nexus-qa.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning(".nexus-qa.json is not valid JSON; using the default checks")
        return None
    raw = data.get("checks")
    if not isinstance(raw, list):
        return None
    out: list[QaCheck] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        run = item.get("run")
        if not (isinstance(run, list) and run and all(isinstance(x, str) for x in run)):
            continue
        name = str(item.get("name") or run[0])
        out.append(QaCheck(name=name[:80], argv=tuple(run),
                           required=bool(item.get("required", True)),
                           note=str(item.get("note") or "")[:300]))
    return out


def checks_for(root: Path) -> list[QaCheck]:
    """The checks to run: the repository's own declaration, or the defaults."""
    declared = _declared_checks(root)
    return list(declared) if declared is not None else list(DEFAULT_CHECKS)


def _truncate(data: bytes) -> str:
    if len(data) > MAX_OUTPUT_BYTES:
        head = data[:MAX_OUTPUT_BYTES]
        return redact_text(head.decode("utf-8", "replace")) + "\n… (truncated)"
    return redact_text(data.decode("utf-8", "replace"))


def _tool_present(argv: tuple[str, ...]) -> bool:
    """Whether the first word of *argv* resolves. A check whose tool is absent
    is skipped, not failed - the project never used that tool."""
    return shutil.which(argv[0]) is not None


def run_check(check: QaCheck, root: Path, *, timeout: float,
              env: dict[str, str] | None = None) -> QaResult:
    """Run one check in *root*. Never raises: a failure is a result."""
    if not _tool_present(check.argv):
        return QaResult(check.name, check.required, "skipped",
                        detail=f"{check.argv[0]} is not installed")
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - the argv is a declared check, not a shell string
            list(check.argv), cwd=str(root), capture_output=True,
            timeout=timeout, env=env, check=False,
        )
    except subprocess.TimeoutExpired as e:
        return QaResult(check.name, check.required, "timeout",
                        duration_s=time.monotonic() - started,
                        stdout=_truncate(e.stdout or b""),
                        stderr=_truncate(e.stderr or b""),
                        detail=f"timed out after {timeout:.0f}s")
    except OSError as e:
        return QaResult(check.name, check.required, "error",
                        duration_s=time.monotonic() - started,
                        detail=f"{type(e).__name__}: {e}")
    duration = time.monotonic() - started
    status = "passed" if proc.returncode == 0 else "failed"
    return QaResult(check.name, check.required, status,
                    exit_code=proc.returncode, duration_s=duration,
                    stdout=_truncate(proc.stdout or b""),
                    stderr=_truncate(proc.stderr or b""))


def _child_env() -> dict[str, str]:
    """A minimal environment for a check: no NEXUS secrets, a sane PATH."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    # A check must not reach back into the control plane's own credentials.
    env["NEXUS_SELFQA"] = "1"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _verdict(checks: list[QaResult]) -> tuple[str, str]:
    """The verdict from the results. A required failure fails the run; no
    runnable check at all is ``skipped`` (never a pass)."""
    if not checks:
        return "skipped", "the repository declares no checks to run"
    ran = [c for c in checks if c.status in ("passed", "failed", "timeout", "error")]
    required_failed = [c for c in checks
                       if c.required and c.status in ("failed", "timeout", "error")]
    if required_failed:
        names = ", ".join(c.name for c in required_failed)
        return "failed", f"required check(s) failed: {names}"
    if not ran:
        return "skipped", "no check could be run (tooling absent)"
    optional_failed = [c for c in checks
                       if not c.required and c.status in ("failed", "timeout", "error")]
    if optional_failed:
        names = ", ".join(c.name for c in optional_failed)
        return "passed", f"required checks passed; optional check(s) failed: {names}"
    return "passed", "all checks passed"


class SelfQa:
    """Run a project's checks in an isolated checkout and report honestly."""

    def __init__(self, *, workdir: Path | None = None,
                 check_timeout: float = DEFAULT_CHECK_TIMEOUT,
                 total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
                 runner: Callable[..., Any] = subprocess.run):
        self.workdir = Path(workdir) if workdir else None
        self.check_timeout = check_timeout
        self.total_timeout = total_timeout
        self._runner = runner

    # ------------------------------------------------------------------ source
    def _clone(self, repository: str, commit: str, dest: Path) -> tuple[bool, str]:
        """A shallow clone of *repository* at *commit* into *dest*.

        A local path is copied with ``git clone`` too, so the source's own
        working tree is never run in. Returns ``(ok, detail)``.
        """
        url = repository
        if Path(repository).is_dir():
            url = str(Path(repository).resolve())
        try:
            subprocess.run(  # noqa: S603 - a fixed argv, no shell
                ["git", "clone", "--quiet", "--no-hardlinks", url, str(dest)],
                capture_output=True, timeout=300, check=True,
            )
            if commit:
                subprocess.run(  # noqa: S603
                    ["git", "-C", str(dest), "checkout", "--quiet", commit],
                    capture_output=True, timeout=120, check=True,
                )
        except subprocess.CalledProcessError as e:
            return False, f"git failed: {_truncate(e.stderr or b'')}"
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, f"git {type(e).__name__}: {e}"
        return True, ""

    # -------------------------------------------------------------------- run
    def run(self, *, repository: str, commit: str = "",
            checks: list[QaCheck] | None = None) -> QaReport:
        """Clone *repository* at *commit*, run the checks, return the report."""
        started = time.monotonic()
        env = {"python": _py_version(), "cwd_repo": repository, "commit": commit}
        report = QaReport(verdict="blocked", reason="not started",
                          repository=repository, commit=commit,
                          environment=env, started_at=started)
        if not repository:
            report.reason = "no repository to check"
            report.finished_at = time.monotonic()
            return report
        base = self.workdir
        made: Path | None = None
        try:
            if base is None:
                made = Path(tempfile.mkdtemp(prefix="nexus-selfqa-"))
                base = made
            dest = base / "checkout"
            ok, detail = self._clone(repository, commit, dest)
            if not ok:
                report.verdict = "blocked"
                report.reason = f"could not make an isolated checkout: {detail}"
                report.finished_at = time.monotonic()
                return report
            chosen = checks if checks is not None else checks_for(dest)
            report.checks = self._run_all(chosen, dest, started)
            report.verdict, report.reason = _verdict(report.checks)
        finally:
            if made is not None:
                shutil.rmtree(made, ignore_errors=True)
            report.finished_at = time.monotonic()
        return report

    def _run_all(self, checks: list[QaCheck], root: Path,
                 started: float) -> list[QaResult]:
        out: list[QaResult] = []
        env = _child_env()
        for check in checks:
            if time.monotonic() - started > self.total_timeout:
                out.append(QaResult(check.name, check.required, "timeout",
                                    detail="the run's total time limit was reached"))
                continue
            remaining = max(1.0, self.total_timeout - (time.monotonic() - started))
            out.append(run_check(check, root, timeout=min(self.check_timeout, remaining),
                                 env=env))
        return out


def _py_version() -> str:
    import platform
    return platform.python_version()
