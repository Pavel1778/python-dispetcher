"""A short order takes the conversation with it; a run is never planned from one word.

What was seen in production, in one conversation of a project with a
repository connected:

* the person described what they wanted (redo the frontend, make it look like
  another product), was told "I cannot start a run", and then wrote
  «Ты авто и можешь сделать прогон. Делай». A run was started - from that
  sentence alone. The planner had nothing to plan from, invented one task
  ("Run the process"), and an agent carried it out;
* «Привет» in a conversation set to "Автономно" started a run;
* a message that said in so many words "делать это в режиме прогона" was
  answered with the question "explain in the chat, or start a run?", and the
  answer to that question - «Сразу работу и прогон» - was read as a remark and
  answered by a model: "I cannot start a run".

The first half of this file is the pure parts (no database): what goes into
the task, how the specification's index reads it, what the intent router
decides. The second half drives the real server over HTTP on the fakes and
asserts what a person would see: which messages start a run, and what the
planner and the agents are given.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_chat import (  # noqa: E402
    TASK_CONTEXT_CHARS,
    TASK_CONTEXT_MAX_CHARS,
    TASK_CONTEXT_MESSAGES,
    task_with_context,
)
from orchestration.nexus_intent import (  # noqa: E402
    AUTONOMOUS,
    CHAT,
    clarification_choice,
    classify,
    effective_mode,
)
from orchestration.nexus_specs import (  # noqa: E402
    CONTEXT_MARKER,
    build_index,
    goal_label,
    split_context,
)
from tests.test_studio_api import DSN, REPO, TOKEN, studio_stack  # noqa: E402


def said(role: str, content: str, **meta) -> dict:
    return {"role": role, "content": content, "meta": meta}


WISH = "Я бы хотел кардинально изменить фронтенд, чтобы он выглядел как elitea"
ADVICE = ("Возможные направления:\n\n1. **Покрытие тестами**: оцените покрытие кода.\n"
          "2. **CI/CD**: убедитесь, что все этапы работают.\n\n"
          "| Что | Зачем |\n|---|---|\n| Линтер | Нужно добавить линтер |")
TALK = [said("user", WISH), said("assistant", ADVICE, mode="ask")]


# ====================================================== the task and its context
def test_a_short_order_takes_the_conversation_with_it_word_for_word():
    task, attached = task_with_context("Делай", TALK)
    assert attached == 2
    message, conversation = split_context(task)
    assert message == "Делай"
    assert conversation.startswith(CONTEXT_MARKER + "\n")
    # What was said, in the order it was said, by whom - not a summary of it.
    assert f"[Пользователь]\n{WISH}\n\n[Ассистент]\n{ADVICE}" in conversation
    assert task.endswith(ADVICE)
    # The note says how to read it: the message is the order.
    assert "Задание - то, что сказано в сообщении выше" in conversation
    assert "Ответы ассистента - справка" in conversation


def test_a_document_and_a_first_message_are_the_task_as_they_stand():
    document = "Реализуй модуль отчётов. " * 40                 # longer than a remark
    assert len(document) > TASK_CONTEXT_MAX_CHARS
    assert task_with_context(document, TALK) == (document, 0)
    assert task_with_context("Исправь ошибку", []) == ("Исправь ошибку", 0)
    assert task_with_context("Исправь ошибку", None) == ("Исправь ошибку", 0)
    # Exactly as sent - the caller stores it byte for byte - when nothing is attached.
    assert task_with_context("  Исправь ошибку \n", [])[0] == "  Исправь ошибку \n"
    # Right at the limit it is still a remark.
    edge = "я" * TASK_CONTEXT_MAX_CHARS
    assert task_with_context(edge, TALK)[1] == 2
    assert task_with_context(edge + "я", TALK)[1] == 0


def test_only_what_people_and_the_assistant_said_is_attached():
    history = [
        said("user", WISH),
        said("tool", "start_run → 1f0c", tool="start_run"),
        said("system", "Не удалось получить ответ: провайдер недоступен", error=True),
        said("assistant", "Автономный прогон запущен: репозиторий `a/b`.", mode="autonomous",
             run_id="1f0c"),
        said("assistant", "Задача передана OpenHands.", mode="code", workflow_id="77"),
        said("assistant", "", mode="ask"),
        said("assistant", "## Отчёт автономного прогона\n\nИзменений нет.", mode="autonomous",
             run_id="1f0c", report=True),
    ]
    task, attached = task_with_context("Исправь замечания из отчёта", history)
    conversation = split_context(task)[1]
    # The wish and the report: "исправь замечания" is about the report.
    assert attached == 2
    assert WISH in conversation and "## Отчёт автономного прогона" in conversation
    for left_out in ("start_run", "Не удалось получить ответ", "прогон запущен",
                     "передана OpenHands"):
        assert left_out not in conversation


def test_sending_the_same_message_again_gives_the_same_task():
    """A retry after a timeout, a double click: the second send must be
    recognisable as the task that is already running. Between the two sends
    the transcript gained the message itself and the reply that work started."""
    first, _ = task_with_context("Делай", TALK)
    after = TALK + [
        said("user", "Делай", spec={"id": "s1"}),
        said("tool", "start_run → 1f0c", tool="start_run"),
        said("assistant", "Автономный прогон запущен: репозиторий `a/b`.", mode="autonomous",
             run_id="1f0c"),
    ]
    assert task_with_context("Делай", after)[0] == first
    # And a third time, after "this text is already running".
    after.append(said("user", "Делай"))
    after.append(said("assistant", "Этот текст уже выполняется.", mode="autonomous",
                      existing=True))
    assert task_with_context(" Делай ", after)[0] == first
    # Something new was said: it is a new task.
    after.append(said("user", "И добавь тёмную тему"))
    assert task_with_context("Делай", after)[0] != first


def test_what_is_attached_is_bounded():
    many = [said("user", f"Сообщение номер {i}") for i in range(40)]
    task, attached = task_with_context("Делай", many)
    assert attached == TASK_CONTEXT_MESSAGES
    assert "Сообщение номер 39" in task and "Сообщение номер 28" in task
    assert "Сообщение номер 27" not in task, "the latest messages are the ones kept"
    assert task.index("Сообщение номер 28") < task.index("Сообщение номер 39")

    # One enormous message is cut, and says by how much; the total is bounded.
    huge = [said("assistant", "я" * 50_000, mode="ask"), said("user", "б" * 3_000)] * 4
    task, attached = task_with_context("Делай", huge)
    assert "сообщение обрезано, ещё 46000 символов]" in task
    assert len(split_context(task)[1]) < TASK_CONTEXT_CHARS + 2_000
    assert attached < 8


def test_a_quoted_task_cannot_nest_a_second_context():
    earlier, _ = task_with_context("Делай", TALK)
    history = [said("user", "Вот что ушло в прошлый раз:\n" + earlier)]
    task, _ = task_with_context("Повтори это", history)
    assert task.count(CONTEXT_MARKER) == 1
    assert split_context(task)[0] == "Повтори это"


# ------------------------------------------------- how the specification reads it
def test_no_plan_is_held_to_a_sentence_of_the_attached_conversation():
    """A list in somebody's answer is not a list of demands. Without this the
    planner was made to cover "оцените покрытие кода" with a task - advice the
    assistant gave, which nobody asked to carry out."""
    task, _ = task_with_context("Исправь ошибку в форме входа", TALK)
    index = build_index(task)
    obligations = [r for r in index.requirements if r.normative]
    assert [r.excerpt for r in obligations] == ["Исправь ошибку в форме входа"]
    # The conversation is still there, piece by piece, for whoever reads the
    # specification - as background.
    background = [r for r in index.requirements if not r.normative]
    assert any("Покрытие тестами" in r.excerpt for r in background)
    assert any("Нужно добавить линтер" in r.excerpt for r in background)

    # The same list written as the task itself is a list of demands, as before.
    assert sum(r.normative for r in build_index(ADVICE).requirements) >= 3


def test_the_marker_counts_only_as_a_line_of_its_own():
    inline = f"Объясни, что значит {CONTEXT_MARKER} в тексте.\n\n- Нужно добавить линтер"
    assert split_context(inline) == (inline, None)
    assert any(r.normative for r in build_index(inline).requirements)
    assert split_context("Делай")[1] is None


def test_a_goal_is_labelled_by_the_message_not_by_the_conversation():
    task, _ = task_with_context("Делай", TALK)
    assert goal_label(task) == "Делай"
    assert goal_label("Исправь ошибку") == "Исправь ошибку"


# =========================================================== the intent router
@pytest.mark.parametrize("text", [
    "Делай", "делай!", "Ну делай", "Да, делай", "Приступай", "Давай, приступай",
    "Действуй", "Запускай", "Ок, запускай прогон", "Поехали", "В работу", "Сделай это",
    "go ahead", "do it", "Proceed", "ok, go",
])
def test_a_go_ahead_is_an_order_when_something_was_said_before_it(text):
    with_context = classify(text, has_context=True)
    assert with_context.intent == AUTONOMOUS and with_context.starts_work, text
    alone = classify(text)
    assert not alone.starts_work, text
    # With nothing before it, it is answered even in a conversation set to
    # "Автономно": there is nothing to go ahead with.
    assert effective_mode("autonomous", classify(text, mode="autonomous")) == "ask", text


@pytest.mark.parametrize("text", [
    "Да", "Давай", "Ок", "Хорошо", "Спасибо", "Делай?", "А если делать так?",
    "Не делай", "Делай как знаешь, но сначала объясни",
])
def test_agreement_and_questions_are_not_a_go_ahead(text):
    assert not classify(text, has_context=True).starts_work, text


@pytest.mark.parametrize("text", [
    "Мне нужно чтобы ты проанализировал проект и решил, что стоить сделать. Также однозначно "
    "делать это в режиме прогона (нужно улучшить и полностью переделать фронтенд)",
    "Проанализируй проект и сделай это прогоном",
    "Сначала анализируем проект, затем запускаем прогон",
    "Проверь логи и запусти автономный прогон",
    "Look at the tests and do it as a run",
    "Analyse the project, then start a run",
])
def test_a_message_that_asks_for_a_run_in_words_is_an_order(text):
    intent = classify(text, mode="auto")
    assert intent.intent == AUTONOMOUS and not intent.ambiguous, text


@pytest.mark.parametrize("text", [
    "Можно ли сделать это прогоном?",                 # a question
    "Почему прогон ломается?",
    "Только не в режиме прогона, просто объясни",     # a refusal
    "Без запуска прогона опиши, что изменилось бы",
    "Не надо запускать прогон, давай обсудим",
    "Нужно остановить прогон",                        # about a run, not for one
    "Проанализируй последний прогон",
])
def test_a_message_about_a_run_is_not_an_order_for_one(text):
    assert not classify(text, mode="auto").starts_work, text


def test_a_greeting_is_answered_even_in_a_conversation_set_to_autonomous():
    for text in ("Привет", "Здравствуйте!", "Спасибо", "ок"):
        intent = classify(text, mode="autonomous")
        assert intent.intent == CHAT
        assert effective_mode("autonomous", intent) == "ask", text
    # A stated goal with no order verb still runs there: the mode was chosen.
    assert effective_mode("autonomous", classify("Цель: 25 задач", mode="autonomous")) \
        == "autonomous"


@pytest.mark.parametrize("text, choice", [
    ("Сразу работу и прогон", "work"), ("Прогон", "work"), ("запускай", "work"),
    ("Выполнить работу", "work"), ("делай", "work"), ("второе", "work"), ("run it", "work"),
    ("В чате", "chat"), ("обсудим", "chat"), ("Объясни", "chat"), ("первое", "chat"),
    ("не надо прогон, объясни", "chat"), ("just explain", "chat"),
    ("Да", None), ("Не знаю", None), ("А что лучше?", None),
    ("объясни, а потом запусти прогон", None), ("", None),
])
def test_the_answer_to_the_clarifying_question_is_read_for_what_it_names(text, choice):
    assert clarification_choice(text) == choice


# ==================================================== the server, over real HTTP
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run chat tests")

PLAN = json.dumps({"tasks": [
    {"id": "frontend", "title": "Переделать фронтенд", "description": "По образцу elitea.",
     "acceptance": ["интерфейс обновлён"], "dependencies": []}]})


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def chat(studio, mode="auto") -> tuple[str, str]:
    project = studio.project()
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository", {"full_name": REPO})
    assert status == 200, body
    return project, studio.conversation(project, mode=mode)


def send(studio, project, conversation, text, mode=None):
    body = {"text": text}
    if mode:
        body["mode"] = mode
    return studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", body)


def runs(studio, project) -> list[dict]:
    return studio.api.get(f"/api/v1/projects/{project}/runs?include_hidden=1")[1]["runs"]


def spec_text(studio, project, spec_id) -> str:
    request = urllib.request.Request(
        f"{studio.api.base}/api/v1/projects/{project}/specs/{spec_id}/content",
        headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def planning_prompts(studio) -> list[str]:
    """The prompts that asked for a plan (JSON), as opposed to an answer."""
    return [call["body"]["messages"][-1]["content"] for call in studio.llm.calls
            if "response_format" in call["body"]]


@needs_db
def test_do_it_after_a_discussion_starts_a_run_planned_from_the_discussion(studio):
    project, conversation = chat(studio)
    studio.answers("Фронтенд лежит в web/. Чтобы я его переделал, напишите «делай».", PLAN)

    status, body = send(studio, project, conversation, WISH)
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "ask"
    assert runs(studio, project) == []

    status, body = send(studio, project, conversation, "Делай")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "tool", "assistant"]
    user, _tool, reply = body["messages"]
    # The transcript shows what was typed; that the conversation went into the
    # task is recorded with the message and said in the reply.
    assert user["content"] == "Делай"
    assert user["meta"]["spec"]["context_messages"] == 2
    assert "Автономный прогон запущен" in reply["content"]
    assert "в задание вошла и предыдущая переписка этого чата (сообщений: 2)" in reply["content"]
    assert reply["meta"]["context_messages"] == 2

    # The task, as stored: the message, then the conversation, word for word.
    text = spec_text(studio, project, user["meta"]["spec"]["id"])
    message, conversation_part = split_context(text)
    assert message == "Делай"
    assert f"[Пользователь]\n{WISH}" in conversation_part
    assert "[Ассистент]\nФронтенд лежит в web/." in conversation_part

    # The planner was given it - the wish is in the request for the plan - and
    # was told how to read it.
    asked = planning_prompts(studio)
    assert len(asked) == 1 and WISH in asked[0] and "Делай" in asked[0]
    system = [call["body"]["messages"][0]["content"] for call in studio.llm.calls
              if "response_format" in call["body"]][0]
    assert "The message is the order; the conversation is there to say what the order is about" \
        in system

    # One run, with the planned task; the goal is labelled by the message.
    started = runs(studio, project)
    assert len(started) == 1 and str(started[0]["id"]) == reply["meta"]["run_id"]
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{reply['meta']['run_id']}")[1]
    assert [task["title"] for task in detail["tasks"]] == ["Переделать фронтенд"]
    goals = studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"]
    assert [goal["text"] for goal in goals] == ["Делай"]
    # The run is at work: its OpenHands conversations are kept, and the detail
    # says that deleting them is switched on and not yet due.
    assert detail["cleanup"] == {"enabled": True, "due_at": None, "requested": 0, "deleted": 0,
                                 "missing": 0, "abandoned": 0, "last_error": None}


@needs_db
def test_the_same_go_ahead_sent_again_starts_no_second_run(studio):
    project, conversation = chat(studio)
    studio.answers("Понял задачу.", PLAN)
    assert send(studio, project, conversation, WISH)[0] == 200
    status, first = send(studio, project, conversation, "Делай")
    assert status == 200, first
    planned = len(planning_prompts(studio))

    status, second = send(studio, project, conversation, "Делай")
    assert status == 200, second
    reply = second["messages"][-1]
    assert reply["meta"].get("existing") is True
    assert reply["meta"]["run_id"] == first["messages"][-1]["meta"]["run_id"]
    assert "второй не запущен" in reply["content"]
    assert second["messages"][0]["meta"]["spec"]["id"] == first["messages"][0]["meta"]["spec"]["id"]
    assert len(runs(studio, project)) == 1
    assert len(planning_prompts(studio)) == planned, "a second plan was requested"


@needs_db
def test_a_go_ahead_with_nothing_before_it_is_answered_not_run(studio):
    for mode in ("auto", "autonomous"):
        project, conversation = chat(studio, mode=mode)
        studio.answers("Что именно сделать? Опишите задачу.")
        status, body = send(studio, project, conversation, "Делай")
        assert status == 200, body
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][-1]["content"] == "Что именно сделать? Опишите задачу."
        assert runs(studio, project) == [], mode
        assert planning_prompts(studio) == []

        # After a greeting there is still nothing to go ahead with.
        assert send(studio, project, conversation, "Привет")[0] == 200
        assert send(studio, project, conversation, "Делай")[0] == 200
        assert runs(studio, project) == [], mode


@needs_db
def test_a_greeting_in_a_conversation_set_to_autonomous_starts_nothing(studio):
    """Run b06ee39b in production was started by «Привет»."""
    project, conversation = chat(studio, mode="autonomous")
    studio.answers("Привет! Чем помочь с проектом?")
    status, body = send(studio, project, conversation, "Привет", mode="autonomous")
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Привет! Чем помочь с проектом?"
    assert body["messages"][-1]["meta"]["mode"] == "ask"
    assert runs(studio, project) == []
    assert studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"] == []
    assert studio.cloud.cloud.conversations == {}


@needs_db
def test_asking_for_a_run_in_words_starts_one_without_a_question_back(studio):
    project, conversation = chat(studio)
    studio.answers("Фронтенд есть, в web/.", PLAN)
    assert send(studio, project, conversation, WISH)[0] == 200

    status, body = send(
        studio, project, conversation,
        "Мне нужно чтобы ты проанализировал проект и решил, что стоить сделать. Также "
        "однозначно делать это в режиме прогона (нужно улучшить и полностью переделать "
        "фронтенд)")
    assert status == 200, body
    reply = body["messages"][-1]
    assert not reply["meta"].get("clarify"), "the router asked what it had just been told"
    assert reply["meta"]["mode"] == "autonomous" and reply["meta"]["run_id"]
    assert len(runs(studio, project)) == 1
    # It is short, so it took the earlier wish with it.
    assert WISH in planning_prompts(studio)[0]


@needs_db
def test_the_answer_to_the_clarifying_question_starts_the_work(studio):
    """«Сразу работу и прогон» used to be answered by a model: "I cannot start
    a run". It is the answer to the question the router itself had asked."""
    project, conversation = chat(studio)
    status, body = send(studio, project, conversation, "Проверь и исправь тесты формы входа")
    assert status == 200, body
    assert body["messages"][-1]["meta"].get("clarify") is True
    assert studio.llm.calls == [] and runs(studio, project) == []

    studio.answers(PLAN)
    status, body = send(studio, project, conversation, "Сразу работу и прогон")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["meta"]["mode"] == "autonomous" and reply["meta"]["run_id"], reply
    assert len(runs(studio, project)) == 1
    # What is to be done is the message the question was about.
    asked = planning_prompts(studio)
    assert len(asked) == 1 and "Проверь и исправь тесты формы входа" in asked[0]
    text = spec_text(studio, project, body["messages"][0]["meta"]["spec"]["id"])
    assert split_context(text)[0] == "Сразу работу и прогон"
    assert "[Пользователь]\nПроверь и исправь тесты формы входа" in text


