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

import contextlib
import json
import os
import re
import sys
import threading
import time
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

# One budget for every wait. A shared CI runner can be several times slower than
# a laptop; a short default would then fail on speed, not on behaviour.
TIMEOUT_MS = 20_000
expect.set_options(timeout=TIMEOUT_MS)

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


@contextlib.contextmanager
def _stack(monkeypatch, **app_options):
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
        app = App(DSN, poll_interval=0.2, token="machine-token", **app_options)
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


@pytest.fixture
def stack(monkeypatch):
    with _stack(monkeypatch) as built:
        yield built


@pytest.fixture
def lab_stack(monkeypatch):
    """The same stack with the server advancing autonomous runs itself, as a
    deployment does: what the page shows then is what the loop really did."""
    with _stack(monkeypatch, lab_inline=True) as built:
        yield built


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
    """What went wrong in the page: uncaught exceptions, console errors (a
    Content-Security-Policy violation is one) and responses of 400 and above.
    A clean run leaves ``problems`` empty; an expected failure is allowed by
    pattern. Console warnings are the browser's own advice and are not counted.
    """

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
        if message.type == "error":
            self._add(f"console.error: {message.text}")

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
    ctx.set_default_timeout(TIMEOUT_MS)
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


def _send_tap(page, text):
    """Send on a touch device, where Enter is a newline and the button sends."""
    page.fill("#prompt", text)
    page.tap("#send")


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
        # ...and it leaves nothing behind. Its shadow is wide enough to reach back
        # onto the screen from there (a grey smear down the left edge, visible in
        # the light theme only), so a closed drawer casts none.
        shadow = "() => getComputedStyle(document.getElementById('sidebar')).boxShadow"
        assert page.evaluate(shadow) == "none"
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
        assert page.evaluate(shadow) != "none", "the open drawer is lifted off the page"
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


# ---------------------------------------------------------------- copy buttons
PHONE = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
         "device_scale_factor": 2}
HEAD_SHA = "c0ffee" + "0" * 34


def _autonomous(page, text, *, tap=False):
    page.click("#mode-btn")
    page.click('.menu-item[data-mode="autonomous"]')
    (_send_tap if tap else _send)(page, text)


def _plan(count, *, chain=False):
    return json.dumps({"tasks": [
        {"id": f"t{i}", "title": f"Задача номер {i} — сделать что-то полезное и проверить",
         "description": "Описание.", "acceptance": ["готово"],
         "dependencies": [f"t{i - 1}"] if chain and i > 1 else []}
        for i in range(1, count + 1)]})


def _task_conversations(stack):
    return [cid for cid, c in list(stack.cloud.cloud.conversations.items())
            if (c.get("tags") or {}).get("nexustask")]


def _finish_task(stack, cid, text):
    stack.cloud.cloud.add_assistant_message(cid, text)
    stack.cloud.cloud.conversations[cid]["execution_status"] = "finished"


