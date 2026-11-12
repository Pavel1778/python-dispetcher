"""Acceptance section G — End-to-End Failover (the most important test).

The task text's sequence:

1. submit a real task;
2. start it on OpenHands #1;
3. wait for work to begin;
4. simulate #1 failing;
5. switch the task to OpenHands #2;
6. check the context was handed over;
7. make sure #1 no longer writes to the workspace;
8. wait for the result;
9. run an independent check;
10. confirm nothing was duplicated.

Everything is real except the two OpenHands transports (there is no second live
Cloud account in this sandbox; the report says so). The transports are honest
fakes of the *client surface*: each owns a **real checkout** — a real directory
with a real git repository — so "did #1 stop writing?" is answered by inspecting
the real files and the real git history, not by reading a flag.

The two writers are separated by NEXUS's own guarantees, both exercised here:

* the failed account is excluded from the task (``tried_accounts``), so it is
  never polled or dispatched again;
* the abandoned conversation is *stopped* (the local transport supports a real
  stop), and until that succeeds the task's branch is held so no second writer
  starts on the same checkout.

NEXUS's scheduler, pool, router, store and branch lock are the real code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import (  # noqa: E402
    CloudConversation,
    CloudNotFound,
    CloudUnavailable,
    StartStatus,
    StartTask,
)
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    GitVerifier,
    LabScheduler,
    OpenHandsExecutor,
    TaskReport,
)
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import (  # noqa: E402
    OpenHandsAccountService,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.openhands_pool import AccountPool  # noqa: E402
from orchestration.openhands_router import AccountRouter  # noqa: E402
from tests.acceptance_kit import reset_schema  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.test_acceptance_dev_task import (  # noqa: E402
    LocalGitHubServer,
    _git,
    _make_repo,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

PRINCIPAL = "user:owner"
REPO = "owner/dev-task"


def _make_bare_origin(origin_dir: str) -> None:
    """A real bare repository with one real commit on ``main``."""
    os.makedirs(origin_dir, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin_dir],
                   check=True, capture_output=True)
    seed = origin_dir + "-seed"
    _make_repo(seed)
    subprocess.run(["git", "-C", seed, "remote", "add", "origin", origin_dir],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", seed, "push", "-q", "origin", "main"],
                   check=True, capture_output=True)
    shutil.rmtree(seed, ignore_errors=True)


class WorkspaceClient:
    """A transport that owns a real checkout and can be told to fail.

    ``start_conversation`` makes the agent "begin work": it writes a marker file
    into its own real clone and records the conversation. ``stop`` marks the
    client stopped, and a stopped client refuses to write again — so a test can
    assert the abandoned executor left no further trace.
    """

    supports_files = False
    supports_conversation_stop = True

    def __init__(self, name: str, workspace: str, origin: str, *,
                 start_fails: bool = False):
        self.name = name
        self.workspace = workspace
        self.origin = origin
        self.start_fails = start_fails
        self.stopped = False
        self.writes: list[str] = []
        self.conversations: dict[str, dict] = {}
        self.started: list[dict] = []
        # A real clone of the real origin, so a commit here can be pushed back.
        subprocess.run(["git", "clone", "-q", origin, workspace],
                       check=True, capture_output=True)
        _git(workspace, "config", "user.email", f"{name}@nexus.test")
        _git(workspace, "config", "user.name", name)

    def _write(self, name: str, text: str) -> None:
        if self.stopped:
            raise AssertionError(f"{self.name} wrote after it was stopped: {name}")
        os.makedirs(self.workspace, exist_ok=True)
        with open(os.path.join(self.workspace, name), "w") as handle:
            handle.write(text)
        self.writes.append(name)

    def start_conversation(self, *, initial_message, title=None, tags=None,
                           selected_repository=None, selected_branch=None, **kw):
        if self.start_fails:
            raise CloudUnavailable(f"{self.name}: sandbox unavailable")
        cid = f"{self.name}-conv-{len(self.started)}"
        self.started.append({"id": cid, "message": initial_message})
        # Work really begins: the agent writes its first artefact.
        self._write(f"{self.name}-started.txt", "work began\n")
        self.conversations[cid] = {"status": "running", "commit": None}
        return StartTask(id=cid, status=StartStatus.READY.value, app_conversation_id=cid)

    def get_conversation(self, conversation_id):
        conv = self.conversations.get(conversation_id)
        if conv is None:
            raise CloudNotFound(conversation_id)
        return CloudConversation(id=conversation_id, title=None, sandbox_id=f"{self.name}-sb",
                                 execution_status=conv["status"], sandbox_status="RUNNING",
                                 selected_repository=REPO, selected_branch="main",
                                 conversation_url=None, raw={})

    def final_response(self, conversation_id):
        return self.conversations[conversation_id].get("text", "")

    def stop_conversation(self, conversation_id):
        self.stopped = True
        return True

    def account_capabilities(self):
        return {"llm_api_key_set": True, "active_agent_profile_id": f"{self.name}-p",
                "mcp_servers": [], "registered_marketplaces": 0,
                "inherited_marketplaces": 0, "disabled_skills": [],
                "provider_tokens_set": {}}

    def delete_conversation(self, conversation_id):
        return True


class FinishingExecutor:
    """The real executor, with the transport's work completed by a real commit.

    It delegates dispatch/poll to :class:`OpenHandsExecutor` (so the account
    selection, the hand-off and the fencing are the real code); on the final poll
    it commits the artefact in the workspace the account owns and finishes.
    """

    def __init__(self, executor: OpenHandsExecutor, clients: dict):
        self.executor = executor
        self.clients = clients
        self.finish_after = 2          # the second poll commits and finishes
        self._polls: dict[str, int] = {}

    def dispatch(self, task: dict) -> str:
        return self.executor.dispatch(task)

    def poll(self, task: dict, conversation_id: str) -> TaskReport:
        report = self.executor.poll(task, conversation_id)
        if report.state != "running":
            return report
        key = task["id"]
        self._polls[key] = self._polls.get(key, 0) + 1
        if self._polls[key] < self.finish_after:
            return report
        account_id = str(task.get("openhands_account_id") or "")
        client = self.clients.get(account_id)
        if client is None:
            return report
        # The agent finishes its work: it writes the change, commits it in its
        # own checkout, and pushes it to the branch. This is what the Git
        # verifier then reads from the branch head.
        client._write("result.py", "def add(a, b):\n    return a + b\n")
        _git(client.workspace, "add", "-A")
        _git(client.workspace, "commit", "-q", "-m", "result")
        _git(client.workspace, "push", "-q", "origin", "main")
        head = _git(client.workspace, "rev-parse", "HEAD")
        client.conversations[conversation_id]["commit"] = head
        client.conversations[conversation_id]["text"] = f"Done. Commit {head}."
        client.conversations[conversation_id]["status"] = "finished"
        return TaskReport(state="finished",
                          final_text=f"Done. Commit {head}.",
                          conversation_id=conversation_id)

    def stop(self, account_id, conversation_id, project_id=None) -> bool:
        return self.executor.stop(account_id, conversation_id, project_id)


# ------------------------------------------------------------------- fixtures
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


def _project_run(store):
    projects = ProjectService(store)
    project = projects.create(PRINCIPAL, "E2E failover")
    store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
    from orchestration.nexus_binding import ProjectRepositoryService

    ProjectRepositoryService(store).link(PRINCIPAL, str(project["id"]), REPO)
    goal, _ = projects.create_goal(PRINCIPAL, str(project["id"]), "Do the work")
    lab = LabStore(store)
    run = lab.start_run(
        PRINCIPAL, str(project["id"]), str(goal["id"]), max_iterations=10,
        plan=[{"id": "T1", "title": "Implement", "description": "Write result.py",
               "acceptance": ["it works"], "dependencies": []}],
        repository=REPO, branch="main")
    return project, run, lab


# ------------------------------------------------------------- the whole test
def test_end_to_end_failover_keeps_the_context_and_never_duplicates(
        store, service, clouds, tmp_path):
    """The task text's ten steps, all asserted on real state."""
    va, vb = _two_accounts(service, clouds)
    origin = str(tmp_path / "origin.git")
    _make_bare_origin(origin)

    with LocalGitHubServer(origin) as github:
        github_client = GitHubClient(base_url=github.base_url, token="t")
        pool = AccountPool(store, service)
        pool.set_config(PRINCIPAL, selection_mode="failover")
        pool.set_priority(PRINCIPAL, vb["id"], 1)
        lab = LabStore(store)
        router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)

        # 1. A real task.  #1 starts it; #2 is the backup.
        project, run, _ = _project_run(store)
        ws_a = str(tmp_path / "ws-a")
        ws_b = str(tmp_path / "ws-b")
        client_a = WorkspaceClient("A", ws_a, origin)
        client_b = WorkspaceClient("B", ws_b, origin)
        clients = {va["id"]: client_a, vb["id"]: client_b}

        real = OpenHandsExecutor(None, router=router,
                                 client_factory=lambda account_id, task=None:
                                 clients[account_id])
        executor = FinishingExecutor(real, clients)
        verifier = GitVerifier(github_client, client_for=lambda _task: github_client)
        scheduler = LabScheduler(lab, executor, verifier, worker_id="w", router=router)

        # 2/3. The task starts on #1 and work really begins.
        scheduler.tick(str(run["id"]))
        task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
        assert task["state"] == "running"
        assert str(task["openhands_account_id"]) == va["id"]
        assert "A-started.txt" in client_a.writes
        assert len(client_a.started) == 1 and client_b.started == []

        # 4. #1 fails: its sandbox is gone, so its conversation can no longer be
        #    seen. The next tick requeues the task for failover (nothing is left
        #    to stop: the conversation is already gone).
        client_a.conversations.clear()
        scheduler.tick(str(run["id"]))
        task = lab.get_task_internal(task["id"])
        assert va["id"] in task["tried_accounts"], "#1 must be excluded"

        # 5. The task moves to #2.
        assert len(client_b.started) == 1, "the task must be handed to #2"
        assert str(task["openhands_account_id"]) == vb["id"]

        # 6. The context was handed over: the message names the plan and the task.
        handoff = client_b.started[0]["message"]
        assert "continuing an interrupted task" in handoff
        assert "T1" in handoff and "Implement" in handoff
        assert REPO in handoff and "main" in handoff
        handoffs = lab.handoffs(task["id"])
        assert handoffs and str(handoffs[0]["from_account_id"]) == va["id"]
        assert str(handoffs[0]["to_account_id"]) == vb["id"]

        # 7. #1 no longer writes. Its conversation is gone and #1 is excluded
        #    from the task, so nothing dispatches to it again; its checkout holds
        #    only the one artefact it wrote before the failure, and no result.
        assert client_a.writes == ["A-started.txt"], (
            "#1 wrote after the task moved to #2")
        assert not os.path.exists(os.path.join(ws_a, "result.py"))
        assert va["id"] not in {str(task["openhands_account_id"])}

        # 8. #2 finishes and the run completes.
        done = None
        for _ in range(4):
            scheduler.tick(str(run["id"]))
            state = lab.get_run(PRINCIPAL, str(project["id"]), str(run["id"]))["state"]
            if state in ("completed", "blocked", "failed"):
                done = state
                break
        assert done == "completed", f"the run ended {done}"
        tasks = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))
        assert tasks[0]["state"] == "completed_verified"

        # 9. Independent check: #2's commit is a real commit in a real repo, and
        #    the verifier read it from the branch head.
        head = client_b.conversations["B-conv-0"]["commit"]
        assert len(head) == 40
        diff = _git(ws_b, "show", "--stat", "HEAD")
        assert "result.py" in diff

        # 10. No duplication: each account started exactly one conversation, and
        #     the work was never started twice on the same account.
        assert len(client_a.started) == 1
        assert len(client_b.started) == 1