@needs_db
def test_choosing_the_chat_answers_the_message_that_was_asked_about(studio):
    project, conversation = chat(studio)
    assert send(studio, project, conversation, "Проверь и исправь тесты формы входа")[0] == 200
    studio.answers("Тесты формы входа лежат в tests/test_app.py.")

    status, body = send(studio, project, conversation, "В чате")
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Тесты формы входа лежат в tests/test_app.py."
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    assert runs(studio, project) == []
    # The model was asked the earlier message, not the two words that chose.
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "Request:\nПроверь и исправь тесты формы входа" in prompt

    # A reply that names neither is not an answer to the question: it is
    # routed like any other message.
    other = studio.conversation(project, mode="auto")
    assert send(studio, project, other, "Проверь и исправь тесты формы входа")[0] == 200
    studio.answers("Пожалуйста.")
    status, body = send(studio, project, other, "Спасибо")
    assert status == 200 and body["messages"][-1]["content"] == "Пожалуйста."
    assert runs(studio, project) == []


@needs_db
def test_a_long_order_is_the_task_exactly_as_it_was_sent(studio):
    project, conversation = chat(studio)
    studio.answers("Понял.", PLAN)
    assert send(studio, project, conversation, WISH)[0] == 200
    document = ("Реализуй новый фронтенд.\n\n"
                + "\n".join(f"{n}. Экран номер {n} должен быть свёрстан заново." for n in
                            range(1, 31)))
    assert len(document) > TASK_CONTEXT_MAX_CHARS

    status, body = send(studio, project, conversation, document)
    assert status == 200, body
    user = body["messages"][0]
    assert "context_messages" not in user["meta"]["spec"]
    assert "context_messages" not in body["messages"][-1]["meta"]
    assert "переписка" not in body["messages"][-1]["content"]
    assert spec_text(studio, project, user["meta"]["spec"]["id"]) == document
    assert WISH not in planning_prompts(studio)[0]


