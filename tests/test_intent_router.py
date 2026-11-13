"""Chat-first intent routing: a question is answered, only an order starts work.

The defect these tests cover: with the conversation's mode set to
``autonomous``, *any* message started an autonomous run - so "почему прогон
ломается?" launched agents. The mode was a label on the conversation, not a
decision about the message.

The first half is the pure classifier (no database): the table from the
requirement, and the shape rules that keep a question a question. The second
half drives the real server over real HTTP and PostgreSQL and asserts the
observable behaviour the requirement names: a question creates no run and no
OpenHands conversation, an order does, "продолжи" continues instead of
duplicating, and an ambiguous message is answered with one clarifying question.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_intent import (  # noqa: E402
    ANALYSIS,
    AUTONOMOUS,
    CHAT,
    classify,
    clarifying_question,
    effective_mode,
)
from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run intent tests")


@pytest.fixture
def studio(monkeypatch):
    """The real server on the fakes, exactly as ``test_studio_api`` builds it."""
    with studio_stack(monkeypatch) as stack:
        yield stack

PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
     "acceptance": ["сводка"], "dependencies": []},
]}


# ------------------------------------------------------------- the classifier
@pytest.mark.parametrize("text, expected", [
    # chat: questions, explanations, discussion, opinion, a plan request.
    ("Привет", CHAT),
    ("Что уже готово?", CHAT),
    ("Ок, что уже готово?", CHAT),
    ("Что ты думаешь об этой архитектуре?", CHAT),
    ("Объясни, что делает этот модуль.", CHAT),
    ("Объясни архитектуру", CHAT),
    ("Давай обсудим, как лучше реализовать эту функцию", CHAT),
    ("Давай обсудим реализацию", CHAT),
    ("Предложи план улучшений", CHAT),
    ("почему проект использует такую архитектуру?", CHAT),
    # analysis: look into the real state, read-only.
    ("Почему прогон остановился?", ANALYSIS),
    ("Почему прогон ломается?", ANALYSIS),
    ("Посмотри, почему автопрогон ломается.", ANALYSIS),
    ("Почему возникла эта ошибка?", ANALYSIS),
    ("Проанализируй проблему", ANALYSIS),
    ("Проанализируй, почему тесты падают.", ANALYSIS),
    ("Найди причину ошибки.", ANALYSIS),
    ("Проверь, почему агент остановился.", ANALYSIS),
    ("Изучи архитектуру.", ANALYSIS),
    ("Сравни эти два подхода.", ANALYSIS),
    ("Посмотри логи и объясни проблему.", ANALYSIS),
    # autonomous: an order to do work.
    ("Исправь эту ошибку", AUTONOMOUS),
    ("Исправь найденные ошибки", AUTONOMOUS),
    ("Реализуй функцию", AUTONOMOUS),
    ("Реализуй эту функцию", AUTONOMOUS),
    ("Доработай проект и проведи тестирование", AUTONOMOUS),
    ("Запусти автономный прогон", AUTONOMOUS),
    ("Выполни этот план", AUTONOMOUS),
    ("Создай полноценную реализацию по этому техническому заданию", AUTONOMOUS),
    ("Продолжай работу до завершения", AUTONOMOUS),
])
def test_the_requirement_table(text, expected):
    assert classify(text, mode="auto").intent == expected


def test_a_question_is_never_an_order_even_in_autonomous_mode():
    """The exact defect: a question in an autonomous conversation must not run."""
    for text in ("Почему прогон ломается?", "Что уже готово?", "Объясни ошибку"):
        intent = classify(text, mode="autonomous")
        assert not intent.starts_work
        assert effective_mode("autonomous", intent) != "autonomous"


def test_an_order_in_autonomous_mode_does_run():
    intent = classify("Исправь эту ошибку", mode="autonomous")
    assert intent.starts_work
    assert effective_mode("autonomous", intent) == "autonomous"


def test_a_buried_order_in_a_question_is_ambiguous_not_a_run():
    """"а не лучше ли переписать?" is a question about a change, not an order."""
    intent = classify("Возможно, стоит переписать модуль?", mode="auto")
    assert intent.intent == CHAT and intent.ambiguous
    assert not intent.starts_work
    assert classify("А не лучше ли переписать модуль?", mode="auto").ambiguous


def test_the_analysis_mode_is_read_only_even_for_an_order():
    assert effective_mode("analysis", classify("Исправь ошибку", mode="analysis")) == "analysis"


def test_continue_is_an_order_only_when_there_is_work():
    assert classify("Продолжи", has_active_run=True).intent == AUTONOMOUS
    alone = classify("Продолжи")
    assert alone.intent == CHAT and not alone.starts_work
    assert classify("Продолжай работу до завершения").intent == AUTONOMOUS


def test_the_clarifying_question_is_one_short_sentence():
    text = clarifying_question()
    assert text.count("?") == 1
    assert "прогон" in text.lower()


# ------------------------------------------------------ the observable behaviour
def _count_runs(stack, project) -> int:
    return len(stack.api.get(f"/api/v1/projects/{project}/runs?include_hidden=1")[1]["runs"])


def _start_conversation(stack, project, mode="auto"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _send(stack, project, conversation, text, mode=None):
    body = {"text": text}
    if mode:
        body["mode"] = mode
    return stack.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", body)


def test_a_question_in_autonomous_mode_starts_no_run(studio):
    """The whole point: asking in an autonomous conversation is answered."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="autonomous")
    studio.answers("Прогон остановился из-за ошибки проверки коммита.")

    status, body = _send(studio, project, conversation, "Почему прогон ломается?")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["meta"]["mode"] in ("ask", "analysis")
    # Nothing was started: no run, no goal, no OpenHands conversation.
    assert _count_runs(studio, project) == 0
    assert studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"] == []
    assert studio.cloud.cloud.conversations == {}


