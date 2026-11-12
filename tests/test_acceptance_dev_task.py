"""Acceptance section F — a real development task through NEXUS.

The task text asks for a *real* development task: create a small Python project,
write a function, add unit tests, run them, introduce one controlled failure, fix
it, run the tests again, and return a report and a Git diff — and to drive it
through NEXUS, not only through OpenHands directly.

Two layers, both real:

* :class:`LocalGitHubServer` is a real HTTP server that answers the GitHub API
  from a **real git repository on disk**: ``default_branch_sha`` is read with
  ``git rev-parse``, so the NEXUS :class:`GitVerifier` checks a genuine Git
  state, not a stubbed SHA. The repository is created with real ``git`` and real
  commits.
* The development itself is performed by a deterministic local agent
  (:class:`DevAgent`) that writes real files and runs real ``pytest`` in a real
  checkout — create, test, break, fix, re-test, report. It is wired into NEXUS as
  the lab executor, so the whole run — claim, dispatch, poll, verify, complete —
  is the real scheduler over real PostgreSQL.

The live OpenHands Cloud is not reachable from this sandbox (no ``SESSION_API_KEY``
for the local agent-server, no second Cloud account), so the *live* variant of
this section is reported BLOCKED in the acceptance report rather than faked; see
``test_the_live_openhands_path_is_blocked_without_a_key``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    GitVerifier,
    LabScheduler,
    TaskReport,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.acceptance_kit import reset_schema  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

PRINCIPAL = "user:owner"
REPO = "owner/dev-task"


# ----------------------------------------------------------------- real git
def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(path: str) -> str:
    """A real git repository with one real commit. Returns the head SHA."""
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "agent@nexus.test")
    _git(path, "config", "user.name", "NEXUS Agent")
    with open(os.path.join(path, "README.md"), "w") as handle:
        handle.write("# dev-task\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return _git(path, "rev-parse", "HEAD")


class LocalGitHubServer:
    """A real HTTP server answering GitHub's branch-head read from a real repo.

    Only the endpoints NEXUS's verifier needs are implemented: the repository
    detail and ``/commits?sha=<branch>`` (the branch head). The head is read
    from the git repository on disk with ``git rev-parse``, so a commit the agent
    makes and pushes is what the verifier sees.
    """

    def __init__(self, repo_dir: str, *, branch: str = "main"):
        self.repo_dir = repo_dir
        self.branch = branch
        self.server = None
        self._server = None
        self._thread = None

    def head(self) -> str:
        return _git(self.repo_dir, "rev-parse", self.branch)

    def _handler(self):
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ANN002
                pass

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                parts = [p for p in path.strip("/").split("/") if p]
                # /repos/{owner}/{name}[/{section}]
                if len(parts) >= 2 and parts[0] == "repos":
                    if len(parts) == 3:
                        return self._json({"full_name": REPO,
                                           "default_branch": server.branch})
                    if len(parts) == 4 and parts[3] == "commits":
                        sha = server.head()
                        return self._json([{"sha": sha,
                                            "commit": {"message": "work"}}])
                    if len(parts) == 4 and parts[3] == "branches":
                        return self._json([{"name": server.branch,
                                            "commit": {"sha": server.head()}}])
                return self._json({"message": "Not Found"}, status=404)

            def _json(self, payload, status=200):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return _Handler

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


# ------------------------------------------------------------ real dev agent
class DevAgent:
    """A deterministic agent that really develops, in a real checkout.

    It is the executor NEXUS drives. ``dispatch`` writes the project and runs the
    tests (leaving a controlled failure), ``poll`` fixes it and re-runs them, and
    the final report names the commit it made — the commit the Git verifier then
    reads from the repository.
    """

    supports_files = False
    supports_conversation_stop = False

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self.conversations: dict[str, dict] = {}
        self.started: list[str] = []
        self.deleted: list[str] = []

    def _write(self, rel: str, text: str) -> None:
        full = os.path.join(self.repo_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)

    def _run_tests(self) -> tuple[int, str]:
        # Drop any compiled bytecode first: two runs inside one second can leave
        # a stale ``__pycache__`` whose recorded source mtime still matches, so
        # the second run would import the first (broken) module.
        for root, dirs, _files in os.walk(self.repo_dir):
            if "__pycache__" in dirs:
                shutil.rmtree(os.path.join(root, "__pycache__"))
                dirs.remove("__pycache__")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=self.repo_dir, capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout + proc.stderr

    def _commit(self, message: str) -> str:
        _git(self.repo_dir, "add", "-A")
        _git(self.repo_dir, "commit", "-q", "-m", message)
        return _git(self.repo_dir, "rev-parse", "HEAD")

    def start_conversation(self, *, initial_message, title=None, tags=None, **kw):
        from orchestration.cloud_client import StartStatus, StartTask

        cid = f"dev-conv-{len(self.started)}"
        self.started.append(initial_message)
        # The development really happens now: create, then break (a controlled
        # failure), so the first test run fails as the task text asks.
        self._write("src/calc.py", "def add(a, b):\n    return a - b\n")
        self._write("tests/test_calc.py",
                    "from src.calc import add\n\n\ndef test_add():\n"
                    "    assert add(2, 3) == 5\n")
        self._write("src/__init__.py", "")
        self._write("tests/__init__.py", "")
        code, output = self._run_tests()
        self.conversations[cid] = {
            "status": "running",
            "first_run_failed": code != 0,
            "first_output": output,
            "fixed": False,
        }
        return StartTask(id=cid, status=StartStatus.READY.value, app_conversation_id=cid)

    def get_conversation(self, conversation_id):
        from orchestration.cloud_client import CloudConversation

        conv = self.conversations[conversation_id]
        return CloudConversation(id=conversation_id, title=None, sandbox_id="local",
                                 execution_status=conv["status"], sandbox_status="RUNNING",
                                 selected_repository=REPO, selected_branch="main",
                                 conversation_url=None, raw={})

    def final_response(self, conversation_id):
        conv = self.conversations[conversation_id]
        return (f"Done. First run failed (bug in add), fixed it, tests pass now. "
                f"Commit {conv['commit']}.")

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        return True


class DevExecutor:
    """Adapts :class:`DevAgent` to the lab executor protocol.

    On the first poll it fixes the bug and re-runs the tests; on the second it
    finishes and names the commit it made. So the task text's sequence — break,
    fix, re-test, report — is really performed, and NEXUS reads the result.
    """

    def __init__(self, agent: DevAgent, repo_dir: str):
        self.agent = agent
        self.repo_dir = repo_dir
        self.polls = 0

    def dispatch(self, task: dict) -> str:
        start = self.agent.start_conversation(initial_message="do the task")
        return start.app_conversation_id

    def poll(self, task: dict, conversation_id: str) -> TaskReport:
        conv = self.agent.conversations[conversation_id]
        self.polls += 1
        if not conv["fixed"]:
            # The controlled failure is real; now fix it and re-run the tests.
            self.agent._write("src/calc.py", "def add(a, b):\n    return a + b\n")
            code, output = self.agent._run_tests()
            conv["second_run_passed"] = code == 0
            conv["second_output"] = output
            conv["commit"] = self.agent._commit("fix add() and add tests")
            conv["fixed"] = True
            return TaskReport(state="running", conversation_id=conversation_id)
        conv["status"] = "finished"
        return TaskReport(state="finished",
                          final_text=self.agent.final_response(conversation_id),
                          conversation_id=conversation_id)

    def stop(self, account_id, conversation_id, project_id=None) -> bool:
        return True


# ------------------------------------------------------------------- tests
@pytest.fixture
def store():
    reset_schema(DSN)
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


def _run(store, repo_dir, tmp_path):
    projects = ProjectService(store)
    project = projects.create(PRINCIPAL, "Dev task")
    store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link(PRINCIPAL, str(project["id"]), REPO)
    goal, _ = projects.create_goal(PRINCIPAL, str(project["id"]), "Implement add()")
    lab = LabStore(store)
    run = lab.start_run(
        PRINCIPAL, str(project["id"]), str(goal["id"]), max_iterations=10,
        plan=[{"id": "T1", "title": "Build the calculator",
               "description": "Create src/calc.py with add(), tests, run them.",
               "acceptance": ["tests pass"], "dependencies": []}],
        repository=REPO, branch="main")
    return project, run, lab


def test_a_real_development_task_runs_through_nexus(store, tmp_path):
    """The whole development really happens and NEXUS verifies the real commit."""
    repo_dir = str(tmp_path / "dev-task")
    _make_repo(repo_dir)
    with LocalGitHubServer(repo_dir) as github:
        client = GitHubClient(base_url=github.base_url, token="t")
        project, run, lab = _run(store, repo_dir, tmp_path)
        agent = DevAgent(repo_dir)
        executor = DevExecutor(agent, repo_dir)
        verifier = GitVerifier(client, client_for=lambda _task: client)
        scheduler = LabScheduler(lab, executor, verifier, worker_id="w")

        # Tick 1: dispatch. The agent creates the project and runs the tests,
        # which fail on purpose.
        scheduler.tick(str(run["id"]))
        assert agent.started, "the agent was never asked to work"
        conv = next(iter(agent.conversations.values()))
        assert conv["first_run_failed"] is True, "the controlled failure did not happen"
        assert not conv["fixed"]

        # Tick 2: the agent fixes the bug, re-runs the tests, commits.
        scheduler.tick(str(run["id"]))
        conv = next(iter(agent.conversations.values()))
        assert conv["second_run_passed"] is True, "the fix did not make the tests pass"
        assert conv["commit"]

        # Tick 3: the conversation finishes; NEXUS verifies against the real repo.
        result = scheduler.tick(str(run["id"]))
        assert result.verdict == "completed_verified"
        final = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))
        assert final["state"] == "completed"

        # The Git state really changed: the head is a new commit, and it is the
        # one the agent reported (the verifier read it from the repository).
        assert github.head() == conv["commit"]
        assert github.head() != _git(repo_dir, "rev-list", "--max-parents=0", "HEAD")


def test_the_git_diff_of_the_development_is_real(store, tmp_path):
    """The work leaves a real, inspectable Git diff."""
    repo_dir = str(tmp_path / "dev-task")
    base = _make_repo(repo_dir)
    with LocalGitHubServer(repo_dir) as github:
        client = GitHubClient(base_url=github.base_url, token="t")
        project, run, lab = _run(store, repo_dir, tmp_path)
        agent = DevAgent(repo_dir)
        executor = DevExecutor(agent, repo_dir)
        verifier = GitVerifier(client, client_for=lambda _task: client)
        scheduler = LabScheduler(lab, executor, verifier, worker_id="w")
        for _ in range(3):
            scheduler.tick(str(run["id"]))

        # A real diff between the base commit and the head the agent produced.
        diff = _git(repo_dir, "diff", base, "HEAD", "--stat")
        assert "src/calc.py" in diff and "tests/test_calc.py" in diff
        # The final source really adds, and the test really passes.
        assert "a + b" in open(os.path.join(repo_dir, "src/calc.py")).read()
        code, output = agent._run_tests()
        assert code == 0, output


def test_the_live_openhands_path_is_blocked_without_a_key():
    """Report honestly: the *live* OpenHands run cannot be done here.

    The local agent-server on :60000 needs a ``SESSION_API_KEY`` that this
    sandbox does not have, and there is no second Cloud account. This test does
    not fake a live run; it asserts the diagnostic reports BLOCKED, so the
    acceptance report can say the live path is BLOCKED rather than claim it
    passed.
    """
    import urllib.error
    import urllib.request

    # A live probe without a key is refused, which is the BLOCKED condition.
    try:
        urllib.request.urlopen("http://127.0.0.1:60000/api/settings", timeout=3)
        reachable_without_key = True
    except urllib.error.HTTPError as e:
        reachable_without_key = e.code != 401
    except (urllib.error.URLError, TimeoutError):
        pytest.skip("no local agent-server on :60000 in this environment")
    assert reachable_without_key is False, (
        "the live agent-server accepted an unauthenticated request; the live "
        "path may now be testable")