@needs_db
def test_a_short_order_in_code_mode_takes_the_conversation_too(studio):
    project, conversation = chat(studio, mode="code")
    studio.answers("Понял задачу.")
    assert send(studio, project, conversation, WISH, mode="ask")[0] == 200

    status, body = send(studio, project, conversation, "Сделай это", mode="code")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["meta"]["mode"] == "code" and reply["meta"]["context_messages"] == 2
    assert "вошла и предыдущая переписка" in reply["content"]
    # The workflow's goal is the message and the conversation.
    with studio.app.store._tx() as cur:
        cur.execute("SELECT goal FROM workflows WHERE id=%s", (reply["meta"]["workflow_id"],))
        goal = cur.fetchone()["goal"]
    assert split_context(goal)[0] == "Сделай это" and WISH in goal
    # The transcript shows the two words, and so does the card of the work.
    assert body["messages"][0]["content"] == "Сделай это"
    status, detail = studio.api.get(
        f"/api/v1/projects/{project}/workflows/{reply['meta']['workflow_id']}")
    assert status == 200, detail
    assert detail["workflow"]["goal"] == "Сделай это"
    # What becomes of its OpenHands conversations is said there too: nothing
    # yet, the workflow is at work.
    assert detail["cleanup"]["enabled"] is True and detail["cleanup"]["due_at"] is None

    # Stopped by a person, it is over for good: from then on its conversations
    # are owed a deletion a day later, and the card says when.
    assert studio.api.post(f"/api/workflows/{reply['meta']['workflow_id']}/cancel", {})[0] == 200
    detail = studio.api.get(
        f"/api/v1/projects/{project}/workflows/{reply['meta']['workflow_id']}")[1]
    assert detail["workflow"]["state"] == "cancelled" and detail["cleanup"]["due_at"]


