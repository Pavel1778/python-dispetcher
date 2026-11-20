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

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

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


#: A repository is named the way the rest of NEXUS names it - ``owner/name`` -
#: not as a URL. Anything else (a URL, a local path, a scp-style remote) is
#: passed through unchanged, so a test or the CLI can still point at a directory.
_OWNER_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def clone_url(repository: str, *, base: str = "https://github.com") -> str:
    """The URL ``git clone`` needs for *repository*.

    A task's ``repository`` is ``owner/name`` - the same string the dispatch
    gate, the GitHub client and the report use. ``git`` cannot clone that: it
    is a *relative path*, and every real project failed with
    ``repository 'owner/name' does not exist``. Only a local directory ever
    worked, which is why the tests passed while the gate was useless in
    production. A string that already looks like a URL or a path is left alone.
    """
    name = (repository or "").strip()
    if not name:
        return name
    if "://" in name or name.startswith("/") or name.startswith("~"):
        return name
    if Path(name).is_dir():
        return str(Path(name).resolve())
    if _OWNER_NAME.fullmatch(name):
        return f"{base.rstrip('/')}/{name}.git"
    return name


def _auth_env(token: str, url: str) -> dict[str, str]:
    """Environment for an authenticated ``git`` call that keeps *token* out of
    argv and out of the clone URL.

    A credential in a URL is written to ``.git/config`` and shows up in the
    child's argv (readable in ``ps``); a header passed with ``-c`` is argv too.
    ``GIT_CONFIG_*`` is the supported way to give git a config value without
    either: the token reaches git through the environment only, and the URL the
    repository is cloned from stays clean. Only https URLs are given a header;
    a token on a local path or an ssh remote would be meaningless.
    """
    if not token or not url.startswith("https://"):
        return {}
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return {}
    # ``x-access-token`` is what GitHub accepts for a PAT used as basic auth
    # (the same identity the GitHub client authenticates with).
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{parts.scheme}://{parts.netloc}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {raw}",
        "GIT_TERMINAL_PROMPT": "0",
    }


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
                 token: str = "",
                 runner: Callable[..., Any] = subprocess.run):
        self.workdir = Path(workdir) if workdir else None
        self.check_timeout = check_timeout
        self.total_timeout = total_timeout
        #: A Git credential for cloning a private repository. Handed to git
        #: through the environment only (``_auth_env``); never logged, never in
        #: argv, never in the URL, and never in a report.
        self.token = (token or "").strip()
        self._runner = runner

    # ------------------------------------------------------------------ source
    def _clone(self, repository: str, commit: str, dest: Path, *,
               branch: str | None = None) -> tuple[bool, str]:
        """A clone of *repository* checked out at *commit* into *dest*.

        *repository* is normally ``owner/name`` (see :func:`clone_url`), which
        is turned into a real https URL here; a local directory is cloned too,
        so the source's own working tree is never run in. When a token is
        configured it reaches git through the environment only - never argv,
        never the URL - so it cannot be read from ``ps`` or left behind in
        ``.git/config``.

        The clone is full, not shallow, because the gate checks a *specific*
        commit (the one the Git verifier accepted) that may not be the head of
        the branch a shallow clone stopped at. If that commit is still missing
        it is fetched by name (a pull request ref, a commit only reachable from
        its own branch). A commit that cannot be reached is a ``blocked``
        report - "not verified", never a pass. Returns ``(ok, detail)``.
        """
        url = clone_url(repository)
        auth = _auth_env(self.token, url)
        env = {**os.environ, **auth} if auth else None
        try:
            self._git(["clone", "--quiet", "--no-hardlinks", url, str(dest)], env=env)
            if commit:
                if not self._git(["cat-file", "-e", f"{commit}^{{commit}}"],
                                 cwd=dest, env=env, check=False):
                    # Not in the clone's history: ask for it by name. A branch
                    # that carries it is tried first, then the commit itself.
                    refs = ([f"+refs/heads/{branch}:refs/nexus-qa/{branch}"] if branch
                            else []) + [commit]
                    for ref in refs:
                        self._git(["fetch", "--quiet", "--no-tags", "origin", ref],
                                  cwd=dest, env=env, check=False)
                        if self._git(["cat-file", "-e", f"{commit}^{{commit}}"],
                                     cwd=dest, env=env, check=False):
                            break
                self._git(["checkout", "--quiet", commit], cwd=dest, env=env)
        except subprocess.CalledProcessError as e:
            return False, f"git failed: {_truncate(e.stderr or b'')}"
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, f"git {type(e).__name__}: {e}"
        return True, ""

    @staticmethod
    def _git(argv: list[str], *, cwd: Path | None = None,
             env: dict[str, str] | None = None, check: bool = True) -> bool:
        """Run one git command. Returns whether it succeeded (when *check* is
        false), and raises on failure otherwise so ``_clone`` reports it."""
        result = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            ["git", *(["-C", str(cwd)] if cwd else []), *argv],
            capture_output=True, timeout=300, check=False, env=env,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr)
        return result.returncode == 0

    # -------------------------------------------------------------------- run
    def run(self, *, repository: str, commit: str = "",
            checks: list[QaCheck] | None = None,
            project_id: str = "", branch: str | None = None) -> QaReport:
        """Clone *repository* at *commit*, run the checks, return the report.

        ``project_id`` and ``branch`` are part of the signature so a plain
        engine and the policy wrapper that decides per project
        (:class:`ProjectQualityGate`) can be used interchangeably by the loop.
        ``branch`` is only used to fetch a commit the default clone depth would
        not contain; ``project_id`` is ignored here (the validator that matters
        for it is on the wrapper).
        """
        del project_id
        started = time.monotonic()
        env = {"python": _py_version(), "cwd_repo": repository, "commit": commit,
               "branch": branch or ""}
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
            # Each run gets its own scratch directory under ``workdir``. A fixed
            # ``workdir/checkout`` was reused by a second run in the same
            # process, so the clone failed ("already exists and is not an empty
            # directory"), the report came back ``blocked``, and a ``blocked``
            # gate leaves the verdict alone - a failing project would pass.
            if base is not None:
                base.mkdir(parents=True, exist_ok=True)
            made = Path(tempfile.mkdtemp(prefix="nexus-selfqa-",
                                         dir=str(base) if base else None))
            base = made
            dest = base / "checkout"
            ok, detail = self._clone(repository, commit, dest, branch=branch)
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