def _wait(read, *, timeout=30.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _drag(page, selector, *, distance=600):
    """A finger put down on *selector* and dragged: up for a positive
    *distance* (the content moves down under it), down for a negative one.

    Real touch events - touchstart, a run of touchmoves, touchend - sent to the
    browser's input pipeline, not a script setting ``scrollTop``: whatever
    would stop a finger (an element lying over the list, ``touch-action``, a
    handler that cancels the gesture) stops this too.
    """
    box = page.locator(selector).first.bounding_box()
    viewport = page.viewport_size
    top = max(box["y"], 0) + 30
    bottom = min(box["y"] + box["height"], viewport["height"]) - 30
    x = box["x"] + box["width"] / 2
    start = bottom if distance > 0 else top
    end = max(top, start - distance) if distance > 0 else min(bottom, start - distance)
    session = page.context.new_cdp_session(page)
    session.send("Input.dispatchTouchEvent", {
        "type": "touchStart", "touchPoints": [{"x": x, "y": start}]})
    steps = 14
    for step in range(1, steps + 1):
        session.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": x, "y": start + (end - start) * step / steps}]})
        page.wait_for_timeout(16)
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    session.detach()
    page.wait_for_timeout(450)                  # the list has stopped moving


def _swipe_up(page, selector, *, distance=600, times=1):
    for _ in range(times):
        _drag(page, selector, distance=distance)


def _drag_to_end(page, selector, *, tries=30):
    for _ in range(tries):
        if _scroll_state(page, selector)["atEnd"]:
            return
        _drag(page, selector)
    assert _scroll_state(page, selector)["atEnd"], _scroll_state(page, selector)


def _scroll_state(page, selector):
    return page.evaluate(
        """(selector) => { const e = document.querySelector(selector);
            return {top: Math.round(e.scrollTop), height: e.scrollHeight, client: e.clientHeight,
                    atEnd: e.scrollTop + e.clientHeight >= e.scrollHeight - 2}; }""", selector)


# ------------------------------------------------------------------- copying
def test_a_user_message_and_the_assistant_reply_can_be_copied(stack, chromium):
    answer = "Ответ, который стоит сохранить.\n\n```python\nprint('ok')\n```\n\n- пункт **один**\n- пункт два"
    stack.answers(answer)
    task = "Задание: сделать что-то полезное.\n\n" + "\n".join(
        f"{n}. Пункт номер {n}: проверить <b>всё</b> & записать «как есть»." for n in range(1, 61))
    ctx, page, watch = _open(chromium, permissions=["clipboard-read", "clipboard-write"])
    try:
        _login(page, stack.base)
        page.fill("#prompt", task)
        page.click("#send")
        page.wait_for_selector(".msg.assistant .md")
        # The person's own message: the whole text, exactly, however long - not
        # what the bubble shows and nothing that is not part of the message.
        page.click(".msg.user [data-action='copy-message']")
        expect(page.locator(".toast")).to_contain_text("Сообщение скопировано")
        assert page.evaluate("() => navigator.clipboard.readText()") == task
        # The assistant's reply: its source text, with the formatting it was
        # written in, not the rendered page.
        page.click(".msg.assistant [data-action='copy-message']")
        expect(page.locator(".toast").last).to_contain_text("Ответ скопирован")
        assert page.evaluate("() => navigator.clipboard.readText()") == answer
        assert watch.problems == []
    finally:
        ctx.close()


def test_the_copy_button_is_there_for_a_finger_on_a_phone(stack, chromium):
    stack.answers("Короткий ответ.")
    ctx, page, watch = _open(chromium, permissions=["clipboard-read", "clipboard-write"], **PHONE)
    try:
        _login(page, stack.base)
        _send_tap(page, "Что скопировать?")
        page.wait_for_selector(".msg.assistant .md")
        for selector in (".msg.user [data-action='copy-message']",
                         ".msg.assistant [data-action='copy-message']"):
            button = page.locator(selector)
            # No hover on a touch screen: the button is simply visible...
            assert button.evaluate("e => getComputedStyle(e).opacity") == "1"
            box = button.bounding_box()
            # ...and large enough to hit.
            assert box["width"] >= 40 and box["height"] >= 40, box
        page.tap(".msg.user [data-action='copy-message']")
        expect(page.locator(".toast")).to_contain_text("Сообщение скопировано")
        assert page.evaluate("() => navigator.clipboard.readText()") == "Что скопировать?"
        assert _overflow(page) <= 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_copying_works_where_there_is_no_clipboard_api(stack, chromium):
    """An http:// origin or an embedded web view has no ``navigator.clipboard``
    to write with. The text is copied the old way."""
    stack.answers("Ответ для старого браузера.")
    ctx, page, watch = _open(chromium, permissions=["clipboard-read", "clipboard-write"])
    try:
        page.add_init_script("""
            const read = navigator.clipboard.readText.bind(navigator.clipboard);
            window.__readClipboard = read;
            Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
        """)
        _login(page, stack.base)
        _send(page, "Вопрос без Clipboard API")
        page.wait_for_selector(".msg.assistant .md")
        assert page.evaluate("() => navigator.clipboard === undefined")
        page.click(".msg.assistant [data-action='copy-message']")
        expect(page.locator(".toast")).to_contain_text("Ответ скопирован")
        assert page.evaluate("() => window.__readClipboard()") == "Ответ для старого браузера."
        page.click(".msg.user [data-action='copy-message']")
        expect(page.locator(".toast").last).to_contain_text("Сообщение скопировано")
        assert page.evaluate("() => window.__readClipboard()") == "Вопрос без Clipboard API"
        assert watch.problems == []
    finally:
        ctx.close()


def test_when_the_browser_lets_nothing_be_copied_the_text_is_put_in_front_of_the_person(stack, chromium):
    """Permission denied, and the old way refused as well. The button does not
    just fail: the whole text is shown, selected, to be copied by hand."""
    answer = "Ответ, который нельзя потерять.\nВторая строка."
    stack.answers(answer)
    ctx, page, watch = _open(chromium)
    try:
        page.add_init_script("""
            Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
                writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) } });
            document.execCommand = () => false;
        """)
        _login(page, stack.base)
        _send(page, "Вопрос")
        page.wait_for_selector(".msg.assistant .md")
        page.click(".msg.assistant [data-action='copy-message']")
        page.wait_for_selector("#copy-area")
        expect(page.locator(".dialog h2")).to_have_text("Скопируйте вручную")
        assert page.input_value("#copy-area") == answer
        assert page.evaluate(
            "() => { const a = document.querySelector('#copy-area');"
            " return document.activeElement === a && a.selectionStart === 0"
            " && a.selectionEnd === a.value.length; }"), "the text is not selected"
        assert page.locator(".toast").count() == 0, "nothing claims the text was copied"
        page.click(".dialog-foot .btn-primary")
        page.wait_for_selector(".dialog-wrap", state="detached")
        assert watch.problems == []
    finally:
        ctx.close()


# ------------------------------------------------------ the report in the chat
def test_the_report_arrives_in_the_open_chat_by_itself_and_can_be_copied(lab_stack, chromium):
    """Nobody reloads the page or opens the list of runs. The server's own loop
    runs the task, checks it and completes the run; the report is then in the
    chat the run was asked for in."""
    stack = lab_stack
    stack.github.push(REPO, "ba5e" + "0" * 36, "initial")
    ctx, page, watch = _open(chromium, permissions=["clipboard-read", "clipboard-write"])
    try:
        _login(page, stack.base)
        _connect_repository(page)
        stack.answers(_plan(1))
        _autonomous(page, "Цель: одна задача, сделать и проверить")
        page.wait_for_selector(".work-chip")
        assert page.locator(".msg.assistant").count() == 1

        cid = _wait(lambda: _task_conversations(stack), what="the task to be started")[0]
        stack.github.push(REPO, HEAD_SHA, "feat: the one task", [
            {"filename": "src/cart.py", "status": "modified", "additions": 7, "deletions": 2}])
        _finish_task(stack, cid, f"Готово. Коммит {HEAD_SHA}. Тесты: 9 passed.")

        # The report appears in the open chat - a second assistant message.
        report = page.locator(".msg.assistant").nth(1)
        expect(report.locator(".md h2")).to_have_text("Отчёт автономного прогона", timeout=30_000)
        text = report.inner_text()
        assert "выполнено, все задачи проверены и подтверждены" in text
        assert "Цель: одна задача, сделать и проверить" in text
        assert "Задача номер 1" in text and HEAD_SHA[:10] in text and "Тесты: 9 passed." in text
        assert "1 коммит, 1 файл (+7 −2)" in text and "src/cart.py" in text
        assert "Независимая проверка" in text and "Ограничения и блокеры" in text

        # What is copied is the report as it is stored, whole.
        principal, project = _owner_principal(stack.app), _first_project(stack.app)
        run = stack.app.lab.list_runs(principal, project, include_hidden=True)[0]
        stored = stack.app.reports.get(principal, project, str(run["id"]))
        assert stored["state"] == "delivered"
        report.locator("[data-action='copy-message']").click()
        expect(page.locator(".toast")).to_contain_text("Отчёт скопирован")
        assert page.evaluate("() => navigator.clipboard.readText()") == stored["body"]

        # The same report in the run's card, with its own copy button. A
        # completed run leaves the active list, so the card is reached through
        # the archive - not through a chip in the transcript.
        page.click("#tab-agent")
        page.wait_for_selector("[data-action='toggle-archive']")
        page.click("[data-action='toggle-archive']")
        page.wait_for_selector("[data-action='open-archived']")
        page.click("[data-action='open-archived']")
        page.wait_for_selector(".work [data-action='copy-report']")
        expect(page.locator(".work .pill").first).to_have_text("готово")
        assert "отправлен в чат" in page.inner_text(".work")
        assert "исполнитель: ключ сервера" in page.inner_text(".work .steps")
        page.click(".work [data-action='copy-report']")
        expect(page.locator(".toast").last).to_contain_text("Отчёт скопирован")
        assert page.evaluate("() => navigator.clipboard.readText()") == stored["body"]

        # One run, one report: further passes of the loop and of the page add nothing.
        page.wait_for_timeout(6000)
        assert page.locator(".msg.assistant").count() == 2
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        page.wait_for_selector(".msg.assistant .md h2")
        assert page.locator(".msg.assistant").count() == 2
        assert watch.problems == []
    finally:
        ctx.close()


def _owner_principal(app):
    with app.store._tx() as cur:
        cur.execute("SELECT principal FROM nexus_project_members ORDER BY created_at LIMIT 1")
        return cur.fetchone()["principal"]


def _first_project(app):
    with app.store._tx() as cur:
        cur.execute("SELECT id FROM nexus_projects ORDER BY created_at LIMIT 1")
        return str(cur.fetchone()["id"])


# ------------------------------------------------- cancel hides the run (phone)
def test_a_cancelled_run_leaves_the_agent_tab_and_stays_gone(lab_stack, chromium):
    stack = lab_stack
    ctx, page, watch = _open(chromium, **PHONE)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        stack.answers(_plan(2))
        _autonomous(page, "Цель: две задачи", tap=True)
        page.wait_for_selector(".work-chip")
        cid = _wait(lambda: _task_conversations(stack), what="a task to be started")[0]
        sandbox = stack.cloud.cloud.conversations[cid]["sandbox_id"]
        page.tap(".work-chip")
        page.wait_for_selector(".work")
        assert page.locator(".work").count() == 1

        # «Отменить»: the card leaves the tab at once.
        page.tap('.work [data-op="cancel"]')
        expect(page.locator(".dialog .lead")).to_contain_text("останутся в базе")
        page.tap(".dialog-foot .btn-danger")
        page.wait_for_selector(".work", state="detached")
        expect(page.locator("#ws-body .empty h3")).to_have_text("Агент ещё не запускался")
        expect(page.locator(".toast")).to_contain_text("Прогон отменён")
        # The executor was really told to stop: the sandbox was paused.
        _wait(lambda: sandbox in stack.cloud.cloud.paused, what="OpenHands to be told to stop")

        # In the transcript the chip says what happened and opens nothing.
        page.tap("#ws-body [data-action='toggle-workspace'], .ws-head [data-action='toggle-workspace']")
        chip = page.locator(".work-chip")
        expect(chip.locator(".work-chip-state")).to_have_text("остановлено")
        expect(chip).to_be_disabled()

        # A reload does not bring it back - not in the tab, not in the badge.
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        expect(page.locator(".work-chip .work-chip-state")).to_have_text("остановлено")
        page.tap("#ws-toggle")
        page.tap("#tab-agent")
        expect(page.locator("#ws-body .empty h3")).to_have_text("Агент ещё не запускался")
        assert page.locator(".work").count() == 0
        assert page.locator("#agent-count").is_hidden()

        # Nothing was deleted: the run, its plan and what was done are in the database.
        principal, project = _owner_principal(stack.app), _first_project(stack.app)
        assert stack.app.lab.list_runs(principal, project) == []
        [kept] = stack.app.lab.list_runs(principal, project, include_hidden=True)
        assert kept["state"] == "cancelled" and kept["hidden"] is True
        assert len(stack.app.lab.list_tasks(principal, project, str(kept["id"]))) == 2
        assert [e["outcome"] for e in stack.app.lab.executions(str(kept["id"]))] == ["cancelled"]
        assert watch.problems == []
    finally:
        ctx.close()


def test_a_stopped_task_leaves_the_agent_tab_too(stack, chromium):
    """«Остановить» on a task of the "code" mode: the same rule as for a run."""
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        page.click("#mode-btn")
        page.click('.menu-item[data-mode="code"]')
        _send(page, "Добавь тест для cart.add")
        page.wait_for_selector(".work-chip")
        page.locator(".work-chip").click()
        page.wait_for_selector(".work [data-action='cancel-workflow']")
        page.click(".work [data-action='cancel-workflow']")
        page.click(".dialog-foot .btn-danger")
        page.wait_for_selector(".work", state="detached")
        expect(page.locator("#ws-body .empty h3")).to_have_text("Агент ещё не запускался")
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        expect(page.locator(".work-chip .work-chip-state")).to_have_text("остановлено")
        if page.locator("#workspace").is_hidden():
            page.click("#ws-toggle")
        page.click("#tab-agent")
        expect(page.locator("#ws-body .empty h3")).to_have_text("Агент ещё не запускался")
        # The workflow is still in the database, cancelled.
        status = stack.app.store.list_workflows()[0]["state"]
        assert status == "cancelled"
        assert watch.problems == []
    finally:
        ctx.close()


# ---------------------------------------------------- scrolling on the phone
def test_a_phone_scrolls_the_list_of_runs_to_the_last_one_with_a_finger(stack, chromium):
    """Four runs, each with a plan longer than the screen. Two things used to
    keep the end of the list out of reach: a card shrank to fit the list
    instead of making it scroll (its content was clipped), and every few
    seconds the list was redrawn from its top while a run was at work."""
    ctx, page, watch = _open(chromium, **PHONE)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        for n in range(1, 5):
            stack.answers(_plan(9))
            if n == 1:
                _autonomous(page, f"Цель номер {n}: девять задач", tap=True)
            else:
                _send_tap(page, f"Цель номер {n}: девять задач")
            expect(page.locator(".work-chip")).to_have_count(n)
        page.tap("#ws-toggle")
        page.tap("#tab-agent")
        expect(page.locator(".work")).to_have_count(4)

        # No card is cut: each is as tall as what is in it.
        clipped = page.evaluate(
            "() => [...document.querySelectorAll('.work')]"
            ".filter(e => e.scrollHeight > e.clientHeight + 1).length")
        assert clipped == 0, "a card is clipped instead of the list scrolling"
        state = _scroll_state(page, ".work-list")
        assert state["height"] > state["client"] * 2 and state["top"] == 0

        # A finger reaches the last run and the last task of its plan.
        _drag(page, ".work-list")
        assert _scroll_state(page, ".work-list")["top"] > 200, "a finger does not move the list"
        _drag_to_end(page, ".work-list")
        last_card = page.locator(".work").last.bounding_box()
        last_task = page.locator(".work").last.locator(".step-row").last.bounding_box()
        cancel = page.locator(".work").last.locator('[data-op="cancel"]').bounding_box()
        for box in (last_task, cancel):
            assert 0 <= box["y"] and box["y"] + box["height"] <= 844, box
        assert last_card["y"] + last_card["height"] <= 844
        expect(page.locator(".work").last.locator(".step-title").last).to_contain_text(
            "Задача номер 9")

        # The runs are at work, so the page reads them again every few seconds.
        # The list stays where the finger left it.
        before = _scroll_state(page, ".work-list")["top"]
        page.wait_for_timeout(11_000)
        after = _scroll_state(page, ".work-list")
        assert after["top"] == before and after["atEnd"], (before, after)

        # Back up, and the first run is there again.
        for _ in range(30):
            if _scroll_state(page, ".work-list")["top"] == 0:
                break
            _drag(page, ".work-list", distance=-600)
        assert _scroll_state(page, ".work-list")["top"] == 0
        first = page.locator(".work").first.bounding_box()
        assert first["y"] >= 0

        # After a reload the list scrolls just the same.
        page.reload()
        page.wait_for_selector('#app[data-ready="true"]')
        page.tap("#ws-toggle")
        page.tap("#tab-agent")
        expect(page.locator(".work")).to_have_count(4)
        _drag_to_end(page, ".work-list")
        assert _overflow(page) <= 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_a_long_plan_scrolls_to_its_last_task_on_a_phone(stack, chromium):
    ctx, page, watch = _open(chromium, **PHONE)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        stack.answers(_plan(40, chain=True))
        _autonomous(page, "Цель: длинная цепочка из сорока шагов", tap=True)
        page.wait_for_selector(".work-chip")
        page.tap(".work-chip")
        page.wait_for_selector(".steps .step-row")
        expect(page.locator(".step-row")).to_have_count(40)
        assert page.evaluate(
            "() => { const e = document.querySelector('.work');"
            " return e.scrollHeight <= e.clientHeight + 1; }"), "the plan is clipped"

        _drag_to_end(page, ".work-list")
        last = page.locator(".step-row").last
        expect(last.locator(".step-title")).to_contain_text("Задача номер 40")
        box = last.bounding_box()
        assert 0 <= box["y"] and box["y"] + box["height"] <= 844
        # The buttons under the plan are reachable as well.
        pause = page.locator('.work [data-op="pause"]').bounding_box()
        assert pause["y"] + pause["height"] <= 844

        # Close the panel and open it again: the thread behind it still scrolls
        # and nothing is wider than the screen.
        page.tap(".ws-head [data-action='toggle-workspace']")
        page.wait_for_selector("#workspace", state="hidden")
        assert page.locator("#send").is_visible()
        page.tap(".work-chip")
        page.wait_for_selector(".steps .step-row")
        assert _overflow(page) <= 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_with_the_keyboard_open_the_chat_and_the_composer_stay_in_reach(stack, chromium):
    """An on-screen keyboard leaves about half the screen. The composer stays
    on it, the thread still scrolls to its last message, and the panel of runs
    still scrolls to its end."""
    stack.answers("\n\n".join(f"Абзац {n}: " + "длинный ответ " * 12 for n in range(1, 13)))
    ctx, page, watch = _open(chromium, **PHONE)
    try:
        _login(page, stack.base)
        _send_tap(page, "Расскажи подробно")
        page.wait_for_selector(".msg.assistant .md")
        page.set_viewport_size({"width": 390, "height": 400})        # the keyboard is up
        page.tap("#prompt")
        composer = page.locator("#composer").bounding_box()
        assert composer["y"] >= 0 and composer["y"] + composer["height"] <= 400, composer
        thread = _scroll_state(page, "#thread")
        assert thread["height"] > thread["client"], "the thread does not scroll"
        # From the top of the thread to its last line, with a finger.
        page.evaluate("() => { document.querySelector('#thread').scrollTop = 0; }")
        page.wait_for_timeout(100)
        _drag(page, "#thread", distance=200)
        assert _scroll_state(page, "#thread")["top"] > 50, "a finger does not move the thread"
        _drag_to_end(page, "#thread")
        last = page.locator(".msg.assistant .md p").last.bounding_box()
        assert last["y"] + last["height"] <= composer["y"] + 1, "the composer covers the last line"
        assert _overflow(page) <= 0
        assert watch.problems == []
    finally:
        ctx.close()


def test_the_settings_scroll_to_their_last_section_on_a_phone(stack, chromium):
    ctx, page, watch = _open(chromium, **PHONE)
    try:
        _login(page, stack.base)
        _connect_repository(page)
        page.tap("#open-sidebar")
        page.tap("#user-menu")
        page.tap(".menu-item[data-action='open-settings']")
        page.tap('.settings-nav [data-tab="project"]')
        page.wait_for_selector("#project-executors")
        target = ".dialog.is-settings .dialog-body"
        scrollable = page.evaluate(
            """() => [...document.querySelectorAll('.dialog.is-settings .dialog-body, .settings-pane')]
                .filter(e => e.scrollHeight > e.clientHeight + 1)
                .map(e => e.className)""")
        assert scrollable, "nothing in the settings scrolls"
        target = "." + scrollable[0].split()[0]
        _drag_to_end(page, target)
        box = page.locator("#project-executors").bounding_box()
        assert box["y"] + box["height"] <= 844 + 1, "the last section is out of reach"
        assert _overflow(page) <= 0
        assert watch.problems == []
    finally:
        ctx.close()


# ------------------------------------------- OpenHands connections and executors
def test_two_openhands_are_connected_and_given_to_the_project_in_the_settings(stack, chromium):
    """Two connections, each to its own service, added and checked in the
    settings; one that stops answering says so without touching the other; the
    project is given both - a primary and a backup - the order is changed and
    the strategy switched. No key is in the page at any point."""
    keys = ("sk-first-openhands-key-0001", "sk-second-openhands-key-0002")
    with FakeCloudServer() as second:
        second.cloud.settings = dict(stack.cloud.cloud.settings)
        ctx, page, watch = _open(chromium)
        try:
            _login(page, stack.base)
            _connect_repository(page)                  # the project the executors are for
            page.click("#user-menu")
            page.click(".menu-item[data-action='open-settings']")
            form = ".openhands-form"
            for label, server, key in (("основной", stack.cloud, keys[0]),
                                       ("запасной", second, keys[1])):
                # The form folds away once there is a connection; unfold it.
                summary = page.locator("details.add summary", has_text="Подключить OpenHands")
                summary.wait_for()
                if not summary.evaluate("el => el.parentElement.open"):
                    summary.click()
                page.fill(f"{form} [name=label]", label)
                page.fill(f"{form} [name=base_url]", server.base_url)
                page.fill(f"{form} [name=api_key]", key)
                page.select_option(f"{form} [name=transport]", "app")
                page.click(f"{form} button[type=submit]")
                card = page.locator("[data-openhands]", has_text=label)
                expect(card.locator(".pill")).to_have_text("активен")
                assert server.base_url in card.inner_text()
            assert page.locator("[data-openhands]").count() == 2
            for key in keys:
                assert key not in page.content(), "a key came back to the page"
            # Each was checked against its own service, with its own key, and
            # neither service was ever shown the other's.
            assert keys[0] in stack.cloud.cloud.keys and keys[1] not in stack.cloud.cloud.keys
            assert set(second.cloud.keys) == {keys[1]}

            # The second service stops answering: its card says so, the first
            # one is untouched; when it is back, a check says that too.
            backup = page.locator("[data-openhands]", has_text="запасной")
            main = page.locator("[data-openhands]", has_text="основной")
            second.cloud.settings_status = 503
            backup.locator("[data-op='verify']").click()
            expect(backup.locator("small.is-bad")).to_be_visible()
            assert main.locator("small.is-bad").count() == 0
            expect(main.locator(".pill")).to_have_text("активен")
            second.cloud.settings_status = 0
            backup.locator("[data-op='verify']").click()
            expect(backup.locator("small.is-bad")).to_have_count(0)
            expect(backup.locator(".pill")).to_have_text("активен")

            # The project tab: both are given to the project, in order.
            page.click(".settings-nav [data-tab='project']")
            page.wait_for_selector("#project-executors")
            assert "Список пуст" in page.inner_text("#project-executors")
            for label in ("основной", "запасной"):
                page.select_option("#executor-add [name=account_id]", label=label)
                page.click("#executor-add button[type=submit]")
                expect(page.locator("[data-executor]", has_text=label)).to_be_visible()
            roles = lambda: [text.split("\n")[1].split(" · ")[0] for text in  # noqa: E731
                             page.locator("[data-executor] .card-main").all_inner_texts()]
            labels = lambda: page.locator("[data-executor] .card-main b").all_inner_texts()  # noqa: E731
            assert labels() == ["основной", "запасной"]
            assert roles() == ["основной", "резервный 1"]

            # The backup becomes the primary; the order follows.
            page.locator("[data-executor]", has_text="запасной").locator(
                "[data-op='primary']").click()
            expect(page.locator("[data-executor] .card-main b").first).to_have_text("запасной")
            assert roles() == ["основной", "резервный 1"] and labels() == ["запасной", "основной"]

            # The strategy is the project's own, and it is kept.
            page.select_option("#executor-strategy", "distribute")
            expect(page.locator(".toast").last).to_be_visible()
            page.click("[data-action='executors-check']")
            expect(page.locator("[data-executor] .pill").first).to_have_text("активен")

            page.reload()
            page.wait_for_selector('#app[data-ready="true"]')
            page.click("#user-menu")
            page.click(".menu-item[data-action='open-settings']")
            page.click(".settings-nav [data-tab='project']")
            page.wait_for_selector("[data-executor]")
            assert labels() == ["запасной", "основной"]
            assert page.input_value("#executor-strategy") == "distribute"
            for key in keys:
                assert key not in page.content()
            assert watch.problems == []
        finally:
            ctx.close()


def test_the_openhands_selection_mode_and_fallback_order_are_driven_from_the_settings(stack, chromium):
    """The OpenHands settings tab carries the operator's controls the request
    names: the default connection, the fallback order and the selection mode.
    Each is changed in the interface and read back from the server, and no key
    is in the page."""
    keys = ("sk-first-openhands-key-0001", "sk-second-openhands-key-0002")
    with FakeCloudServer() as second:
        second.cloud.settings = dict(stack.cloud.cloud.settings)
        ctx, page, watch = _open(chromium)
        try:
            _login(page, stack.base)
            page.click("#user-menu")
            page.click(".menu-item[data-action='open-settings']")
            form = ".openhands-form"
            for label, server, key in (("основной", stack.cloud, keys[0]),
                                       ("запасной", second, keys[1])):
                summary = page.locator("details.add summary", has_text="Подключить OpenHands")
                summary.wait_for()
                if not summary.evaluate("el => el.parentElement.open"):
                    summary.click()
                page.fill(f"{form} [name=label]", label)
                page.fill(f"{form} [name=base_url]", server.base_url)
                page.fill(f"{form} [name=api_key]", key)
                page.click(f"{form} button[type=submit]")
                expect(page.locator("[data-openhands]", has_text=label)).to_be_visible()

            # The pool controls appear once a connection exists: the mode and
            # the order, listing both connections.
            page.wait_for_selector("#openhands-pool")
            assert page.locator("#openhands-pool .step-title").all_inner_texts() == ["основной", "запасной"]

            # Changing the selection mode is a real server setting.
            page.select_option("#pool-mode", "load_balancing")
            expect(page.locator(".toast", has_text="Режим выбора сохранён")).to_be_visible()
            pool = stack.app.account_pool.get_config("user:owner")
            assert pool["selection_mode"] == "load_balancing"

            # Moving a connection up rewrites the fallback order on the server.
            page.locator("#openhands-pool .step-row", has_text="запасной").locator(
                "[data-dir='up']").click()
            expect(page.locator(".toast", has_text="Порядок переключения сохранён")).to_be_visible()
            order = stack.app.account_pool.get_config("user:owner")["fallback_order"]
            accounts = stack.app.openhands_accounts.list_accounts("user:owner")
            by_label = {a["label"]: a["id"] for a in accounts}
            assert order == [by_label["запасной"], by_label["основной"]]

            # Making a connection the default is one action, and it is durable.
            page.locator("[data-openhands]", has_text="запасной").locator(
                "[data-action='openhands-default']").click()
            expect(page.locator(".toast", has_text="Основное подключение изменено")).to_be_visible()
            defaults = [a["label"] for a in stack.app.openhands_accounts.list_accounts("user:owner")
                        if a["is_default"]]
            assert defaults == ["запасной"]

            # The error history is reachable and honest about being empty.
            page.locator("[data-openhands]", has_text="основной").locator(
                "[data-action='openhands-errors']").click()
            errors = page.locator(".dialog:not(.is-settings)").last
            expect(errors).to_be_visible()
            expect(errors.locator(".section-note")).to_contain_text("Ошибок не зафиксировано")
            errors.locator("[data-action='close-dialog']").last.click()

            for key in keys:
                assert key not in page.content(), "a key came back to the page"
            assert watch.problems == []
        finally:
            ctx.close()


# ------------------------------- several repositories, each with its own account
def test_a_project_gets_a_second_repository_from_another_github_account(stack, chromium):
    """Two private repositories, each visible to one GitHub account only. The
    project is given both in the interface: the main one with the first
    account, one more with the second. Each is shown with the account that
    reads it and checked with that account's own token; the second becomes the
    main one; no token is in the page."""
    api_repo, web_repo = "acme/api", "acme/web"
    tokens = {"команда api": "ghp_token_of_the_api_account_0001",
              "команда web": "ghp_token_of_the_web_account_0002"}
    gh = stack.github
    for name in (api_repo, web_repo):
        gh.repos.append({"full_name": name, "default_branch": "main", "private": True,
                         "archived": False, "pushed_at": "2026-01-05T00:00:00Z"})
        gh.push(name, ("a" if name == api_repo else "b") + "0" * 39, "initial")
    gh.token_repos = {tokens["команда api"]: {api_repo}, tokens["команда web"]: {web_repo},
                      "server-github-token": set()}

    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        page.click("#user-menu")
        page.click(".menu-item[data-action='open-settings']")
        for label, token in tokens.items():
            summary = page.locator("details.add summary", has_text="Подключить аккаунт GitHub")
            summary.wait_for()
            if not summary.evaluate("el => el.parentElement.open"):
                summary.click()
            page.fill(".account-form:not(.openhands-form) [name=label]", label)
            page.fill(".account-form:not(.openhands-form) [name=token]", token)
            page.click(".account-form:not(.openhands-form) button[type=submit]")
            expect(page.locator(".card", has_text=label).locator(".pill")).to_have_text("активен")
        page.keyboard.press("Escape")

        def pick(source_label: str, name: str) -> None:
            page.wait_for_selector("#repo-source")
            page.select_option("#repo-source", label=source_label)
            page.click(f'.picker-item[data-name="{name}"]')
            page.wait_for_selector("#repo-branch option:nth-of-type(2)", state="attached")
            page.click("#repo-connect")
            page.wait_for_selector("#repo-connect", state="detached")

        # The main repository, with the first account. That account does not
        # even see the other repository.
        page.click("#repo-chip")
        page.wait_for_selector("#repo-source")
        page.select_option("#repo-source", label="команда api — acme")
        page.wait_for_selector(f'.picker-item[data-name="{api_repo}"]')
        assert page.locator(f'.picker-item[data-name="{web_repo}"]').count() == 0
        page.keyboard.press("Escape")
        page.click("#repo-chip")
        pick("команда api — acme", api_repo)
        expect(page.locator("#repo-chip-text")).to_have_text(f"{api_repo} · main")

        def project_settings() -> None:
            """The project tab of the settings, wherever the page is now."""
            if page.locator(".dialog.is-settings").count() == 0:
                page.click("#user-menu")
                page.click(".menu-item[data-action='open-settings']")
            page.click(".settings-nav [data-tab='project']")
            page.wait_for_selector("#project-repositories")

        # One more repository, from the second account.
        project_settings()
        page.wait_for_selector(f'#project-repositories [data-repo="{api_repo}"]')
        page.click("#project-repositories [data-action='open-repo'][data-additional]")
        pick("команда web — acme", web_repo)

        # The interface comes back to the project's settings by itself, with
        # the new repository listed.
        page.wait_for_selector(f'#project-repositories [data-repo="{web_repo}"]')
        first = page.locator(f'#project-repositories [data-repo="{api_repo}"]')
        second = page.locator(f'#project-repositories [data-repo="{web_repo}"]')
        expect(second).to_be_visible()
        assert "основной" in first.inner_text() and "основной" not in second.inner_text()
        assert "команда web" in second.inner_text()
        # The chat still works in the main repository.
        expect(page.locator("#repo-chip-text")).to_have_text(f"{api_repo} · main")

        # Access is asked of GitHub for each with its own token.
        seen = len(gh.paths)
        page.click("#project-repositories [data-action='repos-check']")
        for card in (first, second):
            expect(card).to_contain_text("приватный · запись разрешена")
            expect(card.locator(".pill")).to_have_text("активен")
        asked = {(path, (header or "").split()[-1])
                 for path, header in zip(gh.paths[seen:], gh.auth_headers[seen:])}
        assert (f"/repos/{api_repo}", tokens["команда api"]) in asked
        assert (f"/repos/{web_repo}", tokens["команда web"]) in asked
        assert (f"/repos/{web_repo}", tokens["команда api"]) not in asked
        assert (f"/repos/{api_repo}", tokens["команда web"]) not in asked

        # The second one becomes the main repository.
        second.locator("[data-op='primary']").click()
        expect(page.locator("#project-repositories [data-repo] b").first).to_have_text(web_repo)
        page.keyboard.press("Escape")
        expect(page.locator("#repo-chip-text")).to_have_text(f"{web_repo} · main")

        for token in tokens.values():
            assert token not in page.content(), "a token came back to the page"
        assert watch.problems == []
    finally:
        ctx.close()


# ------------------------------------------------------- staying signed in
DAY = 24 * 3600


def _time_passes(interval: str) -> None:
    """*interval* passes for the open sessions: their own times move back by
    it in the database, which is where the server reads them from. The
    browser's clock stays where it is, so a cookie's expiry is counted from
    the real moment it was set."""
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "UPDATE sessions SET created_at = created_at - %(i)s::interval, "
            "last_seen = last_seen - %(i)s::interval, "
            "expires_at = expires_at - %(i)s::interval", {"i": interval})