# ------------------------------------------------------------ the «Вручную» mode
# In «Вручную» an order gets a plan and only a launch command starts the run
# (``nexus_workflow``, ``tests/test_chat_workflow.py``). What this file is
# about holds there too: a short order is no more a task in that mode than in
# any other, and a go-ahead must not turn the plan a person has just read into
# another one.
def workflow_of(studio, project, conversation) -> dict:
    return studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]


def task_of_run(studio, run_id) -> str:
    """The stored task of a run: what its agents are given to read."""
    with studio.app.store._tx() as cur:
        cur.execute("SELECT s.content FROM nexus_runs r JOIN nexus_goals g ON g.id = r.goal_id "
                    "JOIN nexus_specs s ON s.id = g.spec_id WHERE r.id=%s", (run_id,))
        return cur.fetchone()["content"]


@needs_db
def test_do_it_in_manual_mode_gets_a_plan_made_from_the_discussion(studio):
    project, conversation = chat(studio, mode="manual")
    studio.answers("Фронтенд лежит в web/.", PLAN)
    status, body = send(studio, project, conversation, WISH)
    assert status == 200 and body["messages"][-1]["meta"]["mode"] == "ask", body

    status, body = send(studio, project, conversation, "Делай")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    reply = body["messages"][-1]
    # A plan, not a run: the mode waits for its launch command.
    assert reply["meta"]["mode"] == "plan" and reply["meta"]["awaiting_approval"] is True
    assert "Предлагаемый план" in reply["content"] and "Переделать фронтенд" in reply["content"]
    assert runs(studio, project) == [] and studio.cloud.cloud.conversations == {}
    assert workflow_of(studio, project, conversation)["workflow_state"] == "AWAITING_APPROVAL"
    # The plan was asked for with the wish in view, and the reply says that
    # the conversation went into the task.
    asked = planning_prompts(studio)
    assert len(asked) == 1 and WISH in asked[0] and "Делай" in asked[0]
    assert CONTEXT_MARKER in asked[0]
    assert reply["meta"]["context_messages"] == 2
    assert "в задание вошла и предыдущая переписка этого чата (сообщений: 2)" in reply["content"]

    # The launch command starts that plan. The task its agents read is the
    # message with the conversation; the goal is labelled by the message.
    status, body = send(studio, project, conversation, "Запускай")
    assert status == 200, body
    started = runs(studio, project)
    assert len(started) == 1
    message, attached = split_context(task_of_run(studio, started[0]["id"]))
    assert message == "Делай" and f"[Пользователь]\n{WISH}" in attached
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{started[0]['id']}")[1]
    assert [task["title"] for task in detail["tasks"]] == ["Переделать фронтенд"]
    goals = studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"]
    assert [goal["text"] for goal in goals] == ["Делай"]


