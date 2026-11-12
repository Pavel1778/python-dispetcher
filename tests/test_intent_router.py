"""Intent routing: a message is answered, analysed or run - never guessed at.

The router is deterministic first; the model is asked only for a turn the rules
cannot place, and a model that is unavailable or uncertain leaves the safe
default (chat). These tests pin the rules and the safe defaults, so a change
that would start work on a plain question fails here.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_intent import ROUTED_MODES, IntentRouter  # noqa: E402


def router() -> IntentRouter:
    # No provider: only the rules and the safe default are exercised, which is
    # the deployment where no model is configured.
    return IntentRouter(None)


# ------------------------------------------------------------------- the default
@pytest.mark.parametrize("text", [
    "Привет!",
    "Как устроен этот проект?",
    "Что ты умеешь?",
    "Расскажи про архитектуру",
    "Спасибо",
])
def test_a_plain_message_is_a_chat_and_starts_nothing(text):
    intent = router().route(text, conversation_mode="ask")
    assert intent.mode == "chat"
    assert intent.mode in ROUTED_MODES


def test_a_question_about_a_run_is_answered_from_data():
    for text in ("Что готово?", "Статус?", "Покажи отчёт", "Что осталось?"):
        intent = router().route(text, conversation_mode="ask")
        assert intent.mode == "chat", text


# ------------------------------------------------------------------ work commands
@pytest.mark.parametrize("text", [
    "Исправь ошибку в корзине",
    "Добавь тесты для модуля",
    "Реализуй авторизацию",
    "Отрефактори этот файл",
    "Запусти автономно: покрыть тестами",
])
def test_a_clear_work_command_starts_work(text):
    intent = router().route(text, conversation_mode="ask")
    assert intent.mode == "autonomous"


def test_an_explicit_autonomous_mode_is_honoured():
    intent = router().route("просто текст", conversation_mode="autonomous")
    assert intent.mode == "autonomous" and intent.source == "rule"


def test_an_explicit_code_mode_is_autonomous_and_plan_is_chat():
    assert router().route("x", conversation_mode="code").mode == "autonomous"
    assert router().route("x", conversation_mode="plan").mode == "chat"


# -------------------------------------------------------------------- analysis
@pytest.mark.parametrize("text", [
    "Почему падает тест?",
    "Объясни, как работает планировщик",
    "Проанализируй, в чём ошибка",
    "Разберись, что не так с этим модулем",
])
def test_an_analysis_request_does_not_change_code(text):
    intent = router().route(text, conversation_mode="ask")
    assert intent.mode == "analysis"


# ------------------------------------------------------------- continuations
def test_continue_with_an_active_run_is_a_continuation():
    intent = router().route("Продолжи", conversation_mode="ask", has_active_run=True)
    assert intent.mode == "autonomous" and intent.continuation


def test_continue_with_no_active_run_does_not_start_one():
    intent = router().route("Продолжи", conversation_mode="ask", has_active_run=False)
    assert intent.mode == "chat" and not intent.continuation


def test_a_work_command_is_not_taken_for_a_continuation():
    intent = router().route("Повтори рефакторинг и добавь тесты",
                            conversation_mode="ask", has_active_run=True)
    assert intent.mode in ("autonomous",)


# ----------------------------------------------------------------- ambiguity
def test_an_ambiguous_request_asks_one_clarifying_question():
    intent = router().route("Можешь глянуть этот модуль", conversation_mode="ask")
    assert intent.mode == "chat"
    assert intent.clarifying and "Автономно" in intent.clarifying


def test_a_plain_message_asks_no_clarifying_question():
    intent = router().route("Привет", conversation_mode="ask")
    assert intent.mode == "chat" and not intent.clarifying


# ------------------------------------------------------------------- model path
class _FakeCascade:
    def __init__(self, answer: str | None, configured: bool = True):
        self._answer = answer
        self._configured = configured
        self.calls = 0

    def configured(self) -> bool:
        return self._configured

    def text(self, prompt, system):
        self.calls += 1
        from orchestration.providers import ProviderResponse
        return ProviderResponse(provider="fake", model="m",
                                data={"text": self._answer or ""})


def test_the_model_classifies_only_what_the_rules_cannot():
    cascade = _FakeCascade("autonomous")
    intent = IntentRouter(cascade).route("нужно поправить этот модуль",
                                         conversation_mode="ask")
    assert intent.mode == "autonomous" and intent.source == "model"
    assert cascade.calls == 1


def test_an_unavailable_model_leaves_the_safe_default():
    cascade = _FakeCascade(None, configured=False)
    intent = IntentRouter(cascade).route("нужно поправить этот модуль",
                                         conversation_mode="ask")
    assert intent.mode == "chat" and cascade.calls == 0


def test_a_model_that_fails_never_breaks_the_turn():
    class Broken:
        def configured(self):
            return True

        def text(self, *a, **k):
            raise RuntimeError("provider down")

    intent = IntentRouter(Broken()).route("нужно поправить этот модуль",
                                          conversation_mode="ask")
    assert intent.mode == "chat"


def test_a_long_document_is_not_classified_by_the_model():
    """A long text is a specification; the planner handles it, not the router."""
    cascade = _FakeCascade("autonomous")
    intent = IntentRouter(cascade).route("x" * 2000, conversation_mode="ask")
    assert intent.mode == "chat" and cascade.calls == 0
