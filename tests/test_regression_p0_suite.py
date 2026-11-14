"""The P0 master task's mandatory regression scenarios (section 10, A-N).

Each test's name and docstring name the letter it covers and the exact
assertion the task requires (an explicit run-count, or a specific behaviour).
Letters already covered end-to-end elsewhere are not repeated here except
where this file adds something the existing test does not check:

* A, B, H are covered by ``tests/test_intent_router.py``
  (``test_the_requirement_table``, ``test_an_order_in_auto_mode_starts_a_run``).
* C, D are covered by ``tests/test_intent_router.py``
  (``test_report_request_with_a_completed_run_reads_history_not_a_new_run``,
  ``test_an_explicit_prohibition_is_honoured_end_to_end_even_in_autonomous_mode``).
* E is covered by ``tests/test_intent_router.py``
  (``test_an_analysis_turn_reads_the_real_state_and_changes_nothing``).

This file adds F, G, I, J, K, L, M, N, which had no end-to-end test before.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")

PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
     "acceptance": ["сводка"], "dependencies": []},
]}


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


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


# ---------------------------------------------------------------------- F
def test_f_a_plan_request_is_shown_in_chat_and_starts_no_run(studio):
    """F: "Составь план исправлений" -> a plan appears in the chat, 0 runs."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers("Это план в прозе, не задача.")

    status, body = _send(studio, project, conversation, "Составь план исправлений")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][-1]["meta"]["mode"] in ("ask", "analysis")
    assert _count_runs(studio, project) == 0
    assert studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"] == []


# ---------------------------------------------------------------------- G
def test_g_davay_prodolzhim_with_no_prior_topic_is_a_question_not_a_run(studio):
    """G: "Давай продолжим" with nothing established yet -> dialogue, not a run."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers("Что именно продолжить? Уточните, пожалуйста.")

    status, body = _send(studio, project, conversation, "Давай продолжим")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert _count_runs(studio, project) == 0
    assert studio.cloud.cloud.conversations == {}


# ---------------------------------------------------------------------- I
def test_i_a_double_submit_of_the_same_message_does_not_create_a_second_run(studio):
    """I: the same text, sent twice for the same conversation (a client retry
    after a dropped response) -> the first run stands, no second run."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))

    text = "Исправь найденные ошибки"
    status1, body1 = _send(studio, project, conversation, text)
    assert status1 == 200, body1
    run_id = next(m["meta"]["run_id"] for m in body1["messages"] if m["meta"].get("run_id"))
    assert _count_runs(studio, project) == 1

    # The exact same text again, as a client would on a retried request.
    status2, body2 = _send(studio, project, conversation, text)
    assert status2 == 200, body2
    assert _count_runs(studio, project) == 1, "a retry of the same message started a second run"
    # The response to the retry names the same run, not a new one.
    second_run_ids = [m["meta"].get("run_id") for m in body2["messages"] if m["meta"].get("run_id")]
    if second_run_ids:
        assert all(r == run_id for r in second_run_ids)


# ---------------------------------------------------------------------- J
def test_j_an_empty_model_response_is_a_clear_error_never_a_silent_empty_message(studio):
    """J: every configured provider answers with empty text -> the turn ends
    in a clear error, never a 200 with an empty assistant message, and never
    an autonomous run started as a fallback."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers("")  # the only configured provider returns empty text

    status, body = _send(studio, project, conversation, "Что уже готово?")
    assert status == 502, body
    assert "messages" in body
    roles = [m["role"] for m in body["messages"]]
    assert "system" in roles, "no error message was recorded for the empty response"
    error_message = next(m for m in body["messages"] if m["role"] == "system")
    assert error_message["meta"].get("error") is True
    assert error_message["content"], "the error message itself must not be empty"
    # No assistant message with empty content was stored.
    assert all(m["content"] for m in body["messages"] if m["role"] == "assistant")
    assert _count_runs(studio, project) == 0, "an empty response must not fall back to a run"


# ---------------------------------------------------------------------- K
def test_k_a_provider_outage_is_a_clear_failure_not_an_infinite_pending_state(studio):
    """K: the chat has no token-level stream (one JSON body per turn, per
    ``_chat_turn``'s own docstring) - the closest real analogue of a
    "streaming interruption" is every configured provider being unreachable
    mid-turn. That must end the turn with a clear, recorded failure: the
    conversation is never left in a processing state with nothing to show
    for it."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.llm.script = [{"status": 500, "payload": {"error": "provider unavailable"}}]
    studio.llm.index = 0

    status, body = _send(studio, project, conversation, "Что уже готово?")
    assert status == 502, body
    convo = studio.api.get(f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]
    assert convo["state"] == "error"
    messages = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]
    assert messages[-1]["role"] == "system" and messages[-1]["meta"].get("error") is True
    assert _count_runs(studio, project) == 0


