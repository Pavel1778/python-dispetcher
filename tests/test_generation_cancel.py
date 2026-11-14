"""Stop generation: cancel the AI answer in flight, keep what it produced.

The provider stand-in can be made slow (``server.respond`` blocks), so a real
cancel arrives *while* the answer is being generated - which is the only way to
prove the stop is immediate and not "after the model finishes". The tests use
the real server, real PostgreSQL and the real HTTP surface.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_generation import (  # noqa: E402
    GenerationCancelled,
    GenerationRegistry,
    GenerationToken,
)
from tests.test_studio_api import DSN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def _conversation(stack, project, mode="ask"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


# --------------------------------------------------------------- the token
def test_a_token_reports_cancellation_and_keeps_partial_text():
    token = GenerationToken("g1")
    assert not token.cancelled
    token.note("часть ответа")
    token.cancel()
    assert token.cancelled
    with pytest.raises(GenerationCancelled) as caught:
        token.check()
    assert caught.value.partial == "часть ответа"


def test_cancelling_a_bound_response_closes_it():
    class Response:
        closed = False

        def close(self):
            self.closed = True

    token = GenerationToken("g2")
    response = Response()
    token.bind(response)
    token.cancel()
    assert response.closed, "a cancel must abort the live response"


def test_the_registry_cancels_only_what_it_holds():
    registry = GenerationRegistry()
    token = registry.register(GenerationToken("g3"))
    assert registry.cancel("g3") is True
    assert token.cancelled
    assert registry.cancel("nope") is False
    registry.unregister("g3")
    assert registry.cancel("g3") is False


# --------------------------------------------------------- the HTTP surface
def test_stopping_a_generation_keeps_the_partial_answer_and_frees_the_chat(studio):
    """A slow provider, a cancel that arrives mid-answer, and a usable chat."""
    project = studio.project()
    conversation = _conversation(studio, project)
    started = threading.Event()

    def slow(body):
        started.set()
        time.sleep(5)          # long enough for the cancel to arrive first
        return 200, {"choices": [{"message": {"content": "поздний ответ"}}]}

    studio.llm.respond = slow
    result: dict = {}

    def send():
        result["status"], result["body"] = studio.api.post(
            f"/api/v1/projects/{project}/conversations/{conversation}/messages",
            {"text": "Проанализируй весь backend"})

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    assert started.wait(timeout=10), "the answer never started"

    # Stop it while it is generating.
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/cancel", {})
    assert status == 200, body
    assert body["cancelled"] is True and body["stopped"] is True
    generation_id = body["generation_id"]

    worker.join(timeout=15)
    assert not worker.is_alive(), "the cancelled answer did not stop promptly"
    assert result["status"] == 200, result["body"]
    assert result["body"].get("cancelled") is True

    # A new request is accepted immediately (the conversation is idle).
    studio.llm.respond = None
    studio.answers("Новый ответ.")
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Стоп. Проверь только авторизацию"})
    assert status == 200, body
    assert body["messages"][-1]["content"] == "Новый ответ."

    # The generation row is cancelled, exactly once.
    with studio.app.store._tx() as cur:
        cur.execute("SELECT state FROM nexus_generations WHERE id=%s", (generation_id,))
        assert cur.fetchone()["state"] == "cancelled"


def test_a_late_cancel_with_no_running_generation_is_a_conflict(studio):
    project = studio.project()
    conversation = _conversation(studio, project)
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/cancel", {})
    assert status == 409
    assert "no generation" in body["error"]


def test_a_cancel_naming_the_wrong_generation_is_refused(studio):
    project = studio.project()
    conversation = _conversation(studio, project)
    started = threading.Event()

    def slow(body):
        started.set()
        time.sleep(3)
        return 200, {"choices": [{"message": {"content": "поздно"}}]}

    studio.llm.respond = slow
    worker = threading.Thread(
        target=lambda: studio.api.post(
            f"/api/v1/projects/{project}/conversations/{conversation}/messages",
            {"text": "Долгий запрос"}), daemon=True)
    worker.start()
    assert started.wait(timeout=10)
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/cancel",
        {"generation_id": "00000000-0000-4000-8000-000000000000"})
    assert status == 409 and "not the one running" in body["error"]
    # The real one can still be stopped.
    assert studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/cancel", {})[0] == 200
    worker.join(timeout=15)


def test_stopping_generation_does_not_touch_a_run(studio):
    """Stop AI and cancel run are different actions (the requirement)."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": "Pavel1778/python-dispetcher"})[0] == 200
    # A real run of the project, in its own conversation.
    autonomous = _conversation(studio, project, mode="autonomous")
    studio.answers('{"tasks": [{"id": "t", "title": "Шаг", "description": "сделать", '
                   '"acceptance": ["ок"], "dependencies": []}]}')
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{autonomous}/messages",
        {"text": "Исправь найденные ошибки"})
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))
    # A text answer in a question conversation, then stop it.
    conversation = _conversation(studio, project, mode="ask")
    started = threading.Event()

    def slow(body):
        started.set()
        time.sleep(3)
        return 200, {"choices": [{"message": {"content": "долго"}}]}

    studio.llm.respond = slow
    worker = threading.Thread(
        target=lambda: studio.api.post(
            f"/api/v1/projects/{project}/conversations/{conversation}/messages",
            {"text": "Проанализируй, почему идёт прогон"}), daemon=True)
    worker.start()
    assert started.wait(timeout=10)
    assert studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/cancel", {})[0] == 200
    worker.join(timeout=15)
    # The run is exactly as it was: a text stop never touches it.
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]
    assert detail["id"] == run_id
    assert detail["state"] != "cancelled"