def _session_cookie(ctx) -> dict:
    return next(c for c in ctx.cookies() if c["name"] == "dispatcher_session")


def test_the_owner_stays_signed_in_from_one_day_to_the_next(stack, chromium):
    """What the owner reported: the studio asked for the password about once a
    day. A login lasted twelve hours whether the studio was used or not.

    One sign-in in a real browser, then: thirteen hours later, and every eleven
    days after that, the studio opens - not the login form. The browser's own
    copy of the cookie follows the session: it is replaced when the session is
    extended, and it is given only what is left when the ninety days are nearly
    over. After ninety days the login form is back, once."""
    ctx, page, watch = _open(chromium)
    try:
        _login(page, stack.base)
        first = _session_cookie(ctx)
        assert first["httpOnly"] and first["sameSite"] == "Strict"
        assert 30 * DAY - 60 < first["expires"] - time.time() <= 30 * DAY

        def visit() -> None:
            page.goto(stack.base + "/")
            page.wait_for_selector('#app[data-ready="true"]')
            assert page.url == stack.base + "/"
            expect(page.locator("#whoami")).to_have_text(USERNAME)

        # Thirteen hours on: the old rule had ended the session an hour ago.
        page.wait_for_timeout(1200)
        _time_passes("13 hours")
        visit()
        renewed = _session_cookie(ctx)
        assert renewed["value"] == first["value"], "the same session, not a new login"
        assert renewed["expires"] > first["expires"] + 1, "the browser's cookie was not renewed"

        # The page that is open goes on working: its CSRF token is the session's.
        stack.answers("Всё ещё здесь.")
        _send(page, "Проверка через тринадцать часов")
        expect(page.locator(".msg.assistant .md")).to_have_text("Всё ещё здесь.")

        # Used every eleven days, up to the 88th.
        for _ in range(8):
            _time_passes("11 days")
            visit()
        # The last time there was anything to add was the 66th day, and the
        # cookie was given what was then left of the ninety: 23 days and a half.
        left = _session_cookie(ctx)["expires"] - time.time()
        assert 23 * DAY < left < 24 * DAY, left / DAY
        assert _session_cookie(ctx)["value"] == first["value"]
        assert watch.problems == []

        # Past the ninety days the session is over, used or not.
        watch.allow(r"HTTP 401 ")      # the open page may ask once more before it leaves
        _time_passes("2 days")
        page.goto(stack.base + "/")
        page.wait_for_selector("#loginbtn")
        assert page.url == stack.base + "/login"
        _login(page, stack.base)
        again = _session_cookie(ctx)
        assert again["value"] != first["value"]
        assert 30 * DAY - 60 < again["expires"] - time.time() <= 30 * DAY
        assert watch.problems == []
    finally:
        ctx.close()