# ---------------------------------------------------------------------- L
def test_l_a_clarifying_follow_up_about_a_named_report_stays_on_it_without_a_new_run(studio):
    """L: "Покажи последний успешный прогон" -> NEXUS names run ABC -> "А что
    в нём исправлено?" must stay on ABC's report, not start a new run."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Реализуй функцию")
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/pause", {})[0] == 200

    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state = 'completed' WHERE id = %s", (run_id,))
        cur.execute(
            "INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, body, outcome) "
            "VALUES (%s, %s, %s, %s, 'changes_made')",
            (run_id, project, conversation,
             "## Отчёт автономного прогона\n\n**Исправлено:** валидация email в форме регистрации."))

    studio.answers(f"Нашёл прогон {run_id}: задача выполнена.")
    status, body = _send(studio, project, conversation, "Покажи последний успешный прогон")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    assert _count_runs(studio, project) == 1

    # The follow-up names no run itself - it must still be answered from the
    # same run's facts, not from a fresh, contextless question, and still
    # without starting anything.
    studio.answers("Исправлена валидация email в форме регистрации.")
    status, body = _send(studio, project, conversation, "А что в нём исправлено?")
    assert status == 200, body
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert run_id in prompt, "the follow-up lost which run was being discussed"
    assert "валидация email" in prompt
    assert _count_runs(studio, project) == 1, "the follow-up started another run"


# ---------------------------------------------------------------------- M
def test_m_an_informational_question_after_a_prohibition_still_does_not_launch(studio):
    """M: the user forbade launching, then asks an informational question ->
    still no launch (the prohibition is not even needed to hold here, since
    an informational question never launches on its own - this checks both
    halves of section 8's requirement together)."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="autonomous")
    studio.answers("Хорошо, не запускаю.", "В проекте пока нет прогонов.")

    status, body = _send(studio, project, conversation, "Не запускай ничего")
    assert status == 200, body
    assert _count_runs(studio, project) == 0

    status, body = _send(studio, project, conversation, "Что уже готово?")
    assert status == 200, body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert _count_runs(studio, project) == 0
    assert studio.cloud.cloud.conversations == {}


# ---------------------------------------------------------------------- N
def test_n_a_report_is_still_readable_after_the_backend_restarts(studio):
    """N: a completed run's report survives a backend restart - it lives in
    PostgreSQL, not in the process that produced it."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Реализуй функцию")
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state = 'completed' WHERE id = %s", (run_id,))
        cur.execute(
            "INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, body, outcome) "
            "VALUES (%s, %s, %s, %s, 'changes_made')",
            (run_id, project, conversation, "## Отчёт\n\nЗадача выполнена."))

    # Simulate a backend restart: a brand-new App/WebServer process, same
    # PostgreSQL, nothing carried over in memory (Studio.boot()'s own
    # contract - see its docstring).
    old_server = studio.server
    studio.boot()
    old_server.stop()

    status, body = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}/report")
    assert status == 200, body
    assert body["report"] is not None
    assert "Задача выполнена" in body["report"]["body"]
