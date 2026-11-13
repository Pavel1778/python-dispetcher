"""The server behaviour the studio relies on, over real HTTP.

The server is the real :class:`orchestration.web_server.WebServer` on a real
PostgreSQL. The three outside services are local socket servers, not mocks:
``tests/fake_cloud.py`` (OpenHands Cloud), ``tests/fake_github.py`` (GitHub, which
records the ``Authorization`` header of every request, so a test can prove
*which identity* a read used) and ``FakeProvider`` (an OpenAI-compatible LLM
endpoint that records every request body).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_direct import CloudDirectClient  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer, title_from_message  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402
from tests.test_providers import FakeProvider  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run studio API tests")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "studio-machine-token"
SERVER_GITHUB_TOKEN = "server-github-token-value"
ACCOUNT_TOKEN = "account-github-token-value"
REPO = "Pavel1778/python-dispetcher"      # in the fake's default repository list
NIL = "00000000-0000-4000-8000-000000000000"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class Api:
    """JSON over HTTP with the machine token; returns ``(status, body)``."""

    def __init__(self, base: str, token: str | None = TOKEN):
        self.base, self.token = base, token

    def __call__(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            return error.code, (json.loads(raw) if raw else None)

    def get(self, path):
        return self("GET", path)

    def post(self, path, body=None):
        return self("POST", path, {} if body is None else body)

    def patch(self, path, body):
        return self("PATCH", path, body)


class Studio:
    """Everything a test needs: the HTTP client and the fakes.

    ``sandbox`` is the state of the stand-in agent-server (``tests/fake_runtime``)
    and is only spoken to on the direct transport.
    """

    def __init__(self, api, app, cloud, github, llm, sandbox=None):
        self.api, self.app, self.cloud, self.github, self.llm = api, app, cloud, github, llm
        self.sandbox = sandbox
        #: The running HTTP server, and every server this stack has started.
        self.server = None
        self.servers: list = []
        self._boot = None

    def boot(self, **app_options):
        """Start one more server process on the same database and the same fakes.

        ``api``, ``app`` and ``server`` then name the new one; the old one keeps
        running until the caller stops it. This is how a test restarts the
        service: nothing is carried over but what is in PostgreSQL.
        """
        self.app, self.server = self._boot(**app_options)
        self.servers.append(self.server)
        self.api = Api(f"http://127.0.0.1:{self.server.port}")
        return self.app

    def project(self, name: str = "Studio") -> str:
        status, body = self.api.post("/api/v1/projects", {"name": name})
        assert status == 201, body
        return body["project"]["id"]

    def account(self, label: str = "personal", token: str = ACCOUNT_TOKEN) -> str:
        status, body = self.api.post("/api/github/accounts", {"label": label, "token": token})
        assert status == 201, body
        return body["account"]["id"]

    def conversation(self, project_id: str, *, title: str = "", mode: str = "ask") -> str:
        status, body = self.api.post(f"/api/v1/projects/{project_id}/conversations",
                                     {"title": title, "mode": mode})
        assert status == 201, body
        return body["conversation"]["id"]

    def answers(self, *contents: str) -> None:
        """Script the LLM: one reply per call, the last one repeating."""
        self.llm.script = [{"payload": {"choices": [{"message": {"content": c}}]}} for c in contents]
        self.llm.index = 0
        self.llm.calls.clear()

    def last_identity(self) -> str:
        """The token GitHub saw on the most recent request."""
        return (self.github.auth_headers[-1] or "").split()[-1]


@contextlib.contextmanager
def studio_stack(monkeypatch, *, transport: str = "app", **app_options):
    """The real server on the fakes. *app_options* go to ``App``.

    *transport* is how the server reaches OpenHands: ``app`` is the
    app-conversation API of the fake Cloud; ``direct`` provisions a sandbox
    there and then talks to its agent-server, a second socket server
    (``tests/fake_runtime.py``) - the transport a deployment uses by default.
    """
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "cloud-key-value")
    monkeypatch.setenv("NEXUS_SECRET_KEY", "studio-tests-secret-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("owner-password"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    for name in ("GITHUB_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    github = FakeGitHub()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    with (FakeCloudServer() as cloud, FakeGitHubServer(github) as github_server,
          FakeServer() as sandbox):
        def boot(**options):
            app = App(DSN, poll_interval=0.2, token=TOKEN, **{**app_options, **options})
            app.client = CloudClient(cloud.base_url, api_key="cloud-key-value", timeout=10.0)
            if transport == "direct":
                cloud.cloud.agent_server_url = sandbox.base_url
                cloud.cloud.sandbox_session_key = sandbox.runtime.expected_key
                app.client = CloudDirectClient(app.client, app.store, wait_interval=0.01,
                                               timeout=10.0)
            app.dispatcher.client = app.client
            app.runtime.client = app.client
            app.verifier.client = app.client
            app.github = GitHubClient(base_url=github_server.base_url,
                                      token=SERVER_GITHUB_TOKEN)
            app.github_accounts._client_factory = lambda token: GitHubClient(
                base_url=github_server.base_url, token=token)
            app.providers = ProviderCascade([GroqProvider(
                base_url=f"http://127.0.0.1:{llm.server_port}", api_key="llm-key-value",
                model="test-model", sleep=lambda _seconds: None)])
            app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                                     start_autonomous=app._chat_start_autonomous)
            app.planner = Planner(app.providers)
            server = WebServer(app, "127.0.0.1", 0)
            server.start()
            return app, server

        stack = Studio(None, None, cloud, github, llm, sandbox.runtime)
        stack._boot = boot
        stack.boot()
        try:
            yield stack
        finally:
            for server in stack.servers:
                try:
                    server.stop()
                except Exception:  # noqa: BLE001 - a server a test already stopped
                    pass
                server.app.store.close()
            llm.shutdown()
            llm.server_close()


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


# ------------------------------------------------------------------ status
def test_status_says_what_is_configured_and_never_the_values(studio, monkeypatch):
    before = list(studio.cloud.cloud.calls)
    status, body = studio.api.get("/api/studio")
    assert status == 200
    assert body["providers"] == ["groq"]
    assert body["openhands"] == {"server_key": True, "transport": "direct"}
    assert body["github"] == {"server_token": True}
    assert body["accounts"] == {"encryption_key": True}
    # This server does not advance autonomous runs unless it is told to. The
    # OpenHands conversations of a finished run are deleted a day later.
    assert body["lab"] == {"inline": False, "cleanup_hours": 24.0}
    assert isinstance(body["version"], str) and body["version"]
    # The page asks on every load, so the answer must not depend on OpenHands
    # being reachable: no request reaches the Cloud API.
    assert studio.cloud.cloud.calls == before
    text = json.dumps(body)
    for value in ("cloud-key-value", SERVER_GITHUB_TOKEN, "llm-key-value",
                  "studio-tests-secret-key", TOKEN):
        assert value not in text

    monkeypatch.delenv("OPENHANDS_API_KEY")
    monkeypatch.delenv("NEXUS_SECRET_KEY")
    studio.app.providers = ProviderCascade([])
    studio.app.github = GitHubClient(base_url="http://127.0.0.1:9", token=None)
    status, body = studio.api.get("/api/studio")
    assert status == 200
    assert body["providers"] == []
    assert body["openhands"]["server_key"] is False
    assert body["github"]["server_token"] is False
    assert body["accounts"]["encryption_key"] is False


def test_status_requires_a_credential(studio):
    status, _ = Api(studio.api.base, token=None).get("/api/studio")
    assert status == 401


# ------------------------------------------------------- connect a repository
def test_connecting_checks_github_then_allowlists_and_binds(studio):
    project = studio.project()
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})
    assert status == 200, body
    binding = body["binding"]
    assert binding["full_name"] == REPO
    assert binding["selected_branch"] == "main"
    assert binding["github_account_id"] is None
    # The repository was looked up on GitHub before anything was written.
    assert f"/repos/{REPO}" in studio.github.paths
    _, listed = studio.api.get("/api/repositories")
    entry = next(r for r in listed["repositories"] if r["full_name"] == REPO)
    assert entry["enabled"] is True and entry["default_branch"] == "main"
    assert studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]["full_name"] == REPO

    # Choosing another branch extends the allowlist entry instead of replacing it.
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": REPO, "branch": "dev"})
    assert status == 200, body
    assert body["binding"]["selected_branch"] == "dev"
    _, listed = studio.api.get("/api/repositories")
    entry = next(r for r in listed["repositories"] if r["full_name"] == REPO)
    assert set(entry["allowed_branches"]) == {"main", "dev"}
    assert len([r for r in listed["repositories"] if r["full_name"] == REPO]) == 1


def test_connecting_refuses_what_github_does_not_have_and_writes_nothing(studio):
    project = studio.project()
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": "Pavel1778/no-such-repository"})
    assert status == 400
    assert "was not found" in body["error"]
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": REPO, "branch": "no-such-branch"})
    assert status == 400
    assert body["error"] == f"branch no-such-branch does not exist in {REPO}"
    # Neither attempt left a trace: no allowlist entry and no binding.
    assert studio.api.get("/api/repositories")[1]["repositories"] == []
    assert studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]["full_name"] is None


def test_connecting_reports_a_github_outage_as_one(studio):
    project = studio.project()
    studio.github.force_status = 500
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})
    assert status in (502, 503), body
    assert "github" in body["error"]
    assert studio.api.get("/api/repositories")[1]["repositories"] == []


@pytest.mark.parametrize("body, expected", [
    ({"full_name": "not a repository"}, "full_name must be owner/name"),
    ({"full_name": ""}, "full_name must be owner/name"),
    ({}, "full_name must be owner/name"),
    ({"full_name": REPO, "branch": 7}, "branch must be a string"),
    ({"full_name": REPO, "account_id": "nope"}, "account_id must be a uuid or null"),
    ({"full_name": REPO, "account_id": 5}, "account_id must be a uuid or null"),
])
def test_connecting_validates_its_input(studio, body, expected):
    project = studio.project()
    status, answer = studio.api.post(f"/api/v1/projects/{project}/repository", body)
    assert status == 400
    assert answer["error"] == expected
    assert studio.github.paths == [], "nothing is asked of GitHub for a malformed request"


def test_connecting_needs_a_project_and_an_account_that_exist(studio):
    project = studio.project()
    status, _ = studio.api.post(f"/api/v1/projects/{NIL}/repository", {"full_name": REPO})
    assert status == 404
    status, _ = studio.api.post(f"/api/v1/projects/{project}/repository",
                                {"full_name": REPO, "account_id": NIL})
    assert status == 404
    assert studio.api.get("/api/repositories")[1]["repositories"] == []


def test_the_identity_is_the_callers_choice_and_becomes_the_projects(studio):
    """An account, the server token, or "whatever it is now": each is explicit."""
    project = studio.project()
    account = studio.account()
    base = f"/api/v1/projects/{project}"

    # 1. A stored account: checked with its token, then bound to the project.
    studio.github.auth_headers.clear()
    status, body = studio.api.post(f"{base}/repository", {"full_name": REPO, "account_id": account})
    assert status == 200, body
    assert body["binding"]["github_account_id"] == account
    assert {h.split()[-1] for h in studio.github.auth_headers} == {ACCOUNT_TOKEN}
    # ... and every later read of the project goes out as that account.
    for path in (f"{base}/github/repositories/{REPO}/tree?ref=main",
                 f"{base}/github/repositories/{REPO}/branches",
                 f"{base}/github/repositories/{REPO}/contents/README.md?ref=main"):
        studio.github.auth_headers.clear()
        assert studio.api.get(path)[0] in (200, 404)
        assert studio.last_identity() == ACCOUNT_TOKEN, path

    # 2. Leaving the key out keeps the identity the project has.
    studio.github.auth_headers.clear()
    status, body = studio.api.post(f"{base}/repository", {"full_name": REPO, "branch": "dev"})
    assert status == 200, body
    assert body["binding"]["github_account_id"] == account
    assert {h.split()[-1] for h in studio.github.auth_headers} == {ACCOUNT_TOKEN}

    # 3. An explicit null is the server's token, and the account is unbound, so
    #    the project does not keep reading as someone it was told to stop using.
    studio.github.auth_headers.clear()
    status, body = studio.api.post(f"{base}/repository", {"full_name": REPO, "account_id": None})
    assert status == 200, body
    assert body["binding"]["github_account_id"] is None
    assert {h.split()[-1] for h in studio.github.auth_headers} == {SERVER_GITHUB_TOKEN}
    studio.github.auth_headers.clear()
    assert studio.api.get(f"{base}/github/repositories/{REPO}/tree?ref=main")[0] == 200
    assert studio.last_identity() == SERVER_GITHUB_TOKEN


def test_a_revoked_account_is_an_error_not_a_quiet_switch_to_the_server_token(studio):
    project = studio.project()
    account = studio.account()
    base = f"/api/v1/projects/{project}"
    assert studio.api.post(f"{base}/repository", {"full_name": REPO, "account_id": account})[0] == 200
    assert studio.api.post(f"/api/github/accounts/{account}/revoke")[0] == 200

    studio.github.auth_headers.clear()
    status, body = studio.api.get(f"{base}/github/repositories/{REPO}/tree?ref=main")
    assert status == 400
    assert body["error"] == "github account: account is revoked"
    assert studio.github.auth_headers == [], "no request went out under another identity"


def test_a_large_file_is_cut_between_characters_and_says_so(studio):
    """200 000 bytes is the limit; the cut must not split a character.

    Cyrillic is two bytes a letter, and the one-byte prefix puts the limit in the
    middle of one. Decoding that slice strictly failed, and the file was then
    reported as "not found or binary".
    """
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})[0] == 200
    base = f"/api/v1/projects/{project}/github/repositories/{REPO}/contents"
    studio.github.contents["big.txt"] = "a" + "я" * 150_000          # 300 001 bytes
    studio.github.contents["small.txt"] = "короткий файл\n"

    status, body = studio.api.get(f"{base}/big.txt?ref=main")
    assert status == 200, body
    assert body["truncated"] is True
    assert body["content"] == "a" + "я" * 99_999                      # 199 999 bytes: whole letters only
    status, body = studio.api.get(f"{base}/small.txt?ref=main")
    assert status == 200
    assert body == {"path": "small.txt", "content": "короткий файл\n", "ref": "main", "truncated": False}


# ------------------------------------------------- what the dialog lists from
def test_an_account_lists_its_repositories_and_branches_with_its_own_token(studio):
    account = studio.account()

    status, body = studio.api.get(f"/api/github/accounts/{account}")
    assert status == 200
    assert body["account"]["id"] == account and body["account"]["login"] == "Pavel1778"

    studio.github.auth_headers.clear()
    status, body = studio.api.get(f"/api/github/accounts/{account}/repositories")
    assert status == 200
    assert REPO in [r["full_name"] for r in body["repositories"]]
    assert studio.last_identity() == ACCOUNT_TOKEN

    studio.github.auth_headers.clear()
    status, body = studio.api.get(f"/api/github/accounts/{account}/repositories/{REPO}/branches")
    assert status == 200
    assert [b["name"] for b in body["branches"]] == ["main", "dev"]
    assert studio.last_identity() == ACCOUNT_TOKEN

    # The stored token is never part of an answer.
    for path in (f"/api/github/accounts/{account}", "/api/github/accounts",
                 f"/api/github/accounts/{account}/repositories"):
        assert ACCOUNT_TOKEN not in json.dumps(studio.api.get(path)[1])


def test_the_server_token_lists_branches_too(studio):
    studio.github.auth_headers.clear()
    status, body = studio.api.get(f"/api/github/repositories/{REPO}/branches")
    assert status == 200
    assert [b["name"] for b in body["branches"]] == ["main", "dev"]
    assert studio.last_identity() == SERVER_GITHUB_TOKEN


@pytest.mark.parametrize("path", [
    "/api/github/accounts/not-a-uuid",
    "/api/github/accounts/not-a-uuid/repositories",
    f"/api/github/accounts/{NIL}",
    f"/api/github/accounts/{NIL}/repositories",
    f"/api/github/accounts/{NIL}/repositories/{REPO}/branches",
    f"/api/github/accounts/{NIL}/repositories/{REPO}/tree",
    f"/api/github/accounts/{NIL}/unknown",
    "/api/openhands/accounts/not-a-uuid",
    f"/api/openhands/accounts/{NIL}",
])
def test_account_routes_answer_404_for_what_is_not_there(studio, path):
    status, body = studio.api.get(path)
    assert status == 404, body
    assert studio.github.paths == []


# ---------------------------------------------------------------- the chat
def test_title_from_message():
    assert title_from_message("Короткий вопрос") == "Короткий вопрос"
    assert title_from_message("  первая   строка \n вторая строка") == "первая строка"
    assert title_from_message("") == ""
    assert title_from_message("   \n  ") == ""
    long = "Объясни пожалуйста как именно устроен модуль диспетчера и почему он перезапускает задачи"
    title = title_from_message(long)
    assert title.endswith("…") and len(title) <= 61
    # Cut between words: what is left is a prefix that ends on a whole word.
    assert long.startswith(title[:-1]) and long[len(title) - 1] == " "
    # One unbroken run of characters has no word boundary to cut at.
    assert title_from_message("x" * 200) == "x" * 60 + "…"
    assert title_from_message("x" * 60) == "x" * 60


def test_a_conversation_is_named_after_its_first_message_only(studio):
    project = studio.project()
    conversation = studio.conversation(project)
    base = f"/api/v1/projects/{project}/conversations/{conversation}"
    studio.answers("Первый ответ.", "Второй ответ.")

    status, body = studio.api.post(f"{base}/messages", {"text": "Как устроен диспетчер?\nподробно"})
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert studio.api.get(base)[1]["conversation"]["title"] == "Как устроен диспетчер?"

    assert studio.api.post(f"{base}/messages", {"text": "А теперь другой вопрос"})[0] == 200
    assert studio.api.get(base)[1]["conversation"]["title"] == "Как устроен диспетчер?"

    # A title the user chose is never replaced.
    chosen = studio.conversation(project, title="Моё название")
    chosen_base = f"/api/v1/projects/{project}/conversations/{chosen}"
    assert studio.api.post(f"{chosen_base}/messages", {"text": "Вопрос"})[0] == 200
    assert studio.api.get(chosen_base)[1]["conversation"]["title"] == "Моё название"


def test_an_answer_is_requested_as_prose_and_a_plan_as_json(studio):
    """A chat answer must not be forced into JSON; a plan must be."""
    project = studio.project()
    conversation = studio.conversation(project)
    base = f"/api/v1/projects/{project}/conversations/{conversation}"

    studio.answers("Обычный **текст**, не JSON.")
    status, body = studio.api.post(f"{base}/messages", {"text": "Вопрос", "mode": "ask"})
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Обычный **текст**, не JSON."
    assert body["messages"][-1]["meta"]["provider"] == "groq"
    assert len(studio.llm.calls) == 1
    assert "response_format" not in studio.llm.calls[0]["body"]

    studio.answers(json.dumps({"tasks": [
        {"id": "a", "title": "Шаг", "description": "Сделать шаг.", "acceptance": ["готово"],
         "dependencies": []}]}))
    status, body = studio.api.post(f"{base}/messages", {"text": "Спланируй", "mode": "plan"})
    assert status == 200, body
    assert studio.llm.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_a_failed_answer_is_stored_as_a_failure_and_the_question_is_kept(studio):
    project = studio.project()
    conversation = studio.conversation(project)
    base = f"/api/v1/projects/{project}/conversations/{conversation}"
    studio.app.providers = ProviderCascade([])
    studio.app.chat = ChatResponder(studio.app.providers)

    status, body = studio.api.post(f"{base}/messages", {"text": "Вопрос без провайдера"})
    assert status == 502
    assert [m["role"] for m in body["messages"]] == ["user", "system"]
    assert body["messages"][1]["meta"] == {"error": True}
    stored = studio.api.get(f"{base}/messages")[1]["messages"]
    assert [m["content"] for m in stored][0] == "Вопрос без провайдера"
    assert stored[1]["meta"] == {"error": True}
    assert studio.api.get(base)[1]["conversation"]["state"] == "error"


def test_a_code_turn_starts_its_workflow_inside_the_project(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})[0] == 200
    conversation = studio.conversation(project, mode="code")
    base = f"/api/v1/projects/{project}/conversations/{conversation}"

    status, body = studio.api.post(f"{base}/messages", {"text": "Добавь тест", "mode": "code"})
    assert status == 200, body
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "tool", "assistant"]
    workflow = body["messages"][2]["meta"]["workflow_id"]
    assert body["messages"][1]["meta"]["workflow_id"] == workflow
    assert REPO in body["messages"][2]["content"] and "«Агент»" in body["messages"][2]["content"]

    # The workflow belongs to this project: its project-scoped view answers.
    # (Created outside any project, it would be a 404 here.)
    status, detail = studio.api.get(f"/api/v1/projects/{project}/workflows/{workflow}")
    assert status == 200, detail
    assert detail["workflow"]["id"] == workflow
    assert detail["workflow"]["goal"] == "Добавь тест"
    other = studio.project("Other")
    assert studio.api.get(f"/api/v1/projects/{other}/workflows/{workflow}")[0] == 404


def test_a_code_turn_without_a_repository_fails_before_anything_starts(studio):
    project = studio.project()
    conversation = studio.conversation(project, mode="code")
    base = f"/api/v1/projects/{project}/conversations/{conversation}"
    before = list(studio.cloud.cloud.calls)
    status, body = studio.api.post(f"{base}/messages", {"text": "Добавь тест", "mode": "code"})
    assert status == 502
    assert "bind a repository to the project" in body["error"]
    assert studio.cloud.cloud.calls == before
    assert studio.api.get("/api/workflows")[1]["workflows"] == []


def test_an_autonomous_turn_plans_and_starts_a_run_for_the_asker(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})[0] == 200
    conversation = studio.conversation(project, mode="autonomous")
    base = f"/api/v1/projects/{project}/conversations/{conversation}"
    studio.answers(json.dumps({"tasks": [
        {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
         "acceptance": ["сводка"], "dependencies": []},
        {"id": "change", "title": "Внести изменение", "description": "Реализовать.",
         "acceptance": ["тесты"], "dependencies": ["audit"]}]}))

    status, body = studio.api.post(f"{base}/messages", {"text": "Цель: покрыть модуль тестами",
                                                        "mode": "autonomous"})
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "tool", "assistant"]
    run = body["messages"][2]["meta"]["run_id"]
    assert "задач в плане: 2" in body["messages"][2]["content"]

    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    assert detail["run"]["state"] == "running"
    assert sorted(t["title"] for t in detail["tasks"]) == ["Внести изменение", "Изучить код"]
    # The conversation remembers the run it started.
    assert studio.api.get(base)[1]["conversation"]["run_id"] == run


def test_an_autonomous_turn_is_refused_once_the_repository_is_switched_off(studio):
    """The repository was fine when it was connected; that is not permission for ever.

    The chat passes the project's bound repository to the lab, and the lab used
    to start work on it without asking again. Now the turn is refused before a
    plan is requested, and the reason is what the person reads.
    """
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})[0] == 200
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE repositories SET enabled=false WHERE full_name=%s", (REPO,))
    conversation = studio.conversation(project, mode="autonomous")
    base = f"/api/v1/projects/{project}/conversations/{conversation}"
    studio.answers(json.dumps({"tasks": [{"id": "a", "title": "A", "acceptance": [],
                                          "dependencies": []}]}))

    status, body = studio.api.post(f"{base}/messages", {"text": "Цель: навести порядок",
                                                        "mode": "autonomous"})
    assert status == 502, body
    assert body["error"] == (
        f"could not start the autonomous run: repository {REPO} is disabled")
    assert [m["role"] for m in body["messages"]] == ["user", "system"]
    assert studio.llm.calls == [], "a plan was requested for work that may not run"
    assert studio.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"] == []
    assert studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"] == []


def test_archiving_hides_a_conversation_and_restoring_brings_it_back(studio):
    project = studio.project()
    kept = studio.conversation(project, title="Остаётся")
    gone = studio.conversation(project, title="В архив")
    base = f"/api/v1/projects/{project}/conversations"

    def titles(query: str = "") -> list[str]:
        return sorted(c["title"] for c in studio.api.get(base + query)[1]["conversations"])

    status, body = studio.api.patch(f"{base}/{gone}", {"archived": True})
    assert status == 200 and body["conversation"]["archived"] is True
    assert titles() == ["Остаётся"]
    assert titles("?archived=1") == ["В архив", "Остаётся"]
    # Its messages are still readable: an archive is not a deletion.
    assert studio.api.get(f"{base}/{gone}/messages")[0] == 200

    status, body = studio.api.patch(f"{base}/{gone}", {"archived": False})
    assert status == 200 and body["conversation"]["archived"] is False
    assert titles() == ["В архив", "Остаётся"]

    for bad in ("yes", 1, None):
        status, body = studio.api.patch(f"{base}/{gone}", {"archived": bad})
        assert status == 400, bad
    assert studio.api.patch(f"{base}/{NIL}", {"archived": True})[0] == 404
    assert studio.api.patch(f"{base}/{kept}", {"title": "Новое имя"})[1]["conversation"]["title"] == "Новое имя"