# --------------------------------------------- a link from another site
@contextlib.contextmanager
def _another_site(links: dict):
    """A page on another site with *links* (``{element id: address}``, read at
    every request, so a link can be added once its address is known).

    It is served from ``localhost``; the studio is on ``127.0.0.1``. To the
    browser those are two sites, so a click here is a cross-site navigation -
    the one a ``SameSite=Strict`` cookie is left out of."""
    import html
    from http.server import BaseHTTPRequestHandler

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802 - the handler's name
            body = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                    "<title>Другой сайт</title>" + "".join(
                        f'<p><a id="{html.escape(name)}" href="{html.escape(url, quote=True)}">'
                        f"{html.escape(name)}</a></p>" for name, url in links.items())
                    ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):          # the test's output is not a log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _journey(page, base: str) -> tuple[list, list]:
    """What the page does from now on: the documents it commits, and how many
    times it asks whether there is a session. Addresses are given without the
    studio's origin."""
    opened: list[str] = []
    asked: list[str] = []
    page.on("framenavigated", lambda frame: frame == page.main_frame
            and opened.append(frame.url.replace(base, "")))
    page.on("request", lambda request: request.url == base + "/login/state"
            and asked.append(request.url))
    return opened, asked


def _sessions() -> int:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        return conn.execute("SELECT count(*) FROM sessions").fetchone()[0]


