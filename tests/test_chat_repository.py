"""The chat answers from the project's repository, read from GitHub for the turn.

The defect, as it was seen in production: a project with a repository
connected, the question "Ты вообще видишь репозиторий и проект?", and the
answer "Я не имею доступа к содержимому вашего репозитория". The answer was
true: the prompt held a stored summary and nothing else - no file list, no
file - and the instructions told the model not to speak of files it was not
given.

These tests drive the real server over HTTP on the fakes (``studio_stack``):
PostgreSQL, a socket stand-in for GitHub that records what it was asked for,
and a provider endpoint that records every prompt. They hold a chat turn to
this:

* the prompt carries the repository's real file list, at its head commit;
* the model can open files and directories, for a bounded number of rounds,
  and its answer says what was read;
* what cannot be read - a credential, a path outside the list, a repository
  GitHub refuses - is said, with the reason, and never fetched;
* the repository is read with the identity the project was given for it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_repo_reader as reader  # noqa: E402
from orchestration.nexus_chat import MAX_READ_ROUNDS, ChatResponder  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from tests.test_studio_api import (  # noqa: E402
    ACCOUNT_TOKEN,
    DSN,
    REPO,
    SERVER_GITHUB_TOKEN,
    studio_stack,
)

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run chat tests")


@pytest.fixture
def studio(monkeypatch):
    reader._STORE.clear()
    with studio_stack(monkeypatch) as stack:
        yield stack
    reader._STORE.clear()


def connected(studio, **binding) -> tuple[str, str]:
    """A project with the repository connected, and a conversation in it."""
    project = studio.project()
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": REPO, **binding})
    assert status == 200, body
    studio.github.paths.clear()
    studio.github.auth_headers.clear()
    return project, studio.conversation(project, mode="ask")


def ask(studio, project, conversation, text, mode=None):
    body = {"text": text}
    if mode:
        body["mode"] = mode
    return studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", body)


def prompts(studio) -> list[str]:
    return [call["body"]["messages"][-1]["content"] for call in studio.llm.calls]


def rules(studio) -> list[str]:
    return [call["body"]["messages"][0]["content"] for call in studio.llm.calls]


def fetched(studio, section: str) -> list[str]:
    prefix = f"/repos/{REPO}/{section}"
    return [path for path in studio.github.paths if path.startswith(prefix)]


def files(studio, contents: dict) -> None:
    studio.github.contents = dict(contents)
    studio.github.tree = [
        {"path": path, "type": "blob",
         "size": len(value if isinstance(value, bytes) else value.encode())}
        for path, value in contents.items()]


# ------------------------------------------------------------ what it is shown
def test_the_question_from_production_is_answered_with_the_repository_in_view(studio):
    project, conversation = connected(studio)
    studio.answers("Да, вижу: в репозитории 4 файла, код в src/app.py.")

    status, body = ask(studio, project, conversation, "Ты вообще видишь репозиторий и проект?")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["content"] == "Да, вижу: в репозитории 4 файла, код в src/app.py."

    prompt, system = prompts(studio)[0], rules(studio)[0]
    # The repository as it is now: which commit, and every file with its size.
    assert f"Repository {REPO}, branch main, at commit abc123" in prompt
    assert "Read from GitHub for this request. 4 files." in prompt
    for line in ("README.md (120 B)", "requirements.txt (30 B)", "src/app.py (340 B)",
                 "tests/test_app.py (210 B)"):
        assert line in prompt
    # The README is there without being asked for.
    assert "=== FILE README.md (lines 1-3 of 3) ===\n# Demo project" in prompt
    # The model is told that it can read, how, and never to deny it.
    assert "REPOSITORY ACCESS" in system and "READ: path/to/file" in system
    assert "Never say that you cannot see or access the repository" in system
    # The request is still there, whole, and the last line says what may be done.
    assert "Request:\nТы вообще видишь репозиторий и проект?" in prompt
    assert prompt.rstrip().endswith("Otherwise answer the request now.")

    # What the answer rests on is recorded with it.
    assert reply["meta"]["repository"] == {
        "full_name": REPO, "branch": "main", "revision": "abc123", "files": 4,
        "read": [{"path": "README.md", "lines": [1, 3], "of": 3}],
        "listed": [], "not_read": [], "rounds": 1}
    assert reply["meta"]["mode"] == "ask" and reply["meta"]["provider"] == "groq"
    # One request to the model: it answered at once.
    assert len(studio.llm.calls) == 1


def test_the_model_opens_the_files_it_needs_and_then_answers(studio):
    project, conversation = connected(studio)
    studio.answers("READ: src/app.py, tests/test_app.py",
                   "Функция `add` в src/app.py складывает два числа; тест проверяет 1 + 2 == 3.")

    status, body = ask(studio, project, conversation, "Что делает функция add и чем она покрыта?")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["content"].startswith("Функция `add` в src/app.py складывает")
    # The line that asked for the files is not a reply: it is not in the chat.
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert "READ:" not in json.dumps(body["messages"], ensure_ascii=False)

    first, second = prompts(studio)
    assert "def add(a, b):" not in first
    assert ("=== FILE src/app.py (lines 1-2 of 2) ===\ndef add(a, b):\n    return a + b\n"
            "=== END src/app.py ===") in second
    assert "=== FILE tests/test_app.py (lines 1-4 of 4) ===" in second
    assert "assert add(1, 2) == 3" in second
    # The second request is the first one plus what was opened: same question.
    assert "Request:\nЧто делает функция add и чем она покрыта?" in second

    meta = reply["meta"]["repository"]
    assert [item["path"] for item in meta["read"]] == [
        "README.md", "src/app.py", "tests/test_app.py"]
    assert meta["rounds"] == 2 and meta["not_read"] == []
    # Each file was fetched once, at the commit of the answer.
    assert sorted(fetched(studio, "contents")) == [
        f"/repos/{REPO}/contents/README.md", f"/repos/{REPO}/contents/src/app.py",
        f"/repos/{REPO}/contents/tests/test_app.py"]


def test_a_file_named_in_the_question_is_shown_without_a_round(studio):
    project, conversation = connected(studio)
    studio.answers("Она возвращает сумму.")
    status, body = ask(studio, project, conversation, "Что возвращает функция в src/app.py?")
    assert status == 200, body
    assert "=== FILE src/app.py (lines 1-2 of 2) ===" in prompts(studio)[0]
    assert len(studio.llm.calls) == 1


def test_a_directory_shown_as_one_line_can_be_opened(studio):
    contents = {"README.md": "# CRM\n", "backend/main.go": "package main\n"}
    contents.update({f"frontend/src/components/Card{i}.tsx": "export {}\n" for i in range(400)})
    files(studio, contents)
    project, conversation = connected(studio)
    studio.answers("LIST: frontend/src/components", "READ: Card399.tsx",
                   "Фронтенд есть: 400 компонентов в frontend/src/components.")

    status, body = ask(studio, project, conversation, "Есть ли в проекте фронтенд?")
    assert status == 200, body
    first, second, third = prompts(studio)
    # 400 files do not fit the list's share of the prompt: the directory is one
    # line that says how many files it holds - so "is there a frontend" is
    # answerable from the first prompt, and the directory can be opened.
    assert "frontend/src/components/  [400 files here, not listed]" in first
    assert "backend/main.go (13 B)" in first
    assert "Card5.tsx" not in first
    # Opened, every one of the 400 is named: by names relative to the
    # directory, because their full paths would not all fit.
    assert ("=== DIRECTORY frontend/src/components (400 files; names below are relative "
            "to frontend/src/components/) ===") in second
    listed = second.split("=== DIRECTORY", 1)[1].split("=== END", 1)[0].splitlines()[1:]
    assert len(listed) == 400 and "Card5.tsx (10 B)" in listed and "Card399.tsx (10 B)" in listed
    # A name taken from that list opens the file it means.
    assert "=== FILE frontend/src/components/Card399.tsx (lines 1-1 of 1) ===" in third
    meta = body["messages"][-1]["meta"]["repository"]
    assert meta["listed"] == ["frontend/src/components"] and meta["files"] == 402
    assert meta["read"][-1]["path"] == "frontend/src/components/Card399.tsx"


def test_reading_is_bounded_and_a_model_that_never_answers_is_a_failure(studio):
    project, conversation = connected(studio)
    studio.answers("READ: src/app.py")             # for ever

    status, body = ask(studio, project, conversation, "Как устроен проект?")
    assert status == 502, body
    assert "модель не дала ответа" in body["error"]
    # A bounded number of requests, and the last of them said to answer.
    assert len(studio.llm.calls) == MAX_READ_ROUNDS + 1
    asked = prompts(studio)
    assert all("Otherwise answer the request now." in prompt for prompt in asked[:-1])
    assert "No more files can be opened for this answer" in asked[-1]
    assert "do not reply with READ: or LIST:" in asked[-1]
    # The request for files is never shown as an answer; the question is kept.
    assert [m["role"] for m in body["messages"]] == ["user", "system"]
    assert body["messages"][1]["meta"] == {"error": True}
    stored = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]
    assert [m["content"] for m in stored][0] == "Как устроен проект?"
    assert not any(m["role"] == "assistant" for m in stored)


def test_a_file_asked_for_twice_is_not_sent_twice(studio):
    project, conversation = connected(studio)
    studio.answers("READ: src/app.py", "READ: src/app.py", "Готово.")
    status, body = ask(studio, project, conversation, "Опиши код")
    assert status == 200, body
    last = prompts(studio)[-1]
    assert last.count("def add(a, b):") == 1
    assert "=== src/app.py is already shown above ===" in last


def test_a_long_file_is_read_in_parts(studio):
    text = "\n".join(f"value_{i} = {i}" for i in range(1, 6001)) + "\n"
    files(studio, {"README.md": "# x\n", "settings.py": text})
    project, conversation = connected(studio)
    studio.answers("READ: settings.py", "READ: settings.py:5990-6000", "value_6000 равно 6000.")

    status, body = ask(studio, project, conversation, "Чему равно последнее значение?")
    assert status == 200, body
    _first, second, third = prompts(studio)
    # The beginning, and how to ask for more.
    assert "=== FILE settings.py (lines 1-" in second and "of 6000; for more: READ:" in second
    assert "value_6000 = 6000" not in second
    assert "=== FILE settings.py (lines 5990-6000 of 6000) ===" in third
    assert "value_6000 = 6000" in third
    read = body["messages"][-1]["meta"]["repository"]["read"]
    assert read[-1] == {"path": "settings.py", "lines": [5990, 6000], "of": 6000}


# ----------------------------------------------------------- what is refused
def test_a_credential_file_is_never_fetched_and_never_reaches_the_model(studio):
    files(studio, {"README.md": "# x\n", ".env": "DATABASE_PASSWORD=do-not-send\n",
                   "deploy/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
                   ".env.example": "DATABASE_PASSWORD=\n"})
    project, conversation = connected(studio)
    studio.answers("READ: .env, deploy/id_rsa, .env.example",
                   "Файл .env не открывается в чате; в .env.example задан DATABASE_PASSWORD.")

    status, body = ask(studio, project, conversation, "Какие переменные окружения нужны?")
    assert status == 200, body
    second = prompts(studio)[1]
    assert "=== NOT READ .env: .env is named like a file that holds credentials" in second
    assert "=== NOT READ deploy/id_rsa:" in second
    assert "=== FILE .env.example" in second
    sent = json.dumps([call["body"] for call in studio.llm.calls], ensure_ascii=False)
    assert "do-not-send" not in sent and "OPENSSH PRIVATE KEY" not in sent
    # Not "fetched and held back": GitHub was never asked for them.
    assert sorted(fetched(studio, "contents")) == [
        f"/repos/{REPO}/contents/.env.example", f"/repos/{REPO}/contents/README.md"]
    assert [item["path"] for item in body["messages"][-1]["meta"]["repository"]["not_read"]] == [
        ".env", "deploy/id_rsa"]


def test_a_path_that_is_not_in_the_repository_is_refused_not_fetched(studio):
    project, conversation = connected(studio)
    studio.answers("READ: ../../etc/passwd, src/missing.py, docs",
                   "Таких файлов в репозитории нет.")
    status, body = ask(studio, project, conversation, "Покажи конфигурацию")
    assert status == 200, body
    second = prompts(studio)[1]
    assert "=== NOT READ ../../etc/passwd:" in second
    assert "=== NOT READ src/missing.py: there is no file src/missing.py in the list" in second
    assert fetched(studio, "contents") == [f"/repos/{REPO}/contents/README.md"]


def test_a_repository_github_refuses_is_said_so_and_the_turn_still_answers(studio):
    project, conversation = connected(studio)
    studio.github.force_status = 401               # the token was revoked
    studio.answers("Репозиторий сейчас не читается: GitHub отклонил доступ (401).")

    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 200, body
    prompt, system = prompts(studio)[0], rules(studio)[0]
    assert (f"Repository {REPO}, branch main. It could not be read for this request: "
            "github auth failed -> 401") in prompt
    assert "Files:" not in prompt and "=== FILE" not in prompt
    # The model is told not to describe files from memory, and not that it can read.
    assert "could not be read for this request" in system
    assert "REPOSITORY ACCESS" not in system
    assert body["messages"][-1]["meta"]["repository"] == {
        "full_name": REPO, "branch": "main", "error": "github auth failed -> 401"}
    assert len(studio.llm.calls) == 1


def test_a_project_with_no_repository_says_that_none_is_connected(studio):
    project = studio.project()
    conversation = studio.conversation(project, mode="ask")
    studio.github.paths.clear()
    studio.answers("К проекту не подключён репозиторий.")

    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 200, body
    assert "No repository is connected to this project." in prompts(studio)[0]
    assert "a repository has to be connected to the project first" in rules(studio)[0]
    assert "repository" not in body["messages"][-1]["meta"]
    assert studio.github.paths == [], "GitHub was asked about a project with no repository"


def test_github_failing_in_the_middle_of_reading_is_reported_per_file(studio):
    project, conversation = connected(studio)

    def respond(body):
        # The first reply asks for a file; GitHub breaks before it is fetched.
        if len(studio.llm.calls) == 1:
            studio.github.force_status = 502
            return 200, {"choices": [{"message": {"content": "READ: src/app.py"}}]}
        return 200, {"choices": [{"message": {"content": "Файл src/app.py не прочитан."}}]}

    studio.llm.respond = respond
    try:
        status, body = ask(studio, project, conversation, "Опиши код")
    finally:
        studio.llm.respond = None
    assert status == 200, body
    assert "=== NOT READ src/app.py: src/app.py could not be read from GitHub: github -> 502" \
        in prompts(studio)[1]
    assert body["messages"][-1]["meta"]["repository"]["not_read"][0]["path"] == "src/app.py"


# ------------------------------------------------------------------ the model
def small_model(studio, context_tokens: int) -> None:
    """Replace the provider with one whose model reads *context_tokens*."""
    studio.app.providers = ProviderCascade([GroqProvider(
        base_url=f"http://127.0.0.1:{studio.llm.server_port}", api_key="llm-key-value",
        model="small-model", sleep=lambda _seconds: None, context_tokens=context_tokens)])
    studio.app.chat = ChatResponder(studio.app.providers,
                                    dispatch_code=studio.app._chat_dispatch_code,
                                    start_autonomous=studio.app._chat_start_autonomous)


def test_a_model_too_small_for_the_file_list_answers_without_it_and_says_so(studio):
    project, conversation = connected(studio)
    small_model(studio, 3_400)
    studio.answers("Список файлов не поместился в окно модели.")

    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 200, body
    prompt = prompts(studio)[0]
    assert "It could not be read for this request: the file list does not fit" in prompt
    assert "Files:" not in prompt
    assert "does not fit" in body["messages"][-1]["meta"]["repository"]["error"]
    assert len(studio.llm.calls) == 1


def test_a_provider_that_refuses_the_size_is_asked_again_without_the_list(studio):
    project, conversation = connected(studio)
    studio.llm.script = [
        {"status": 413, "payload": {"error": {"message": "Request too large"}}},
        {"payload": {"choices": [{"message": {"content": "Ответ без списка файлов."}}]}}]
    studio.llm.index = 0
    studio.llm.calls.clear()

    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Ответ без списка файлов."
    first, second = prompts(studio)
    assert "Files:\n" in first and "Files:\n" not in second
    assert "was refused as too large" in second
    assert "too large" in body["messages"][-1]["meta"]["repository"]["error"]


def test_files_that_no_longer_fit_are_taken_back_and_the_answer_is_still_given(studio):
    """The model asked for more than its provider then accepted. What it opened
    last is dropped, it is told so, and it answers from what fitted before."""
    project, conversation = connected(studio)
    studio.llm.script = [
        {"payload": {"choices": [{"message": {"content": "READ: src/app.py"}}]}},
        {"status": 413, "payload": {"error": {"message": "Request too large"}}},
        {"payload": {"choices": [{"message": {"content": "Отвечаю по README."}}]}}]
    studio.llm.index = 0
    studio.llm.calls.clear()

    status, body = ask(studio, project, conversation, "Опиши код")
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Отвечаю по README."
    _first, refused, last = prompts(studio)
    assert "def add(a, b):" in refused and "def add(a, b):" not in last
    assert "=== NOT READ src/app.py: with it the request no longer fits" in last
    assert "No more files can be opened for this answer" in last
    meta = body["messages"][-1]["meta"]["repository"]
    assert [item["path"] for item in meta["read"]] == ["README.md"]
    assert [item["path"] for item in meta["not_read"]] == ["src/app.py"]


def test_a_provider_outage_is_a_failure_not_an_invented_answer(studio):
    project, conversation = connected(studio)
    studio.llm.script = [{"status": 500, "payload": {}}]
    studio.llm.index = 0
    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 502, body
    assert "no orchestrator provider succeeded" in body["error"]
    assert [m["role"] for m in body["messages"]] == ["user", "system"]


# ---------------------------------------------------------------- the identity
def test_the_repository_is_read_with_the_identity_the_project_was_given(studio):
    account = studio.account()
    project, conversation = connected(studio, account_id=account)
    studio.answers("Вижу.")
    assert ask(studio, project, conversation, "Что в репозитории?")[0] == 200
    seen = {(header or "").split()[-1] for header in studio.github.auth_headers}
    assert seen == {ACCOUNT_TOKEN}

    # A project with no account reads with the server's token.
    reader._STORE.clear()
    other, chat = connected(studio, account_id=None)
    assert ask(studio, other, chat, "Что в репозитории?")[0] == 200
    seen = {(header or "").split()[-1] for header in studio.github.auth_headers}
    assert seen == {SERVER_GITHUB_TOKEN}


def test_an_account_that_cannot_see_the_repository_gets_nothing_kept_from_another(studio):
    """One project read the repository; what was kept from that read is not
    shown to a project whose own identity GitHub refuses."""
    project, conversation = connected(studio)
    studio.answers("Вижу.")
    assert ask(studio, project, conversation, "Что в репозитории?")[0] == 200

    account = studio.account()
    other, chat = connected(studio, account_id=account)
    studio.github.token_repos[ACCOUNT_TOKEN] = set()         # access was taken away
    studio.answers("Не читается.")
    status, body = ask(studio, other, chat, "Что в репозитории?")
    assert status == 200, body
    prompt = prompts(studio)[0]
    assert "It could not be read for this request" in prompt and "(404)" in prompt
    assert "src/app.py" not in prompt and "Demo project" not in prompt


def test_the_branch_of_the_project_is_the_one_read(studio):
    project, conversation = connected(studio, branch="dev")
    studio.answers("Вижу ветку dev.")
    status, body = ask(studio, project, conversation, "Что в репозитории?")
    assert status == 200, body
    assert f"Repository {REPO}, branch dev, at commit" in prompts(studio)[0]
    assert body["messages"][-1]["meta"]["repository"]["branch"] == "dev"


# ------------------------------------------------------- always the latest code
def test_each_question_is_answered_from_the_head_of_the_branch_as_it_is_then(studio):
    project, conversation = connected(studio)
    studio.answers("Фронтенда нет.")
    assert ask(studio, project, conversation, "Есть ли фронтенд?")[0] == 200
    assert "web/index.html" not in prompts(studio)[0]

    # Somebody pushes. The very next question sees the new commit and its files.
    studio.github.push(REPO, "0a3db0b9a41d671cb6ab99c37f09de8842f22d8a", "Add the frontend")
    studio.github.tree.append({"path": "web/index.html", "type": "blob", "size": 512})
    studio.github.contents["web/index.html"] = "<!doctype html>\n<title>CRM</title>\n"
    studio.answers("READ: web/index.html", "Теперь есть: web/index.html.")
    status, body = ask(studio, project, conversation, "А теперь?")
    assert status == 200, body
    first, second = prompts(studio)
    assert "at commit 0a3db0b9a41d" in first and "web/index.html (512 B)" in first
    assert "<title>CRM</title>" in second
    assert body["messages"][-1]["meta"]["repository"]["revision"] == \
        "0a3db0b9a41d671cb6ab99c37f09de8842f22d8a"


def test_an_unchanged_repository_is_listed_once_but_its_head_is_asked_every_time(studio):
    project, conversation = connected(studio)
    studio.answers("Ответ.")
    for question in ("Первый вопрос", "Второй вопрос", "Третий вопрос"):
        assert ask(studio, project, conversation, question)[0] == 200
    assert len(fetched(studio, "commits")) == 3
    assert len(fetched(studio, "git/trees")) == 1
    assert len(fetched(studio, "contents")) == 1               # the README, once


# --------------------------------------------------------------- other modes
def test_an_analysis_turn_has_the_repository_and_the_runs(studio):
    project, conversation = connected(studio)
    studio.answers("READ: src/app.py", "Разбор: add складывает.")
    status, body = ask(studio, project, conversation, "Проанализируй код", mode="analysis")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    second = prompts(studio)[1]
    assert "Autonomous runs of this project (real state):" in second
    assert "def add(a, b):" in second
    assert "REPOSITORY ACCESS" in rules(studio)[0]


def test_a_plan_is_drawn_with_the_file_list_in_view(studio):
    project, conversation = connected(studio)
    studio.answers(json.dumps({"tasks": [
        {"id": "a", "title": "Покрыть add тестами", "description": "В tests/test_app.py.",
         "acceptance": ["готово"], "dependencies": []}]}))
    status, body = ask(studio, project, conversation, "Спланируй покрытие тестами", mode="plan")
    assert status == 200, body
    prompt = prompts(studio)[0]
    assert f"Repository {REPO}, branch main, at commit abc123" in prompt
    assert "src/app.py (340 B)" in prompt
    assert len(studio.llm.calls) == 1
    assert studio.llm.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_a_turn_that_starts_work_does_not_read_the_repository_itself(studio):
    """The agents read it. The turn that starts them does not spend a model's
    window on it, and does not ask GitHub for the file list."""
    project, conversation = connected(studio)
    studio.answers(json.dumps({"tasks": [
        {"id": "a", "title": "Шаг", "description": "Сделать.", "acceptance": ["готово"],
         "dependencies": []}]}))
    status, body = ask(studio, project, conversation, "Исправь функцию add", mode="autonomous")
    assert status == 200, body
    assert fetched(studio, "git/trees") == []
    assert not any("Files:\n" in prompt for prompt in prompts(studio))


@pytest.mark.parametrize("mode, told, not_told", [
    # An order starts a run by itself.
    ("auto", "say that it starts with such an order", "«Запускай» starts it"),
    ("autonomous", "say that it starts with such an order", "the mode is to be switched"),
    # An order gets a plan; a launch command starts it.
    ("manual", "say that an order gets a plan and that «Запускай» starts it",
     "say that it starts with such an order"),
    # The mode only answers, an order included: the mode is to be switched.
    ("ask", "say that the mode is to be switched first",
     "say that it starts with such an order"),
    ("analysis", "say that the mode is to be switched first",
     "say that it starts with such an order"),
])
def test_the_assistant_is_told_how_work_is_started_in_the_mode_it_is_in(
        studio, mode, told, not_told):
    """It used to answer "я не могу запускать прогоны" to a person who asked
    for one. A reply changes nothing, but the system it speaks for can - and
    how depends on the mode: told the same thing everywhere, it sent a person
    in «Вопрос» to write an order that «Вопрос» only answers."""
    project, conversation = connected(studio)
    studio.answers("Могу.")
    assert ask(studio, project, conversation, "Ты можешь сам переделать фронтенд?",
               mode=mode)[0] == 200
    system = rules(studio)[0]
    assert "NEXUS itself can do the work" in system
    assert "never say that changes or runs are impossible" in system
    assert told in system and not_told not in system


def test_a_question_in_manual_mode_is_answered_from_the_repository_too(studio):
    """«Вручную» answers a question itself, apart from the other modes; it
    reads the repository for it like they do."""
    project, conversation = connected(studio)
    studio.answers("READ: src/app.py", "Да, вижу: код в src/app.py, там функция add.")
    status, body = ask(studio, project, conversation, "Ты вообще видишь репозиторий и проект?",
                       mode="manual")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["content"] == "Да, вижу: код в src/app.py, там функция add."
    assert f"Repository {REPO}, branch main, at commit abc123" in prompts(studio)[0]
    assert "def add(a, b):" in prompts(studio)[1]
    assert "REPOSITORY ACCESS" in rules(studio)[0]
    assert reply["meta"]["repository"]["full_name"] == REPO
    assert [item["path"] for item in reply["meta"]["repository"]["read"]] == [
        "README.md", "src/app.py"]
    # Nothing was planned and nothing started.
    assert studio.api.get(f"/api/v1/projects/{project}/runs?archived=1")[1]["runs"] == []