@needs_db
@pytest.mark.parametrize("text", ["Делай", "Сделай", "Да, делай", "Ладно, запускай"])
def test_a_go_ahead_that_is_not_a_launch_command_leaves_the_offered_plan_alone(studio, text):
    """With a plan waiting, «Делай» is not one of the launch commands, and it
    is not a new order either. It used to be planned again - from one word -
    and the plan the person had read was replaced. Now nothing is asked of a
    model, the plan is the same, and the reply names the command."""
    project, conversation = chat(studio, mode="manual")
    studio.answers(PLAN)
    assert send(studio, project, conversation, "Реализуй вход по ссылке из письма")[0] == 200
    offered = workflow_of(studio, project, conversation)
    assert offered["workflow_state"] == "AWAITING_APPROVAL" and offered["plan_hash"]
    asked = len(studio.llm.calls)

    status, body = send(studio, project, conversation, text)
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["meta"] == {"mode": "ask"}
    assert "План остаётся на подтверждении" in reply["content"]
    assert "«Запускай»" in reply["content"]
    assert len(studio.llm.calls) == asked, "a model was asked to plan a go-ahead"
    after = workflow_of(studio, project, conversation)
    assert after["plan_hash"] == offered["plan_hash"]
    assert after["workflow_state"] == "AWAITING_APPROVAL"
    assert after["workflow_plan"] == offered["workflow_plan"]
    assert runs(studio, project) == [] and studio.cloud.cloud.conversations == {}

    # The command itself still starts exactly that plan, once.
    status, body = send(studio, project, conversation, "Запускай")
    assert status == 200, body
    started = runs(studio, project)
    assert len(started) == 1
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{started[0]['id']}")[1]
    assert [task["title"] for task in detail["tasks"]] == ["Переделать фронтенд"]