def test_an_order_in_auto_mode_starts_a_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))

    status, body = _send(studio, project, conversation, "Исправь найденные ошибки")
    assert status == 200, body
    run_ids = [m["meta"].get("run_id") for m in body["messages"] if m["meta"].get("run_id")]
    assert run_ids, body
    assert _count_runs(studio, project) == 1


def test_an_analysis_turn_reads_the_real_state_and_changes_nothing(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers("Разбор.")

    status, body = _send(studio, project, conversation, "Проанализируй, почему тесты падают")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    assert _count_runs(studio, project) == 0
    # The prompt carried the real run state (the facts block), not a guess.
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "Autonomous runs of this project" in prompt


def test_an_analysis_turn_in_a_project_that_has_runs_is_answered_from_them(studio):
    """The case above has no run, so the loop that gathers the facts never ran.
    With a run in the project it asked ``LabStore`` for a method no class has,
    and the turn ended in a 500. Here: a run, and a finished one's report."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Исправь найденные ошибки")
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))

    studio.answers("Прогон идёт: одна задача в работе.")
    status, body = _send(studio, project, conversation, "Проанализируй, почему тесты падают")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    assert body["messages"][-1]["content"] == "Прогон идёт: одна задача в работе."
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "Autonomous runs of this project" in prompt and run_id in prompt
    assert '"policy": "durable"' in prompt
    assert _count_runs(studio, project) == 1, "the question started another run"

    # A run that has a report: its text is among the facts.
    with studio.app.store._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, body, outcome) "
            "VALUES (%s, %s, %s, %s, 'no_changes')",
            (run_id, project, conversation,
             "## Отчёт автономного прогона\n\n**Статус:** изменений в репозитории нет."))
    studio.answers("В репозитории ничего не изменилось.")
    status, body = _send(studio, project, conversation, "Проанализируй отчёт последнего прогона")
    assert status == 200, body
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "изменений в репозитории нет" in prompt and '"report_outcome": "no_changes"' in prompt


def test_an_ambiguous_message_asks_one_question_and_starts_nothing(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")

    status, body = _send(studio, project, conversation, "Возможно, стоит переписать модуль?")
    assert status == 200, body
    last = body["messages"][-1]
    assert last["meta"].get("clarify") is True
    assert last["content"].count("?") == 1
    assert _count_runs(studio, project) == 0
    assert studio.llm.calls == [], "an ambiguous message must not even call the model"


def test_continue_resumes_the_conversations_run_and_never_duplicates_it(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Реализуй функцию")
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))

    # Pause it, then ask to continue: the same run is resumed, not a new one.
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/pause", {})[0] == 200
    status, body = _send(studio, project, conversation, "Продолжи")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["run_id"] == run_id
    assert body["messages"][-1]["meta"].get("resumed") is True
    assert _count_runs(studio, project) == 1, "a duplicate run was created"
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]
    assert detail["run"]["state"] == "running"


def test_a_question_during_a_running_run_does_not_stop_or_restart_it(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Реализуй функцию")
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))
    before = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]

    studio.answers("Агент выполняет первую задачу.")
    status, body = _send(studio, project, conversation, "Что сейчас делает агент?")
    assert status == 200, body
    after = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]
    assert after["state"] == before["state"], "the question changed the run's state"
    assert _count_runs(studio, project) == 1
