"""Intent routing: is a chat turn a question, an analysis, or a task to run?

NEXUS must not turn every message into an autonomous run. The default is a
conversation: a question is answered in the chat, an error is analysed without
changing code, and an autonomous run is started **only** when the person clearly
asks for work to be done. This module decides which of those a turn is.

It is deterministic first. A fast, explainable rule pass runs before any model:
a clear command ("продолжи", "запусти автономно", "исправь ...") is recognised
without a provider call, so the common cases are cheap and never depend on a
model being configured. Only a turn the rules cannot place is handed to the
model, and a model that is unavailable or uncertain is treated as a **question**
- the safe default is to answer, never to start work.

The three modes it routes to:

* ``chat``        - answer the question in the chat. Nothing is started.
* ``analysis``    - explain what is wrong, without changing code. Nothing runs.
* ``autonomous``  - the person asked for work: plan it and start a run.

A turn the rules or the model cannot place is ``chat`` with a clarifying
question, so an ambiguous request is asked about once rather than guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The modes a routed turn can be. ``chat`` is the default and the safe fallback.
ROUTED_MODES = ("chat", "analysis", "autonomous")

#: A command that clearly asks for work, in the imperative or an explicit
#: "autonomous" phrasing. Matched case-insensitively against the message start.
_WORK_COMMANDS = (
    "реализуй", "сделай", "исправь", "почини", "добавь", "удали", "перепиши",
    "отрефактори", "закоммить", "запушь", "напиши код", "внедри", "обнови",
    "implement", "fix", "refactor", "commit", "push", "deploy",
)
#: Phrases that name the autonomous mode outright.
_AUTONOMOUS_PHRASES = (
    "автономн", "выполни", "запусти прогон", "запусти автономно", "сделай полностью",
    "выполни задачу", "доведи до конца", "run it autonomously", "autonomous run",
)
#: Commands that continue an existing run rather than start a new one.
_CONTINUE_PHRASES = (
    "продолжи", "продолжай", "повтори", "попробуй ещё", "доделай", "возобнови",
    "continue", "retry", "resume",
)
#: Questions about a run's state - answered from stored data, never a new run.
_STATUS_PHRASES = (
    "что готово", "что сделано", "статус", "как дела", "прогресс", "что сейчас",
    "где остановились", "что осталось", "покажи отчёт", "отчёт готов",
    "status", "what is done", "what's left", "progress", "report",
)
#: An analysis request: explain, do not change.
_ANALYSIS_PHRASES = (
    "почему", "как работает", "объясни", "разбери", "проанализируй", "в чём ошибка",
    "что не так", "какая причина", "разберись", "диагностика",
    "why", "explain", "analyse", "analyze", "what is wrong", "diagnose",
)
#: A weak signal that the person *might* want work done, without a clear
#: command. When one of these is present the turn is answered and a single
#: clarifying question is asked ("do you want this done?"), rather than either
#: silently starting work or silently ignoring the intent.
_WORK_HINTS = (
    "нужно", "надо", "давай", "можешь", "хочу", "помоги", "стоит ли",
    "could you", "can you", "i need", "i want", "let's", "shall we",
)
#: A pure question: starts with an interrogative or ends with a question mark.
_QUESTION_STARTS = (
    "что", "как", "почему", "зачем", "где", "когда", "кто", "сколько", "можно ли",
    "какой", "какая", "какие", "есть ли", "чем",
    "what", "how", "why", "where", "when", "who", "which", "is ", "are ", "can ",
    "does ", "do ",
)


def _starts_with(text: str, phrases: tuple[str, ...]) -> str | None:
    """The first phrase *text* begins with, or ``None``.

    Only the start matters for a command: "сделай X" is a command, but a
    sentence that merely mentions "сделай" halfway is not (it may be a
    question about a past action).
    """
    low = text.lstrip().lower()
    for phrase in phrases:
        if low.startswith(phrase):
            return phrase
    return None


def _mentions(text: str, phrases: tuple[str, ...]) -> str | None:
    low = text.lower()
    for phrase in phrases:
        if phrase in low:
            return phrase
    return None


#: A status question is short: a person typing "что готово?" asks about the run;
#: a 5 000-character document that contains the word "status" is a specification
#: and must reach the planner, not be answered as a question about a run.
_STATUS_MAX_CHARS = 120


def _short_status_question(text: str) -> str | None:
    """The status phrase *text* asks about, if it is a short question about a run."""
    stripped = text.strip()
    if len(stripped) > _STATUS_MAX_CHARS:
        return None
    low = stripped.lower()
    for phrase in _STATUS_PHRASES:
        if low.startswith(phrase) or (phrase in low and stripped.endswith("?")):
            return phrase
    return None


@dataclass(frozen=True)
class Intent:
    """What a turn is, and why - both are shown to the caller and the UI."""

    mode: str                       # chat | analysis | autonomous
    reason: str                     # a short, human-readable explanation
    source: str                     # "rule" | "model" | "default"
    confidence: float               # 0..1
    continuation: bool = False      # "continue the run I already have"
    clarifying: str = ""            # a question to ask when it is ambiguous

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason, "source": self.source,
                "confidence": self.confidence, "continuation": self.continuation,
                "clarifying": self.clarifying}


class IntentRouter:
    """Decide a turn's mode: deterministic rules first, the model only after.

    The router never starts anything; it only classifies. The caller
    (:class:`orchestration.web_server.Handler._chat_turn`) acts on the mode, and
    the dispatch hooks it calls are the ones that actually run work.
    """

    def __init__(self, providers: Any | None = None):
        #: The provider cascade, used only for a turn the rules cannot place.
        #: ``None`` (or an unavailable cascade) leaves the safe default: chat.
        self.providers = providers

    def route(self, text: str, *, conversation_mode: str | None = None,
              has_active_run: bool = False) -> Intent:
        """Classify *text*.

        *conversation_mode*, when the person picked one explicitly ("Автономно"
        in the composer), is honoured: an explicit choice is not second-guessed.
        *has_active_run* makes "продолжи"/"повтори" a continuation of that run
        instead of a new one, so a follow-up never starts a duplicate.
        """
        text = (text or "").strip()
        if not text:
            return Intent("chat", "пустое сообщение", "default", 1.0)

        # 1. An explicit "План" or "Код" mode is an instruction, not a hint, and
        #    is not re-classified. The default "ask" (a plain chat) does NOT
        #    short-circuit - it is exactly the case the router exists for.
        #    "Автономно" is handled below (after the continuation and status
        #    checks) so that "продолжи" in that mode continues the run and a
        #    status question is answered, rather than starting a second run.
        if conversation_mode == "code":
            return Intent("autonomous", "выбран режим «Код»", "rule", 1.0)
        if conversation_mode == "plan":
            return Intent("chat", "выбран режим «План»", "rule", 1.0)

        # 2. A continuation of an existing run is not a new run - this applies in
        #    every mode, including an explicit "Автономно", so a follow-up never
        #    starts a duplicate.
        cont = _starts_with(text, _CONTINUE_PHRASES) or _mentions(text, _CONTINUE_PHRASES)
        if cont and has_active_run:
            return Intent("autonomous", f"продолжение текущего прогона («{cont}»)",
                          "rule", 0.9, continuation=True)

        # 3. A status question about a run is answered from stored data, never a
        #    new run - this is the "что готово?" case. It must be *short* and
        #    *start* with the phrase (or end with "?"): a long document that
        #    merely contains the word "status" is a specification, not a
        #    question, and must not be diverted from the run it asks for.
        status = _short_status_question(text)
        if status and not _starts_with(text, _WORK_COMMANDS):
            return Intent("chat", f"вопрос о ходе работы («{status}»)", "rule", 0.9)

        # 4. An explicit "Автономно" mode: the text is the task. Reached only
        #    after the continuation and status checks above.
        if conversation_mode == "autonomous":
            return Intent("autonomous", "выбран режим «Автономно»", "rule", 1.0)

        # 5. A continuation with no run to continue is answered, not started.
        if cont:
            return Intent("chat", f"продолжение без активного прогона («{cont}»)",
                          "rule", 0.7)

        # 6. A clear work command in the imperative starts work. Only the start
        #    of the message counts, so a document that mentions "сделай" in its
        #    prose is not taken for a command.
        command = _starts_with(text, _WORK_COMMANDS)
        if command:
            return Intent("autonomous", f"команда выполнить работу («{command}»)",
                          "rule", 0.85)
        # A *phrase* match ("выполни", "разбери", ...) is only trusted for a
        # short message; a long one is a specification, and the planner - not
        # the router - decides what it asks for.
        if len(text) <= _STATUS_MAX_CHARS:
            phrase = _mentions(text, _AUTONOMOUS_PHRASES)
            if phrase:
                return Intent("autonomous", f"явный запрос автономной работы («{phrase}»)",
                              "rule", 0.85)

        # 7. An analysis request is answered without changing code.
        if len(text) <= _STATUS_MAX_CHARS:
            analysis = (_starts_with(text, _ANALYSIS_PHRASES)
                        or _mentions(text, _ANALYSIS_PHRASES))
            if analysis:
                return Intent("analysis", f"запрос на анализ («{analysis}»)", "rule", 0.8)

        # 8. An obvious question is answered in the chat.
        if text.endswith("?") or _starts_with(text, _QUESTION_STARTS):
            return Intent("chat", "вопрос", "rule", 0.8)

        # 9. Nothing matched. Ask the model, but only for a short message; a
        #    long one is a document, and the rules above already cover the
        #    explicit commands. An unavailable model leaves the safe default.
        decided = self._ask_model(text)
        if decided is not None:
            return decided

        # 10. The safe default is a conversation: the turn is answered, and work
        #    is not started on a guess. A *weak* work hint ("можешь ...", "нужно
        #    ...") earns one clarifying question, because the person may have
        #    meant a task; a plain message gets a plain answer with no question.
        hint = _mentions(text, _WORK_HINTS)
        if hint:
            return Intent("chat", f"возможная задача без явной команды («{hint}»)",
                          "default", 0.5,
                          clarifying="Это вопрос или задача, которую нужно выполнить? "
                                     "Если задачу — включите режим «Автономно» и повторите.")
        return Intent("chat", "намерение неясно; по умолчанию — ответ в чате",
                      "default", 0.4)

    # ------------------------------------------------------------------ model
    def _ask_model(self, text: str) -> Intent | None:
        """Ask the cascade to classify a short message. ``None`` if it cannot.

        A provider that is missing, fails, or answers something unexpected
        returns ``None``, and the caller keeps the safe default (chat). The
        classification never starts work on its own.
        """
        if self.providers is None:
            return None
        if len(text) > 600:
            # A long message is a specification; classifying it is the planner's
            # job, and the rules already cover an explicit command.
            return None
        try:
            if not self.providers.configured():
                return None
            response = self.providers.text(text, _CLASSIFY_SYSTEM)
            answer = (response.data.get("text") or "").strip().lower()
        except Exception:  # noqa: BLE001 - a router must never break a turn
            return None
        for mode in ROUTED_MODES:
            if re.search(rf"\b{mode}\b", answer):
                return Intent(mode, f"модель классифицировала как «{mode}»",
                              "model", 0.6)
        return None


_CLASSIFY_SYSTEM = (
    "You classify a developer's message to an AI coding assistant into exactly "
    "one word: 'chat' (a question or a request for an explanation), 'analysis' "
    "(explain what is wrong or why, without changing code), or 'autonomous' "
    "(an explicit request to perform work in the repository). Reply with one "
    "word only. When unsure, reply 'chat'."
)
