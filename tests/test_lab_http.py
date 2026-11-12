"""The autonomous lab over real HTTP: context, plan, run, verify, status.

Uses the real web server and PostgreSQL. The provider is a real local HTTP
server (the fake provider from tests/test_providers.py); the GitHub client
points at the real fake GitHub server. So the whole path is exercised without
mocking a client.
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_lab import LabScheduler, TaskReport, Verification  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import AuthService, hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_github import FakeGitHubServer  # noqa: E402
from tests.test_nexus_projects import request  # noqa: E402
from tests.test_providers import FakeProvider, chat_body  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

PLAN = {"tasks": [
    {"id": "audit", "title": "Audit the repo", "acceptance": ["summary written"]},
    {"id": "impl", "title": "Implement feature", "dependencies": ["audit"],
     "acceptance": ["tests pass"]},
]}


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class _FakeExec:
    def dispatch(self, task):
        return f"conv-{task['task_key']}"

    def poll(self, task, conversation_id):
        return TaskReport(state="finished", final_text="ok", conversation_id=conversation_id)


class _FakeVerifier:
    def verify(self, task, report):
        return Verification("completed_verified", "test", {"task": task["task_key"]})


@pytest.fixture
def harness():
    _reset()
    gh = FakeGitHubServer()
    gh.__enter__()
    provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    provider.script = [{"payload": chat_body(PLAN)}]
    provider.calls = []
    provider.index = 0
    t = threading.Thread(target=provider.serve_forever, daemon=True)
    t.start()

    monkeypatch_env = {"OPENHANDS_API_KEY": "fake"}
    old = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)

    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("t"),
                           session_secret="test-session-secret-which-is-long-enough")
    app.github = GitHubClient(gh.base_url, token="t", timeout=5.0)
    app.providers = ProviderCascade([
        GroqProvider(base_url=f"http://127.0.0.1:{provider.server_port}",
                     api_key="k", model="m", sleep=lambda _s: None)])
    app.planner = __import__("orchestration.nexus_planner", fromlist=["Planner"]).Planner(app.providers)
    alice = app.auth._open_session("alice")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    yield {"base": f"http://127.0.0.1:{server.port}", "alice": alice, "app": app,
           "gh": gh, "provider": provider}
    server.stop()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    provider.shutdown()
    t.join()
    provider.server_close()
    gh.__exit__(None, None, None)


def _project(harness, name="Lab"):
    _, p = request(harness["base"], "/api/v1/projects", "POST", {"name": name},
                   session=harness["alice"])
    return p["project"]["id"]


REPO = "Pavel1778/python-dispetcher"


def _connect(harness, pid, repository=REPO, **more):
    """Connect a repository the way the studio does: looked up on GitHub, added
    to the allowlist, linked and bound to the project."""
    status, payload = request(harness["base"], f"/api/v1/projects/{pid}/repository", "POST",
                              {"full_name": repository, **more}, session=harness["alice"])
    assert status == 200, payload
    return payload


def _goal(harness, pid, text="Ship it"):
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": text}, session=harness["alice"])
    return goal["goal"]["id"]


def _start(harness, pid, gid, body=None):
    return request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run", "POST",
                   body if body is not None else {}, session=harness["alice"])


def _run_action(harness, pid, run_id, action):
    return request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}/{action}", "POST",
                   {}, session=harness["alice"])


def _detail(harness, pid, run_id):
    _, detail = request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}",
                        session=harness["alice"])
    return detail


def _set_enabled(harness, repository, enabled):
    """Switch an allowlist entry on or off, as an operator would."""
    with harness["app"].store._tx() as cur:
        cur.execute("UPDATE repositories SET enabled=%s WHERE full_name=%s",
                    (enabled, repository))
        assert cur.rowcount == 1


def test_context_refresh_reads_the_repository(harness):
    pid = _project(harness)
    status, payload = request(harness["base"], f"/api/v1/projects/{pid}/context/refresh",
                              "POST", {"repository": "Pavel1778/python-dispetcher"},
                              session=harness["alice"])
    assert status == 201
    assert payload["context"]["revision"] == "abc123"
    assert payload["context"]["summary"]["languages"].get("Python") == 2
    # A second refresh at the same revision is not a new row.
    status2, payload2 = request(harness["base"], f"/api/v1/projects/{pid}/context/refresh",
                                "POST", {"repository": "Pavel1778/python-dispetcher"},
                                session=harness["alice"])
    assert status2 == 200 and payload2["created"] is False
    assert harness["gh"].gh.writes == []


def test_start_run_plans_and_creates_tasks(harness):
    pid = _project(harness)
    _connect(harness, pid)
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": "Add a deterministic feature"}, session=harness["alice"])
    gid = goal["goal"]["id"]
    status, payload = request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run",
                              "POST", {"repository": "Pavel1778/python-dispetcher",
                                       "branch": "main"}, session=harness["alice"])
    assert status == 201
    assert payload["plan"]["provider"] == "groq"
    assert [t["task_key"] for t in payload["tasks"]] == ["audit", "impl"]
    run_id = payload["run"]["id"]

    # The run and its tasks are readable, and the task graph is intact.
    _, detail = request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}",
                        session=harness["alice"])
    assert len(detail["tasks"]) == 2
    assert detail["tasks"][1]["dependencies"] == ["audit"]


def test_full_autonomous_loop_completes_over_http(harness):
    pid = _project(harness)
    _connect(harness, pid)
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": "Ship it"}, session=harness["alice"])
    gid = goal["goal"]["id"]
    _, payload = request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run",
                         "POST", {"repository": "Pavel1778/python-dispetcher"},
                         session=harness["alice"])
    run_id = payload["run"]["id"]

    # Drive the loop with the fake executor/verifier (the worker is separate).
    sched = LabScheduler(harness["app"].lab, _FakeExec(), _FakeVerifier())
    sched.run_until_done(run_id)

    _, detail = request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}",
                        session=harness["alice"])
    assert detail["run"]["state"] == "completed"
    assert all(t["state"] == "completed_verified" for t in detail["tasks"])

    # Notifications were persisted.
    _, notes = request(harness["base"], f"/api/v1/projects/{pid}/notifications",
                       session=harness["alice"])
    kinds = {n["kind"] for n in notes["notifications"]}
    assert "goal_started" in kinds and "goal_completed" in kinds


def test_pause_and_resume_a_run(harness):
    pid = _project(harness)
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": "Pause me"}, session=harness["alice"])
    gid = goal["goal"]["id"]
    _, payload = request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run",
                         "POST", {}, session=harness["alice"])
    run_id = payload["run"]["id"]
    _, paused = request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}/pause",
                        "POST", {}, session=harness["alice"])
    assert paused["run"]["state"] == "paused"
    _, resumed = request(harness["base"], f"/api/v1/projects/{pid}/runs/{run_id}/resume",
                         "POST", {}, session=harness["alice"])
    assert resumed["run"]["state"] == "running"


def test_lab_routes_require_authentication(harness):
    pid = _project(harness)
    assert request(harness["base"], f"/api/v1/projects/{pid}/runs")[0] == 401
    assert request(harness["base"], f"/api/v1/projects/{pid}/context/refresh",
                   "POST", {"repository": "a/b"})[0] == 401


def test_second_start_is_refused_with_409(harness):
    pid = _project(harness)
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": "Once"}, session=harness["alice"])
    gid = goal["goal"]["id"]
    request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run", "POST", {},
            session=harness["alice"])
    status, _ = request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run",
                        "POST", {}, session=harness["alice"])
    assert status == 409


# ------------------------------------------------- which repository a run may use
def _provider_calls(harness):
    return len(harness["provider"].calls)


def test_a_run_is_refused_for_a_repository_the_project_may_not_use(harness):
    """The start is the first of two gates (the scheduler is the second).

    Before this existed the body's ``repository`` and ``branch`` were stored on
    the run tasks unchecked, and the lab started conversations for them without
    the gate every other path goes through: a run for a repository that was in
    nobody's allowlist was accepted and its first task reached the Cloud.
    """
    pid = _project(harness)
    gid = _goal(harness, pid)
    # 1. Not in the allowlist at all.
    status, body = _start(harness, pid, gid, {"repository": "someone-else/private-thing",
                                              "branch": "prod"})
    assert (status, body["error"]) == (
        409, "repository someone-else/private-thing is not in the allowlist")

    # 2. In the allowlist, but another project's: not linked to this one.
    other = _project(harness, "Other")
    _connect(harness, other, "Pavel1778/Klyrshift")
    status, body = _start(harness, pid, gid, {"repository": "Pavel1778/Klyrshift"})
    assert status == 409 and "is not linked to project" in body["error"]

    # 3. Linked, but a branch the allowlist does not permit.
    _connect(harness, pid)
    status, body = _start(harness, pid, gid, {"repository": REPO, "branch": "prod"})
    assert (status, body["error"]) == (409, "branch prod is not allowed")

    # 4. Linked, then switched off.
    _set_enabled(harness, REPO, False)
    status, body = _start(harness, pid, gid, {"repository": REPO})
    assert (status, body["error"]) == (409, f"repository {REPO} is disabled")

    # Nothing was planned (no provider call was spent) and nothing was stored.
    assert _provider_calls(harness) == 0
    _, runs = request(harness["base"], f"/api/v1/projects/{pid}/runs", session=harness["alice"])
    assert runs["runs"] == []

    # 5. Allowed again: the same request goes through.
    _set_enabled(harness, REPO, True)
    status, body = _start(harness, pid, gid, {"repository": REPO, "branch": "main"})
    assert status == 201
    assert {(t["repository"], t["branch"]) for t in body["tasks"]} == {(REPO, "main")}


@pytest.mark.parametrize("body, message", [
    ({"repository": "not a repository"}, "repository must be owner/name"),
    ({"repository": ["a/b"]}, "repository and branch must be strings"),
    ({"max_iterations": 0}, "max_iterations must be a whole number between 1 and 500"),
    ({"max_iterations": 501}, "max_iterations must be a whole number between 1 and 500"),
    ({"max_iterations": "many"}, "max_iterations must be a whole number between 1 and 500"),
    ({"max_iterations": True}, "max_iterations must be a whole number between 1 and 500"),
    ({"max_attempts_per_task": 11}, "max_attempts_per_task must be a whole number between 1 and 10"),
    ({"deadline_seconds": -5}, "deadline_seconds must be a whole number between 1 and 2592000"),
    ({"deadline_seconds": 10 ** 12}, "deadline_seconds must be a whole number between 1 and 2592000"),
])
def test_a_malformed_start_is_a_400_before_anything_is_planned(harness, body, message):
    pid = _project(harness)
    gid = _goal(harness, pid)
    status, payload = _start(harness, pid, gid, body)
    assert (status, payload["error"]) == (400, message)
    assert _provider_calls(harness) == 0


def test_a_second_start_does_not_spend_a_planning_call(harness):
    pid = _project(harness)
    gid = _goal(harness, pid)
    assert _start(harness, pid, gid)[0] == 201
    assert _provider_calls(harness) == 1
    status, payload = _start(harness, pid, gid)
    assert (status, payload["error"]) == (409, "goal already has an active run")
    assert _provider_calls(harness) == 1


def test_the_budgets_asked_for_are_the_budgets_stored(harness):
    pid = _project(harness)
    gid = _goal(harness, pid)
    status, payload = _start(harness, pid, gid, {"max_iterations": 7, "max_attempts_per_task": 1,
                                                 "deadline_seconds": 3600})
    assert status == 201
    run = payload["run"]
    assert (run["max_iterations"], run["max_attempts_per_task"]) == (7, 1)
    assert run["deadline_at"] is not None
    # The per-task limit used to stay at 3 whatever was asked.
    assert [t["max_attempts"] for t in payload["tasks"]] == [1, 1]


# ----------------------------------------------------------- pause, resume, cancel
def test_run_states_follow_the_rules_over_http(harness):
    pid = _project(harness)
    gid = _goal(harness, pid)
    run_id = _start(harness, pid, gid)[1]["run"]["id"]

    assert _run_action(harness, pid, run_id, "pause")[1]["run"]["state"] == "paused"
    assert _run_action(harness, pid, run_id, "pause")[0] == 200            # asking twice is fine
    assert _run_action(harness, pid, run_id, "resume")[1]["run"]["state"] == "running"

    status, payload = _run_action(harness, pid, run_id, "cancel")
    assert (status, payload["run"]["state"]) == (200, "cancelled")
    assert _run_action(harness, pid, run_id, "cancel")[0] == 200

    # A cancelled run stays cancelled. It used to come back as "running".
    for action in ("resume", "pause"):
        status, payload = _run_action(harness, pid, run_id, action)
        assert status == 409, (action, payload)
        assert payload["error"].startswith("a cancelled run cannot become ")
    detail = _detail(harness, pid, run_id)
    assert detail["run"]["state"] == "cancelled"
    # Its tasks no longer claim to be waiting for anything.
    assert {(t["state"], t["last_error"]) for t in detail["tasks"]} == {("blocked", "run cancelled")}

    # The goal is free for a new run.
    assert _start(harness, pid, gid)[0] == 201


def test_a_paused_run_starts_nothing_and_continues_after_resume(harness):
    pid = _project(harness)
    gid = _goal(harness, pid)
    run_id = _start(harness, pid, gid)[1]["run"]["id"]
    executor = _FakeExec()
    started = []
    executor.dispatch = lambda task: started.append(task["task_key"]) or f"conv-{task['task_key']}"
    sched = LabScheduler(harness["app"].lab, executor, _FakeVerifier())

    _run_action(harness, pid, run_id, "pause")
    for _ in range(3):
        sched.tick(run_id)
    assert started == [], "a paused run dispatched a task"
    detail = _detail(harness, pid, run_id)
    assert detail["run"]["state"] == "paused"
    assert [t["state"] for t in detail["tasks"]] == ["pending", "pending"]

    _run_action(harness, pid, run_id, "resume")
    sched.run_until_done(run_id)
    assert started == ["audit", "impl"]
    assert _detail(harness, pid, run_id)["run"]["state"] == "completed"



def test_manual_executor_switch_over_http(harness):
    """The operator can move a running task to another OpenHands account.

    The task must be in flight (409 otherwise), the target must be the
    principal's active account, and a non-member is refused. The switch is
    recorded; the lab worker performs it on the next tick.
    """
    from orchestration.cloud_client import CloudClient
    from tests.fake_cloud import FakeCloudServer

    app = harness["app"]
    pid = _project(harness)
    _, goal = request(harness["base"], f"/api/v1/projects/{pid}/goals", "POST",
                      {"text": "Switch me"}, session=harness["alice"])
    gid = goal["goal"]["id"]
    _, payload = request(harness["base"], f"/api/v1/projects/{pid}/goals/{gid}/run",
                         "POST", {}, session=harness["alice"])
    run_id = payload["run"]["id"]
    task = payload["tasks"][0] if payload.get("tasks") else _detail(
        harness, pid, run_id)["tasks"][0]
    task_id = task["id"]

    with FakeCloudServer() as cloud:
        cloud.cloud.settings = {"llm_api_key_set": True, "active_agent_profile_id": "p",
                                "agent_settings": {"mcp_config": {}},
                                "registered_marketplaces": [], "inherited_marketplaces": []}
        app.openhands_accounts = __import__(
            "orchestration.nexus_openhands_accounts",
            fromlist=["OpenHandsAccountService"]).OpenHandsAccountService(
            app.store, client_factory=lambda u, k: CloudClient(base_url=u, api_key=k,
                                                               timeout=5.0))
        os.environ["NEXUS_SECRET_KEY"] = "test-secret-key"
        account = app.openhands_accounts.add("user:alice", label="B", api_key="sk-b",
                                             base_url=cloud.base_url)

        # A task that is only pending has no executor to switch from: 409.
        status, _ = request(harness["base"],
                            f"/api/v1/projects/{pid}/runs/{run_id}/tasks/{task_id}/switch",
                            "POST", {"account_id": account["id"]}, session=harness["alice"])
        assert status == 409, "a pending task has no executor to switch from"

        # Put the task in flight, then switch it.
        app.lab.claim_task(run_id, "w", lease_seconds=600)
        status, out = request(
            harness["base"],
            f"/api/v1/projects/{pid}/runs/{run_id}/tasks/{task_id}/switch",
            "POST", {"account_id": account["id"]}, session=harness["alice"])
        assert status == 200, out
        assert out["task"]["state"] == "pending"
        assert out["task"]["preferred_account_id"] == account["id"]

        # A malformed account id is a 400, not a crash.
        status, _ = request(
            harness["base"],
            f"/api/v1/projects/{pid}/runs/{run_id}/tasks/{task_id}/switch",
            "POST", {"account_id": "not-a-uuid"}, session=harness["alice"])
        assert status == 400