@needs_db
def test_a_new_order_in_manual_mode_still_replaces_the_offered_plan(studio):
    """Only a go-ahead leaves the plan alone. A message that names work is a
    change of requirements: a new plan, to be confirmed on its own."""
    project, conversation = chat(studio, mode="manual")
    other = json.dumps({"tasks": [
        {"id": "sso", "title": "Вход через SSO", "description": "Вместо ссылки.",
         "acceptance": ["вход работает"], "dependencies": []}]})
    studio.answers(PLAN, other)
    assert send(studio, project, conversation, "Реализуй вход по ссылке из письма")[0] == 200
    first = workflow_of(studio, project, conversation)["plan_hash"]

    status, body = send(studio, project, conversation, "Сделай иначе: вход через SSO")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["awaiting_approval"] is True
    assert "Вход через SSO" in body["messages"][-1]["content"]
    after = workflow_of(studio, project, conversation)
    assert after["plan_hash"] != first and after["plan_approved_hash"] is None
    # The short order was planned with the earlier one in view.
    assert "Реализуй вход по ссылке из письма" in planning_prompts(studio)[1]
    assert runs(studio, project) == []


@needs_db
def test_in_manual_mode_the_answer_to_the_clarifying_question_is_read_too(studio):
    project, conversation = chat(studio, mode="manual")
    status, body = send(studio, project, conversation, "Проверь и исправь тесты формы входа")
    assert status == 200, body
    assert body["messages"][-1]["meta"].get("clarify") is True

    # "Work": a plan of the message that was asked about, and no run.
    studio.answers(PLAN)
    status, body = send(studio, project, conversation, "Сразу работу и прогон")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["meta"]["mode"] == "plan" and reply["meta"]["awaiting_approval"] is True
    asked = planning_prompts(studio)
    assert len(asked) == 1 and "Проверь и исправь тесты формы входа" in asked[0]
    assert runs(studio, project) == []

    # «Запускай» is a natural answer to "…or do the work and start a run?".
    # It answers the question: the message that was asked about gets its plan.
    # Read as the launch command, it would have started the plan above, which
    # is about something else.
    first = workflow_of(studio, project, conversation)["plan_hash"]
    status, body = send(studio, project, conversation, "Проверь и почини сборку документации")
    assert status == 200 and body["messages"][-1]["meta"].get("clarify") is True, body
    docs = json.dumps({"tasks": [
        {"id": "docs", "title": "Починить сборку документации", "description": "mkdocs.",
         "acceptance": ["сборка проходит"], "dependencies": []}]})
    studio.answers(docs)
    status, body = send(studio, project, conversation, "Запускай")
    assert status == 200, body
    reply = body["messages"][-1]
    assert reply["meta"]["mode"] == "plan" and reply["meta"]["awaiting_approval"] is True
    assert "Починить сборку документации" in reply["content"]
    assert "Проверь и почини сборку документации" in planning_prompts(studio)[-1]
    assert workflow_of(studio, project, conversation)["plan_hash"] != first
    assert runs(studio, project) == [], "the plan that was on offer before was started"
    # Said again, with the new plan on offer and no question pending, it is
    # the launch command: the new plan starts, once.
    status, body = send(studio, project, conversation, "Запускай")
    assert status == 200, body
    started = runs(studio, project)
    assert len(started) == 1
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{started[0]['id']}")[1]
    assert [task["title"] for task in detail["tasks"]] == ["Починить сборку документации"]

    # "In the chat": the message that was asked about is what gets answered.
    other = studio.conversation(project, mode="manual")
    assert send(studio, project, other, "Проверь и исправь тесты формы входа")[0] == 200
    studio.answers("Тесты формы входа лежат в tests/test_app.py.")
    status, body = send(studio, project, other, "В чате")
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Тесты формы входа лежат в tests/test_app.py."
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "Request:\nПроверь и исправь тесты формы входа" in prompt
    assert len(runs(studio, project)) == 1, "an answer in the chat started a run"