def test_a_link_from_another_site_opens_the_studio_for_someone_signed_in(stack, chromium):
    """The second reason the owner met the login form while signed in.

    The session cookie is ``SameSite=Strict``, so Chromium leaves it out of a
    navigation that starts on another site: the server sees nobody and shows
    the form. The form's page asks, from its own origin, whether there is a
    session, and goes on to the studio - to the very chat the link named."""
    links: dict = {}
    ctx, page, watch = _open(chromium)
    try:
        with _another_site(links) as elsewhere:
            _login(page, stack.base)
            stack.answers("Ответ в чате по ссылке.")
            _send(page, "Вопрос, на который ведёт ссылка")
            expect(page.locator(".msg.assistant .md")).to_have_text("Ответ в чате по ссылке.")
            chat = page.url
            assert re.search(r"/#/p/[0-9a-f-]{36}/c/[0-9a-f-]{36}$", chat), chat
            links.update(studio=stack.base + "/", chat=chat)
            cookie = _session_cookie(ctx)
            assert cookie["sameSite"] == "Strict", "the cookie stays strict; the page copes"

            opened, asked = _journey(page, stack.base)

            # A plain link to the studio.
            page.goto(elsewhere)
            page.click("#studio")
            page.wait_for_selector('#app[data-ready="true"]')
            assert page.url == stack.base + "/"
            expect(page.locator("#whoami")).to_have_text(USERNAME)
            # What happened on the way: the browser asked for the studio
            # without the cookie and was sent to the form; the form asked once
            # and left for the studio.
            assert opened == [elsewhere, "/login", "/"], opened
            assert len(asked) == 1

            # A link to one chat opens that chat.
            del opened[:], asked[:]
            page.goto(elsewhere)
            page.click("#chat")
            page.wait_for_selector('#app[data-ready="true"]')
            assert page.url == chat
            route = chat.replace(stack.base + "/", "")
            assert opened[:3] == [elsewhere, "/login" + route, "/" + route], opened
            assert len(asked) == 1
            expect(page.locator(".msg.user").first).to_contain_text(
                "Вопрос, на который ведёт ссылка")
            expect(page.locator(".msg.assistant .md")).to_have_text("Ответ в чате по ссылке.")

            # The form is not left behind in the history: "back" is the other site.
            page.go_back()
            page.wait_for_selector("#chat")
            assert page.url == elsewhere

            # Nobody signed in a second time, and the page still works.
            assert _sessions() == 1
            assert _session_cookie(ctx)["value"] == cookie["value"]
            page.click("#chat")
            page.wait_for_selector('#app[data-ready="true"]')
            stack.answers("И дальше работает.")
            _send(page, "Ещё вопрос")
            expect(page.locator(".msg.assistant .md").last).to_have_text("И дальше работает.")
            assert watch.problems == []
    finally:
        ctx.close()