#: The project's quality-gate modes (``nexus_projects.quality_gate``). The
#: column's CHECK is the same set, so a value that reaches the engine is one the
#: database accepted.
GATE_MODES: tuple[str, ...] = ("auto", "on", "off")

#: The file a repository declares its own checks in. ``auto`` means "run the
#: gate only when this file exists", so its name is part of the policy and not
#: only of the check reader.
QA_DECLARATION = ".nexus-qa.json"


def normalise_mode(value: Any) -> str:
    """A gate mode from a stored value, or ``auto`` for anything unexpected.

    Never raises: a row written before the column existed, or by hand, reads as
    the default rather than failing the loop. ``True``/``False`` are accepted
    because the column used to be imagined as a boolean; ``on``/``off`` is what
    they mean.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value or "").strip().lower()
    return text if text in GATE_MODES else "auto"


def declares_checks(repository: str, *, branch: str | None = None,
                    client: Any | None = None) -> bool | None:
    """Whether *repository* declares its own checks (``.nexus-qa.json``).

    Read through the GitHub client the project's own reads use, so a private
    repository is read with the identity that can see it. ``None`` means "could
    not be read" - GitHub unavailable, no client, a revoked account - and the
    caller must treat that as "do not run", never as "nothing declared, so use
    the defaults": a gate that cannot see the declaration has not established
    anything, and ``auto`` is opt-in by declaration.
    """
    if client is None or not repository:
        return None
    try:
        found = client.read_text_file(repository, QA_DECLARATION, branch=branch)
    except Exception as e:  # noqa: BLE001 - a read failure is "not established"
        log.debug("could not read %s of %s: %s", QA_DECLARATION, repository,
                  type(e).__name__)
        return None
    return found is not None


def _declares_default(repository: str, *, branch: str | None = None,
                      project_id: str = "", client: Any | None = None) -> bool | None:
    """The default declaration reader: no client means nothing was established."""
    return declares_checks(repository, branch=branch, client=client)


class ProjectQualityGate:
    """The Self-QA gate, applied per the project's own mode.

    * ``off``  - nothing runs; the verdict is ``skipped`` with a reason that
      says the project turned the gate off. Never a pass, never a failure.
    * ``auto`` - the checks run only when the repository declares them
      (``.nexus-qa.json``). A repository that declares none is ``skipped``.
    * ``on``   - the checks always run: the declared ones, or the engine's
      defaults.

    A token for cloning a private repository is resolved per project through
    ``token_for(project_id)``, which the caller supplies; it is handed to the
    engine, which keeps it out of argv and out of the report. Without a
    ``token_for`` the clone is anonymous, exactly as before.

    The object is *pure policy*: it never decides that a result is a pass. The
    verdict it returns is the engine's, and only ``failed`` ever weakens a task
    verdict in the scheduler.
    """

    def __init__(self, engine: SelfQa, *,
                 mode_for: Callable[[str], Any] | None = None,
                 declares: Callable[..., bool | None] | None = None,
                 token_for: Callable[[str], str] | None = None):
        self.engine = engine
        self._mode_for = mode_for or (lambda project_id: "auto")
        self._declares = declares or _declares_default
        self._token_for = token_for

    def mode(self, project_id: str) -> str:
        try:
            return normalise_mode(self._mode_for(project_id))
        except Exception as e:  # noqa: BLE001 - an unreadable mode is the default
            log.debug("could not read the quality-gate mode of %s: %s",
                      project_id, type(e).__name__)
            return "auto"

    def _engine_for(self, project_id: str) -> SelfQa:
        if self._token_for is None:
            return self.engine
        try:
            token = self._token_for(project_id) or ""
        except Exception as e:  # noqa: BLE001 - no token is anonymous, not fatal
            log.debug("no quality-gate token for %s: %s", project_id,
                      type(e).__name__)
            token = ""
        if not token or token == self.engine.token:
            return self.engine
        # A fresh engine rather than mutating the shared one: two projects may
        # resolve different identities, and the engine is used concurrently.
        return SelfQa(workdir=self.engine.workdir,
                      check_timeout=self.engine.check_timeout,
                      total_timeout=self.engine.total_timeout, token=token)

    def run(self, *, repository: str, commit: str = "", project_id: str = "",
            branch: str | None = None, checks: list[QaCheck] | None = None) -> QaReport:
        mode = self.mode(project_id)
        now = time.monotonic()
        if mode == "off":
            return QaReport(verdict="skipped",
                            reason="the project turned the quality gate off",
                            repository=repository, commit=commit,
                            started_at=now, finished_at=now)
        if mode == "auto":
            declared = self._declares(repository, branch=branch,
                                      project_id=project_id)
            if declared is not True:
                # ``False``: nothing declared, so `auto` has nothing to run.
                # ``None``: the declaration could not be read - also nothing
                # established. Either way the task verdict stands, unchanged.
                why = ("the repository declares no checks (auto)" if declared is False
                       else "the repository's checks could not be read (auto)")
                return QaReport(verdict="skipped", reason=why,
                                repository=repository, commit=commit,
                                started_at=now, finished_at=now)
        return self._engine_for(project_id).run(
            repository=repository, commit=commit, checks=checks,
            project_id=project_id, branch=branch)
