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
           "gh": gh}
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