def test_the_abandoned_executor_cannot_write_to_the_branch(store, service, clouds,
                                                          tmp_path):
    """A switched task holds its branch until the old writer is really stopped.

    The operator switches the task to #2 while #1's conversation is alive. NEXUS
    must not start a second writer on the same checkout until the old
    conversation is stopped; the stop fails once here, so #2 stays out for that
    pass, and starts only once the stop is confirmed.
    """
    va, vb = _two_accounts(service, clouds)
    origin = str(tmp_path / "origin.git")
    _make_bare_origin(origin)
    pool = AccountPool(store, service)
    pool.set_config(PRINCIPAL, selection_mode="failover")
    pool.set_priority(PRINCIPAL, vb["id"], 1)
    lab = LabStore(store)
    router = AccountRouter(store, pool=pool, lab=lab, principal=PRINCIPAL)

    class StubbornClient(WorkspaceClient):
        """A client whose stop keeps failing until the test lets it through."""

        def __init__(self, name, workspace, origin, **kw):
            super().__init__(name, workspace, origin, **kw)
            self.stop_calls = 0
            self.allow_stop = False

        def stop_conversation(self, conversation_id):
            self.stop_calls += 1
            if not self.allow_stop:
                return False
            return super().stop_conversation(conversation_id)

        def conversation_state(self, item):
            # The stop is only *confirmed* once the conversation reports a
            # stopped state, which it does after the real stop succeeds.
            return "paused" if self.stopped else "running"

    client_a = StubbornClient("A", str(tmp_path / "wa"), origin)
    client_b = WorkspaceClient("B", str(tmp_path / "wb"), origin)
    clients = {va["id"]: client_a, vb["id"]: client_b}
    real = OpenHandsExecutor(None, router=router,
                             client_factory=lambda account_id, task=None:
                             clients[account_id])
    executor = FinishingExecutor(real, clients)
    scheduler = LabScheduler(lab, executor, verifier=_AlwaysVerified(),
                             worker_id="w", router=router)
    project, run, _ = _project_run(store)

    # T1 starts on A; then the operator switches it to B while A is still alive.
    scheduler.tick(str(run["id"]))
    task = lab.list_tasks(PRINCIPAL, str(project["id"]), str(run["id"]))[0]
    assert task["state"] == "running"
    lab.switch_task_executor(task["id"], target_account_id=vb["id"])
    assert lab.pending_stops(str(run["id"])), "the switch must owe a stop"

    scheduler.tick(str(run["id"]))                 # the stop cannot be made
    # The stop was not confirmed, so the branch is held: B has not started, and
    # the stop is still owed (it is a row, not a thread).
    assert client_a.stop_calls >= 1
    assert client_b.started == [], "B must not start while the old writer is not stopped"
    assert lab.pending_stops(str(run["id"])), "the stop is still owed"

    # The executor can be stopped now; the next pass stops it and B starts.
    client_a.allow_stop = True
    scheduler.tick(str(run["id"]))
    assert client_a.stopped is True
    assert len(client_b.started) == 1, "B starts once the old writer is stopped"


class _AlwaysVerified:
    """A verifier that says 'verified' so the run can complete without a repo."""

    def verify(self, task, report):
        from orchestration.nexus_lab import Verification

        return Verification("completed_verified", "stub", {"state": report.state})