def test_a_stranger_following_the_same_link_gets_the_form_once(stack, chromium):
    """Nobody is signed in: the form stays, it asks once, nothing in the page
    fails, and signing in works as it always did."""
    links = {"studio": stack.base + "/"}
    ctx, page, watch = _open(chromium)
    try:
        with _another_site(links) as elsewhere:
            opened, asked = _journey(page, stack.base)
            page.goto(elsewhere)
            page.click("#studio")
            page.wait_for_selector("#loginbtn")
            page.wait_for_timeout(700)
            assert page.url == stack.base + "/login"
            assert opened == [elsewhere, "/login"] and len(asked) == 1, (opened, asked)
            assert _sessions() == 0

            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("#loginbtn")
            page.wait_for_selector('#app[data-ready="true"]')
            assert page.url == stack.base + "/" and _sessions() == 1
            assert watch.problems == []
    finally:
        ctx.close()


def test_the_form_does_not_send_the_visitor_round_in_circles(stack, chromium):
    """The one way this could go wrong: the form is told there is a session,
    and the navigation it then starts arrives without one - a browser that
    leaves the cookie out of it, or a sign-out in another tab at that moment.
    The server sends the visitor back to the form, and a form that tried again
    would never stop. It tries once: the second time it stays, and can be used.
    """
    links = {"studio": stack.base + "/"}
    ctx, page, watch = _open(chromium)
    try:
        with _another_site(links) as elsewhere:
            _login(page, stack.base)

            def the_cookie_goes_missing(route):
                # The form's question is answered by the server, truthfully -
                # and the cookie is gone from the browser before the form can
                # act on the answer.
                answer = route.fetch()
                assert answer.json() == {"signed_in": True}
                ctx.clear_cookies()
                route.fulfill(response=answer)

            page.route(stack.base + "/login/state", the_cookie_goes_missing)
            opened, asked = _journey(page, stack.base)
            page.goto(elsewhere)
            page.click("#studio")
            page.wait_for_selector("#loginbtn")
            page.wait_for_timeout(1500)
            assert page.url == stack.base + "/login"
            # To the form, away once, back to the form - and there it stays.
            assert opened == [elsewhere, "/login", "/login"], opened
            assert len(asked) == 1, "the form asked again"

            # The form that stayed is a working form.
            page.unroute(stack.base + "/login/state")
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("#loginbtn")
            page.wait_for_selector('#app[data-ready="true"]')
            assert page.url == stack.base + "/"
            assert watch.problems == []
    finally:
        ctx.close()
