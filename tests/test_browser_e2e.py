"""Browser end-to-end: the studio, driven the way a person uses it.

Chromium runs against the real HTTP server on a real PostgreSQL. The three
outside services are local socket servers, not mocks: ``tests/fake_cloud.py``
(OpenHands Cloud), ``tests/fake_github.py`` (GitHub) and ``FakeProvider`` (an
OpenAI-compatible LLM endpoint). So what is exercised is the whole path: markup,
stylesheet, script, session and CSRF, routes, store.

What that does not prove: that a live OpenHands account accepts a task or that a
real model answers. Those need real keys and are outside this suite.

The pages are served with ``script-src 'self'``, which also stops Playwright's
``wait_for_function`` (it evaluates a string). Waiting is done on selectors.

Chromium is Playwright's own by default (CI installs it). To use another build,
set ``E2E_CHROMIUM_EXECUTABLE`` and, if it needs private libraries,
``E2E_CHROMIUM_LD_LIBRARY_PATH``. Outside CI the module is skipped when no
browser can be started; in CI a browser that does not start is a failure.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402
from tests.test_providers import FakeProvider  # noqa: E402

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run browser E2E tests")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
USERNAME, PASSWORD = "owner", "browser-pw"
REPO = "acme/storefront"
FILES = {
    "README.md": "# Storefront\n\nДемо-репозиторий.\n\n- пункт один\n- пункт два\n",
    "src/cart.py": "def add(a: int, b: int) -> int:\n    \"\"\"Сложить.\"\"\"\n    return a + b  # 1 < 2\n",
    "src/util/text.py": "NAME = '<b>not markup</b>'\n",
    "web/index.html": "<!doctype html>\n<script>window.pwned = 'file'</script>\n",
}


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


class Stack:
    def __init__(self, base, app, cloud, github, llm):
        self.base, self.app, self.cloud, self.github, self.llm = base, app, cloud, github, llm

    def answers(self, *contents: str) -> None:
        self.llm.script = [{"payload": {"choices": [{"message": {"content": c}}]}} for c in contents]
        self.llm.index = 0
        self.llm.calls.clear()

    def no_provider(self) -> None:
        self.app.providers = ProviderCascade([])
        self.app.chat = ChatResponder(self.app.providers, dispatch_code=self.app._chat_dispatch_code,
                                      start_autonomous=self.app._chat_start_autonomous)


@pytest.fixture
def stack(monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("NEXUS_SECRET_KEY", "browser-tests-secret-key")
    monkeypatch.setenv("AUTH_USERNAME", USERNAME)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "b" * 32)
    for name in ("GITHUB_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    github = FakeGitHub(repos=[
        {"full_name": REPO, "default_branch": "main", "private": True, "archived": False,
         "pushed_at": "2026-01-03T00:00:00Z"},
        {"full_name": "acme/docs", "default_branch": "main", "private": False, "archived": False,
         "pushed_at": "2026-01-02T00:00:00Z"},
    ])
    github.login = "acme"
    directories = sorted({"/".join(p.split("/")[:i]) for p in FILES for i in range(1, p.count("/") + 1)})
    github.tree = ([{"path": d, "type": "tree", "size": None} for d in directories]
                   + [{"path": p, "type": "blob", "size": len(c.encode())} for p, c in FILES.items()])
    github.contents = dict(FILES)

    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    with FakeCloudServer() as cloud, FakeGitHubServer(github) as github_server:
        app = App(DSN, poll_interval=0.2, token="machine-token")
        app.client = CloudClient(cloud.base_url, api_key="test-key", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        app.github = GitHubClient(base_url=github_server.base_url, token="server-github-token")
        app.github_accounts._client_factory = lambda token: GitHubClient(
            base_url=github_server.base_url, token=token)
        app.providers = ProviderCascade([GroqProvider(
            base_url=f"http://127.0.0.1:{llm.server_port}", api_key="k", model="test-model",
            sleep=lambda _seconds: None)])
        app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                                 start_autonomous=app._chat_start_autonomous)
        app.planner = Planner(app.providers)
        server = WebServer(app, "127.0.0.1", 0)
        server.start()
        try:
            yield Stack(f"http://127.0.0.1:{server.port}", app, cloud, github, llm)
        finally:
            server.stop()
            app.store.close()
            llm.shutdown()
            llm.server_close()


@pytest.fixture(scope="module")
def chromium():
    options: dict = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
    if os.environ.get("E2E_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["E2E_CHROMIUM_EXECUTABLE"]
    if os.environ.get("E2E_CHROMIUM_LD_LIBRARY_PATH"):
        # For the browser process only: this path must not leak into the tests.
        options["env"] = {**os.environ, "LD_LIBRARY_PATH": os.environ["E2E_CHROMIUM_LD_LIBRARY_PATH"]}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(**options)
        except PlaywrightError as error:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Chromium could not be started: {str(error).splitlines()[0]}")
        yield browser
        browser.close()


class Watch:
    """Everything the page complained about. A clean run leaves it empty."""

    def __init__(self, page):
        self.problems: list[str] = []
        self.expected: list[re.Pattern] = []
        page.on("pageerror", lambda error: self.problems.append(f"pageerror: {error}"))
        page.on("console", self._console)
        page.on("response", self._response)

    def allow(self, pattern: str) -> None:
        self.expected.append(re.compile(pattern))

    def _add(self, text: str) -> None:
        if not any(pattern.search(text) for pattern in self.expected):
            self.problems.append(text)

    def _console(self, message) -> None:
        if message.type in ("error", "warning"):
            self._add(f"console.{message.type}: {message.text}")

    def _response(self, response) -> None:
        if response.status >= 400:
            self._add(f"HTTP {response.status} {response.request.method} {response.url}")


def _open(chromium, **context):
    context.setdefault("viewport", {"width": 1440, "height": 900})
    context.setdefault("locale", "ru-RU")
    context.setdefault("color_scheme", "dark")
    # Geometry is asserted right after an action; with motion reduced the
    # stylesheet ends every transition at once, so there is nothing to wait out.
    context.setdefault("reduced_motion", "reduce")
    ctx = chromium.new_context(**context)
    ctx.set_default_timeout(15000)
    page = ctx.new_page()
    return ctx, page, Watch(page)


def _login(page, base):
    page.goto(base + "/login")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#loginbtn")
    page.wait_for_url(base + "/")
    page.wait_for_selector('#app[data-ready="true"]')


def _send(page, text):
    page.fill("#prompt", text)
    page.keyboard.press("Enter")


def _overflow(page) -> int:
    """How far the document is wider than the window, in pixels."""
    return page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")


def _connect_repository(page):
    page.click("#repo-chip")
    page.click(f'.picker-item[data-name="{REPO}"]')
    page.wait_for_selector("#repo-branch option:nth-of-type(2)", state="attached")
    page.click("#repo-connect")
    page.wait_for_selector(".dialog-wrap", state="detached")
    expect(page.locator("#repo-chip-text")).to_have_text(f"{REPO} · main")


# ------------------------------------------------------------------- journey
def test_first_visit_ask_and_find_the_chat_again(stack, chromium):
    stack.answers("Привет из **NEXUS**!\n\n```python\nprint('ok')\n```")
    ctx, page, watch = _open(chromium)
    try:
        # Nothing is shown to a visitor without a session.
        page.goto(stack.base + "/")
        assert page.url == stack.base + "/login"
        _login(page, stack.base)
        assert page.inner_text("#whoami") == USERNAME

        # The first screen is the composer, not a dashboard.
        assert page.get_attribute("#app", "data-view") == "home"
        expect(page.locator(".hero h1")).to_have_text("Что будем строить?")
        expect(page.locator("#prompt")).to_be_focused()
        expect(page.locator("#send")).to_be_disabled()
        assert page.locator(".sb-item").count() == 0

        # One message: no project or chat has to be created by hand first.
        _send(page, "Что ты умеешь?\nи как начать")
        page.wait_for_selector("#pending")
        expect(page.locator(".msg.assistant .md strong")).to_have_text("NEXUS")
        assert len(stack.llm.calls) == 1, "the provider was really called, once"
        assert page.get_attribute("#app", "data-view") == "chat"
        expect(page.locator(".msg.user .bubble")).to_have_text("Что ты умеешь?\nи как начать")
        expect(page.locator(".msg.assistant .code-lang")).to_have_text("python")
        expect(page.locator(".msg.assistant .msg-model")).to_have_text("groq · test-model")
        # The chat names itself after the question and appears in the history.
        expect(page.locator("#conv-title")).to_have_text("Что ты умеешь?")
        expect(page.locator(".sb-item.is-active .sb-link")).to_have_text("Что ты умеешь?")
        expect(page.locator("#project-name")).to_have_text("Мой проект")
        assert re.fullmatch(r"#/p/[0-9a-f-]{36}/c/[0-9a-f-]{36}", page.evaluate("() => location.hash"))
        expect(page.locator("#prompt")).to_have_value("")

        # It is stored, not held in the page: a reload brings the same chat back.
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        expect(page.locator(".msg.assistant .md strong")).to_have_text("NEXUS")
        expect(page.locator("#conv-title")).to_have_text("Что ты умеешь?")
        assert len(stack.llm.calls) == 1, "opening a chat does not ask the model again"

        # "New chat" returns to the start screen and keeps the history.
        page.click("#new-chat")
        expect(page.locator(".hero h1")).to_be_visible()
        assert page.locator(".sb-item").count() == 1
        assert page.locator(".sb-item.is-active").count() == 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_connect_a_repository_read_its_files_and_hand_a_task_to_the_agent(stack, chromium):
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        expect(page.locator("#repo-chip-text")).to_have_text("Подключить репозиторий")

        # The dialog lists what GitHub returns, and connecting is one action.
        page.click("#repo-chip")
        expect(page.locator(".picker-item")).to_have_count(2)
        expect(page.locator("#repo-connect")).to_be_disabled()
        page.fill("#repo-query", "store")
        expect(page.locator(".picker-item")).to_have_count(1)
        page.click(f'.picker-item[data-name="{REPO}"]')
        page.wait_for_selector("#repo-branch option:nth-of-type(2)", state="attached")
        assert page.eval_on_selector_all("#repo-branch option", "els => els.map(e => e.value)") == ["main", "dev"]
        page.click("#repo-connect")
        page.wait_for_selector(".dialog-wrap", state="detached")
        expect(page.locator("#repo-chip-text")).to_have_text(f"{REPO} · main")
        expect(page.locator("#project-repo")).to_have_text(f"{REPO} · main")

        # Files: the tree, then a file with its lines numbered and highlighted.
        page.click("#ws-toggle")
        expect(page.locator(".tree-row.dir")).to_have_count(3)          # src, src/util, web
        expect(page.locator(".doc .md h1")).to_have_text("Storefront")  # the README opens by itself
        page.click('.tree-row.file[data-path="src/cart.py"]')
        expect(page.locator(".codeview .cl")).to_have_count(4)
        expect(page.locator(".crumbs")).to_have_text("src/cart.py")
        assert page.inner_text(".codeview .cl >> nth=0").strip() == "def add(a: int, b: int) -> int:"
        assert page.locator(".codeview .t-k").first.inner_text() == "def"
        # A file is text: markup inside it is shown, never run.
        page.click('.tree-row.file[data-path="web/index.html"]')
        expect(page.locator(".codeview .cl")).to_have_count(3)
        assert "<script>window.pwned = 'file'</script>" in page.inner_text(".codeview")
        assert page.evaluate("() => window.pwned") is None
        page.fill("#tree-filter", "text")
        expect(page.locator(".tree-row.file")).to_have_count(1)
        expect(page.locator(".tree-row .tree-path")).to_have_text("src/util")

        # Code mode: the message becomes a real workflow in this project.
        page.click("#mode-btn")
        page.click('.menu-item[data-mode="code"]')
        expect(page.locator("#mode-label")).to_have_text("Код")
        _send(page, "Добавь проверку отрицательного количества")
        page.wait_for_selector(".work-chip")
        expect(page.locator("#tab-agent")).to_have_attribute("aria-selected", "true")
        expect(page.locator(".work .work-kind")).to_have_text("Задача OpenHands")
        expect(page.locator(".work .work-goal")).to_have_text("Добавь проверку отрицательного количества")
        workflows = stack.app.store.list_workflows()
        assert len(workflows) == 1 and workflows[0]["goal"] == "Добавь проверку отрицательного количества"
        assert page.inner_text(".work .work-id") == workflows[0]["id"][:8]
        # The state on screen is the state in the store, not a guess.
        label = page.inner_text(".work .pill")
        assert label in {"в очереди", "выполняется", "проверяется", "заблокировано", "ошибка", "готово"}
        assert watch.problems == []
    finally:
        ctx.close()


def test_an_autonomous_goal_becomes_a_plan_the_agent_tab_shows(stack, chromium):
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        stack.answers(json.dumps({"tasks": [
            {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
             "acceptance": ["сводка"], "dependencies": []},
            {"id": "change", "title": "Внести изменение", "description": "Реализовать.",
             "acceptance": ["тесты"], "dependencies": ["audit"]}]}))
        page.click("#mode-btn")
        page.click('.menu-item[data-mode="autonomous"]')
        _send(page, "Цель: покрыть корзину тестами")
        page.wait_for_selector(".work-chip")
        expect(page.locator(".work .work-kind")).to_have_text("Автономный прогон")
        expect(page.locator(".work .step-title")).to_have_text(["Изучить код", "Внести изменение"])
        expect(page.locator(".work .pill")).to_have_text("выполняется")
        # Pausing is a real state change, read back from the server.
        page.click('.work [data-op="pause"]')
        expect(page.locator(".work .pill")).to_have_text("на паузе")
        expect(page.locator('.work [data-op="resume"]')).to_be_visible()
        assert watch.problems == []
    finally:
        ctx.close()


# ----------------------------------------------------------------- hostile text
def test_text_from_a_model_or_a_person_is_never_markup(stack, chromium):
    hostile = ('<img src=x onerror="window.pwned=1"> <script>window.pwned=2</script>\n\n'
               '[ссылка](javascript:window.pwned=3) и [настоящая](https://example.com/a)\n\n'
               '```html\n</code></pre><script>window.pwned=4</script>\n```')
    stack.answers(hostile)
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        _send(page, '<b onmouseover="window.pwned=5">жирный</b> <script>window.pwned=6</script>')
        page.wait_for_selector(".msg.assistant .md a")
        assert page.evaluate("() => window.pwned") is None
        # Nothing but the renderer's own elements exists inside the messages.
        assert page.locator("#thread script, #thread img, #thread iframe, #thread b").count() == 0
        assert page.locator(".msg.assistant .md a").count() == 1
        link = page.locator(".msg.assistant .md a")
        assert link.get_attribute("href") == "https://example.com/a"
        assert link.get_attribute("rel") == "noopener noreferrer"
        # The reader sees exactly what was written.
        assert '<img src=x onerror="window.pwned=1">' in page.inner_text(".msg.assistant .md")
        assert "<script>window.pwned=4</script>" in page.inner_text(".msg.assistant pre")
        assert page.inner_text(".msg.user .bubble").startswith('<b onmouseover="window.pwned=5">жирный</b>')
        # The same text is the chat's title: in the header and in the history.
        title = page.inner_text("#conv-title")
        assert title.startswith("<b onmouseover=")
        assert page.locator("#sidebar b, #sidebar script, .topbar b").count() == 0
        page.hover(".msg.assistant")
        page.click(".msg.assistant [data-action='copy-message']")
        assert page.evaluate("() => window.pwned") is None
        assert watch.problems == []
    finally:
        ctx.close()


# ------------------------------------------------------------------- failure
def test_a_failed_answer_says_why_and_can_be_retried(stack, chromium):
    stack.no_provider()
    ctx, page, watch = _open(chromium)
    watch.allow(r"HTTP 502 POST .*/messages")
    watch.allow(r"Failed to load resource.*502")
    try:
        _login(page, stack.base)
        # The start screen already says that answers are not available.
        expect(page.locator(".setup")).to_contain_text("не настроен LLM-провайдер")
        _send(page, "Вопрос без провайдера")
        page.wait_for_selector(".msg.error")
        expect(page.locator(".msg.error .err-title")).to_have_text("Ответ не получен")
        expect(page.locator(".msg.error .err-text")).to_contain_text("GEMINI_API_KEY")
        # The question is kept, and the failure is what the server stored.
        expect(page.locator(".msg.user .bubble")).to_have_text("Вопрос без провайдера")
        page.reload()
        page.wait_for_selector(".msg.error")
        expect(page.locator(".sb-item .sb-state.is-error")).to_have_count(1)

        # With a provider back, "retry" sends the same question again.
        stack.app.providers = ProviderCascade([GroqProvider(
            base_url=f"http://127.0.0.1:{stack.llm.server_port}", api_key="k", model="test-model",
            sleep=lambda _seconds: None)])
        stack.app.chat = ChatResponder(stack.app.providers)
        stack.answers("Теперь ответ есть.")
        page.click(".msg.error [data-action='retry']")
        expect(page.locator(".msg.assistant .md")).to_have_text("Теперь ответ есть.")
        assert stack.llm.calls[0]["body"]["messages"][-1]["content"].rstrip().endswith("Вопрос без провайдера")
        assert watch.problems == []
    finally:
        ctx.close()


def test_code_mode_without_a_repository_asks_for_one_instead_of_failing(stack, chromium):
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        page.click("#mode-btn")
        page.click('.menu-item[data-mode="code"]')
        expect(page.locator("#composer-note")).to_contain_text("нужен репозиторий")
        _send(page, "Добавь тест")
        # No request is made that could only fail: the dialog opens instead.
        expect(page.locator(".dialog h2")).to_have_text("Репозиторий проекта")
        expect(page.locator("#prompt")).to_have_value("Добавь тест")
        assert stack.app.store.list_workflows() == []
        assert watch.problems == []
    finally:
        ctx.close()


# ------------------------------------------------------- history and settings
def test_rename_archive_restore_and_search_a_chat(stack, chromium):
    stack.answers("Ответ.")
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        _send(page, "Первый вопрос")
        page.wait_for_selector(".msg.assistant .md")
        page.click("#new-chat")
        _send(page, "Второй вопрос")
        page.wait_for_selector(".msg.assistant .md")
        expect(page.locator(".sb-item")).to_have_count(2)

        # Rename from the header.
        page.click("#conv-title")
        page.fill("#one-input", "Переименованный чат")
        page.keyboard.press("Enter")
        page.wait_for_selector(".dialog-wrap", state="detached")
        expect(page.locator("#conv-title")).to_have_text("Переименованный чат")
        expect(page.locator(".sb-item.is-active .sb-link")).to_have_text("Переименованный чат")

        # Search narrows the history on the server.
        page.fill("#chat-search", "Первый")
        expect(page.locator(".sb-item")).to_have_count(1)
        expect(page.locator(".sb-item .sb-link")).to_have_text("Первый вопрос")
        page.fill("#chat-search", "")
        expect(page.locator(".sb-item")).to_have_count(2)

        # Archive the open chat: it leaves the list and the start screen returns.
        assert page.locator(".sb-more").count() == 0
        page.hover(".sb-item.is-active")
        page.click(".sb-item.is-active [data-action='chat-menu']")
        page.click(".menu-item[data-action='archive-chat']")
        expect(page.locator(".hero h1")).to_be_visible()
        expect(page.locator(".sb-item")).to_have_count(1)
        expect(page.locator(".sb-more")).to_have_text("Архив · 1")
        # ... and it can be brought back.
        page.click(".sb-more")
        page.hover(".sb-item.is-archived")
        page.click(".sb-item.is-archived [data-action='chat-menu']")
        page.click(".menu-item[data-action='archive-chat']")
        expect(page.locator(".sb-item:not(.is-archived)")).to_have_count(2)
        assert page.locator(".sb-more").count() == 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_settings_add_and_remove_a_github_account(stack, chromium):
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        page.click("#user-menu")
        page.click(".menu-item[data-action='open-settings']")
        expect(page.locator(".dialog h2")).to_have_text("Настройки")
        # The server block reports what is configured, without any value.
        status = page.inner_text(".kv.is-status")
        assert "groq" in status and "ключ задан" in status and "токен сервера задан" in status
        assert "server-github-token" not in page.inner_text(".dialog")

        page.fill(".account-form [name=label]", "рабочий")
        page.fill(".account-form [name=token]", "ghp_account_token_for_the_browser_test")
        page.click(".account-form button[type=submit]")
        expect(page.locator(".card .card-main b")).to_have_text("рабочий")
        expect(page.locator(".card .card-main small").first).to_have_text("acme")
        expect(page.locator(".card .pill")).to_have_text("активен")
        # The token went to the server and is nowhere in the page afterwards.
        assert "ghp_account_token_for_the_browser_test" not in page.content()
        assert page.input_value(".account-form [name=token]") == ""
        assert stack.github.auth_headers[-1].endswith("ghp_account_token_for_the_browser_test")

        # The new account is offered as the identity for a repository.
        page.keyboard.press("Escape")
        page.click("#repo-chip")
        page.wait_for_selector(".picker-item")
        options = page.eval_on_selector_all("#repo-source option", "els => els.map(e => e.textContent)")
        assert options == ["рабочий — acme", "Токен сервера (GITHUB_TOKEN)"]
        page.keyboard.press("Escape")

        page.click("#user-menu")
        page.click(".menu-item[data-action='open-settings']")
        page.click(".card [data-op='revoke']")
        page.click("#confirm-ok")
        expect(page.locator(".card .pill")).to_have_text("отключён")
        assert page.locator(".card [data-op='revoke']").count() == 0

        # The project tab renames the project.
        page.click(".settings-nav [data-tab='project']")
        assert page.locator("#project-rename").count() == 0, "no project exists yet"
        expect(page.locator(".settings-pane .empty h3")).to_have_text("Проекта ещё нет")
        assert watch.problems == []
    finally:
        ctx.close()


def test_theme_keyboard_and_logout(stack, chromium):
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        dark = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        assert page.get_attribute("html", "data-theme") == "dark"   # follows the system here

        page.click("#user-menu")
        page.click("[data-action='theme'][data-theme='light']")
        assert page.get_attribute("html", "data-theme") == "light"
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") != dark
        # Escape closes the menu and gives the focus back to what opened it.
        page.keyboard.press("Escape")
        assert page.locator(".menu").count() == 0
        expect(page.locator("#user-menu")).to_be_focused()
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        assert page.get_attribute("html", "data-theme") == "light", "the choice is remembered"

        # Shift+Enter is a new line; nothing is sent.
        page.click("#prompt")
        page.keyboard.type("первая")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("вторая")
        assert page.input_value("#prompt") == "первая\nвторая"
        assert page.get_attribute("#app", "data-view") == "home"
        assert stack.llm.calls == []

        # A dialog keeps the focus inside and closes on Escape.
        page.click("#repo-chip")
        page.wait_for_selector(".dialog")
        for _ in range(30):
            page.keyboard.press("Tab")
            assert page.evaluate("() => !!document.activeElement.closest('.dialog')")
        page.keyboard.press("Escape")
        assert page.locator(".dialog").count() == 0

        # Logging out ends the session on the server, not only in the page.
        page.click("#user-menu")
        page.click("#logout")
        page.wait_for_url(stack.base + "/login")
        page.goto(stack.base + "/")
        assert page.url == stack.base + "/login"
        watch.allow(r"401")
        assert page.request.get(stack.base + "/api/v1/projects").status == 401
        assert watch.problems == []
    finally:
        ctx.close()


# ---------------------------------------------------------------- responsive
def test_a_phone_gets_one_column_and_nothing_wider_than_the_screen(stack, chromium):
    stack.answers("Ответ с длинной строкой кода:\n\n```python\n" + "x = " + "1 + " * 60 + "1\n```\n\n"
                  "| колонка | ещё колонка | и ещё одна | четвёртая |\n|---|---|---|---|\n| a | b | c | d |")
    ctx, page, watch = _open(chromium, viewport={"width": 390, "height": 844}, is_mobile=True,
                             has_touch=True, device_scale_factor=2)
    try:
        _login(page, stack.base)
        assert _overflow(page) <= 0
        # The history is a drawer: off the screen until asked for.
        assert page.get_attribute("#app", "data-sidebar") == "closed"
        assert page.locator("#sidebar").bounding_box()["x"] < -200
        # The composer is on screen without scrolling, and usable by touch.
        box = page.locator("#composer").bounding_box()
        assert box["y"] > 0 and box["y"] + box["height"] <= 844
        for selector in ("#send", "#open-sidebar", "#ws-toggle", "#mode-btn", "#repo-chip"):
            size = page.locator(selector).bounding_box()
            assert size["height"] >= 30 and size["width"] >= 30, selector

        # On a touch keyboard Enter is a new line; the button sends.
        page.tap("#prompt")
        page.keyboard.type("Вопрос с телефона")
        page.keyboard.press("Enter")
        assert page.input_value("#prompt") == "Вопрос с телефона\n"
        page.tap("#send")
        page.wait_for_selector(".msg.assistant .md table")
        assert _overflow(page) <= 0, "a long code line or a table scrolls inside its own box"
        assert page.locator(".msg.assistant").bounding_box()["width"] <= 390

        page.tap("#open-sidebar")
        expect(page.locator("#scrim")).to_be_visible()
        assert page.locator("#sidebar").bounding_box()["x"] == 0
        # Without hover the per-chat menu has to be visible to be reachable.
        assert page.evaluate("() => getComputedStyle(document.querySelector('.sb-item .icon-btn')).opacity") == "1"
        assert _overflow(page) <= 0
        page.tap(".sb-link")
        assert page.get_attribute("#app", "data-sidebar") == "closed"

        # The workspace takes the whole screen and shows one pane at a time.
        _connect_repository(page)
        page.tap("#ws-toggle")
        page.wait_for_selector(".tree-row")
        box = page.locator("#workspace").bounding_box()
        assert (box["x"], box["width"]) == (0, 390)
        assert page.locator(".file-pane").is_hidden()
        page.tap('.tree-row.file[data-path="src/cart.py"]')
        page.wait_for_selector(".codeview .cl")
        assert page.locator(".tree-pane").is_hidden()
        assert _overflow(page) <= 0
        page.tap("[data-action='files-back']")
        expect(page.locator(".tree-pane")).to_be_visible()
        page.tap("#workspace [data-action='toggle-workspace']")
        assert page.locator("#workspace").is_hidden()
        assert watch.problems == []
    finally:
        ctx.close()


@pytest.mark.parametrize("width, height", [(360, 640), (768, 1024), (1024, 768), (1280, 800), (1920, 1080)])
def test_every_width_fits_with_the_workspace_open(stack, chromium, width, height):
    stack.answers("Ответ.\n\n```\n" + "длинная строка " * 40 + "\n```")
    ctx, page, watch = _open(chromium, viewport={"width": width, "height": height})
    try:
        _login(page, stack.base)
        _connect_repository(page)
        _send(page, "Вопрос")
        page.wait_for_selector(".msg.assistant .md")
        assert _overflow(page) <= 0
        page.click("#ws-toggle")
        page.wait_for_selector(".tree-row")
        assert _overflow(page) <= 0
        workspace = page.locator("#workspace").bounding_box()
        assert workspace["x"] >= 0 and workspace["x"] + workspace["width"] <= width
        if width > 1180:
            # Side by side: the chat keeps a usable column next to the workspace.
            main = page.locator("#main").bounding_box()
            assert main["width"] >= 380
            assert abs((main["x"] + main["width"]) - workspace["x"]) <= 1
        else:
            # Not enough room for both: the workspace lies over the chat.
            assert page.locator("#main").bounding_box()["width"] >= min(width, 360)
        # The last message is still in view after the layout changed.
        last = page.locator(".msg").last.bounding_box()
        if width > 1180:
            assert last["y"] < height
        page.keyboard.press("Escape") if width <= 1180 else page.click("#workspace [data-action='toggle-workspace']")
        assert page.locator("#workspace").is_hidden()
        assert watch.problems == []
    finally:
        ctx.close()
