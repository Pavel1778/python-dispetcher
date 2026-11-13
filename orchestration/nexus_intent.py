"""The Intent Router: decide what a chat message *asks for* before acting.

The defect this fixes: NEXUS used to treat a chat message as work to start
whenever the conversation's mode was ``autonomous``. A person who asked "почему
прогон ломается?" in that conversation got an autonomous run started for the
question. The mode was a label on the *conversation*, not a decision about the
*message*, so a question could not be asked without launching agents.

This module makes the decision explicit and deterministic. It classifies one
message into one of three intents:

* ``chat``       - answer in the chat. A question, an explanation, a greeting,
                   a discussion, an opinion. Nothing is started.
* ``analysis``   - look at the real state (the project, the runs, the logs) and
                   answer in the chat. Read-only: it never changes code and
                   never starts a run.
* ``autonomous`` - a real engineering task: implement, fix, refactor, run.
                   Only this intent may start an autonomous run (or a code
                   workflow), and only when the message also asks for work to be
                   *done*.

The classification is a small set of ordered rules, not a keyword match. The
same word ("проверь", "проект", "код") appears in a question and in an order, so
the *shape* of the message decides. Read :func:`classify` for the order.

The router never *starts* anything itself; it returns a decision. The caller
(the chat turn) is what acts on it, and it acts conservatively: a decision the
router marks ambiguous does not start a run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The intents a message can have.
CHAT = "chat"
ANALYSIS = "analysis"
AUTONOMOUS = "autonomous"

#: The modes a conversation may be *set* to. ``analysis`` is a real mode (it
#: reads and answers); ``auto`` is the default that lets the router decide per
#: message, so a question in an "auto" conversation stays a question. ``manual``
#: is the deliberate mode: it discusses, proposes a plan and runs only on an
#: explicit confirmation - a work order proposes a plan instead of starting.
CONVERSATION_MODES = ("ask", "analysis", "plan", "code", "autonomous", "auto", "manual")

#: The modes that mean "the person chose this; do not start work".
_QUIET_MODES = ("ask", "plan", "analysis")

#: The mode that must confirm before running. A work order in it is planned, not
#: started (``effective_mode`` turns it into ``plan``).
_MANUAL = "manual"

#: Question marks, in the scripts this project sees.
_QUESTION_MARK = re.compile(r"[?？]")

#: Openers that make a message a question whatever else it contains. Anchored to
#: the start (after optional polite words) so "почему" inside a sentence does
#: not by itself make an order a question.
_INTERROGATIVE = re.compile(
    r"^\s*(?:"
    r"почему|зачем|отчего|что|чё|чем|как|какой|какая|какие|каков|когда|где|куда|откуда|"
    r"кто|кого|кому|сколько|можно\s+ли|есть\s+ли|будет\s+ли|стоит\s+ли|"
    r"why|what|how|when|where|which|who|whom|whose|is|are|does|do|did|can|could|should|"
    r"would|will|may|might"
    r")\b", re.IGNORECASE)

#: "Why did X happen / why is X broken" - a question that needs the real state
#: looked at. A plain "what is done?" does not need this and stays chat.
_WHY = re.compile(r"\b(?:почему|зачем|отчего|why|how\s+come)\b", re.IGNORECASE)

#: Requests to explain / discuss / advise / show - a conversation, not work.
_DISCUSS = re.compile(
    r"\b(?:"
    r"объясни|объясните|поясни|расскажи|опиши|обсуди|обсудим|поговорим|"
    r"что\s+думаешь|твоё\s+мнение|твое\s+мнение|посоветуй|подскажи|"
    r"помоги\s+понять|помоги\s+разобраться|"
    r"explain|describe|discuss|advise|suggest|tell\s+me|what\s+do\s+you\s+think|"
    r"opinion|thoughts?\s+on"
    r")\b", re.IGNORECASE)

#: Requests to look at / analyse / investigate real state. Read-only.
_ANALYSIS = re.compile(
    r"\b(?:"
    r"проанализируй|проанализир|анализ|исследуй|изучи|разберись|разбери|"
    r"посмотри|погляди|взгляни|проверь|проверить|найди|найти|выясни|"
    r"сравни|сравнить|оцени|диагностир|"
    r"analyz|analys|investigat|examine|inspect|review|compare|diagnos|"
    r"look\s+at|find\s+out|figure\s+out|trace"
    r")", re.IGNORECASE)

#: The analysis verbs, anchored to the start: the first word decides, so
#: "Посмотри логи и объясни проблему" is analysis, not a discussion.
_ANALYSIS_LEAD = re.compile(
    r"^\s*(?:"
    r"проанализируй|проанализир|анализ|исследуй|изучи|разберись|разбери|"
    r"посмотри|погляди|взгляни|проверь|проверить|найди|найти|выясни|"
    r"сравни|сравнить|оцени|диагностир|"
    r"analyz|analys|investigat|examine|inspect|review|compare|diagnos|"
    r"look\s+at|find\s+out|figure\s+out|trace"
    r")", re.IGNORECASE)

#: Imperative engineering verbs - an order to *do* work. Chosen as stems so a
#: word ending does not matter; deliberately does not include read-only verbs.
_ORDER = re.compile(
    r"(?:^|[\s,;:(\-—–])(?:"
    r"реализу|реализова|"
    r"исправ|почин|поправь|"
    r"добавь|добави|внедри|"
    r"сделай|сделать|создай|созда|напиши|написа|"
    r"удали|убери|убра|замени|замен|перепиши|переписа|"
    r"отрефактори|рефакторинг|рефакторь|переработай|"
    r"обнови|обнов|улучши|улучш|оптимизиру|оптимизир|доработа|"
    r"запусти|запуска|перезапусти|"
    r"выполни|выполн|собери|собра|разверни|задеплой|"
    r"покрой|покры|протестируй|настрой|настро|подключи|подключ|"
    r"продолж|доведи|заверши|закончи|"
    r"implement|fix|add|create|write|remove|delete|refactor|rewrite|"
    r"update|improve|optimiz|upgrade|migrate|deploy|build|run|rerun|"
    r"continue|finish|complete|carry\s+out"
    r")", re.IGNORECASE)

#: Words that mark a message as referring to work already under way.
_CONTINUE = re.compile(
    r"\b(?:продолж\w*|возобнови\w*|continue|resume|carry\s+on|go\s+on|keep\s+going)\b",
    re.IGNORECASE)

#: A greeting or a bare courtesy - a conversation opener, never work.
_GREETING = re.compile(
    r"^\s*(?:привет|здравствуй|здравствуйте|добрый\s+день|добрый\s+вечер|"
    r"хай|ку|спасибо|благодарю|ок|окей|ok|okay|hello|hi|hey|thanks|thank\s+you)"
    r"[\s!.,:)-]*$", re.IGNORECASE)

#: A request for a plan, answered in the chat (the "plan" reply), never a run.
_PLAN = re.compile(
    r"\b(?:предложи\s+план|составь\s+план|спланируй|составить\s+план|"
    r"план\s+улучшени|suggest\s+a\s+plan|make\s+a\s+plan)\b",
    re.IGNORECASE)

#: Words that make a request about the *state of work* (runs, progress, status).
#: Stems on purpose: "ошибка", "прогон", "задача" and their forms must all
#: match, so no trailing word boundary is written after a stem.
_STATE = re.compile(
    r"(?:готов|сдела|прогресс|статус|состояни|"
    r"прогон|задач|выполнени|"
    r"останови|остановл|упал|сломал|ломается|ошибк|падает|заверши|закончи|"
    r"done|ready|status|progress|\bruns?\b|\btasks?\b|finished|stopped|failed|error)",
    re.IGNORECASE)

#: An order phrased as a direct command. This is the narrow case that lets
#: "исправь эту ошибку" be work while "почему не исправил?" stays a question:
#: the order must be at the *start* of the message (after an optional polite
#: prefix), not quoted inside a question.
_EXPLICIT_ORDER = re.compile(
    r"^\s*(?:пожалуйста[,\s]+|давай[,\s]+|ну[,\s]+|please[,\s]+|now[,\s]+)?"
    r"(?:"
    r"реализуй|исправь|почини|поправь|добавь|внедри|сделай|создай|напиши|"
    r"удали|убери|замени|перепиши|отрефактори|переработай|обнови|улучши|"
    r"оптимизируй|доработай|запусти|перезапусти|выполни|собери|разверни|"
    r"задеплой|покрой|протестируй|настрой|подключи|доведи|"
    r"заверши|закончи|продолж(?:ай|и|ите|ать|айте)|"
    r"implement|fix|add|create|write|remove|delete|refactor|rewrite|update|"
    r"improve|optimiz|upgrade|migrate|deploy|build|run|rerun|continue|finish|"
    r"complete"
    r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Intent:
    """What one message asks for, and why.

    ``intent`` is ``chat``/``analysis``/``autonomous``. ``ambiguous`` is True
    when the message is not clearly a question and not clearly an order, so the
    caller should ask one clarifying question rather than start work.
    ``continue_run`` is True when the message asks to carry on work already
    started. ``reason`` is a short human-readable note for the caller/logs.
    """

    intent: str
    ambiguous: bool = False
    continue_run: bool = False
    question: bool = False
    reason: str = ""

    @property
    def starts_work(self) -> bool:
        return self.intent == AUTONOMOUS and not self.ambiguous


#: A message longer than this is a document (a specification, a brief), not a
#: remark to be asked about: a long text that mixes verbs is read, not
#: intercepted with a clarifying question.
_SHORT_CHARS = 600


def _short(text: str) -> bool:
    return len(text) <= _SHORT_CHARS


def _looks_like_question(text: str) -> bool:
    if _QUESTION_MARK.search(text):
        return True
    return bool(_INTERROGATIVE.match(text))


def _has_explicit_order(text: str) -> bool:
    """Whether *text* begins with a direct order to do work.

    "исправь ошибку" -> yes. "почему прогон ломается?" -> no. "ты можешь
    исправить?" -> no (the order is not at the start). This is what keeps a
    question a question while still letting an imperative order through.
    """
    return bool(_EXPLICIT_ORDER.match(text))


def _residual_after_continue(text: str) -> str:
    """*text* with the continuation cue and polite filler removed.

    A bare "Продолжи" leaves nothing; "Продолжай работу до завершения" leaves
    "работу до завершения". The residual decides whether a continuation is an
    order to start something or a question about what to continue.
    """
    stripped = _CONTINUE.sub(" ", text)
    stripped = re.sub(r"^\s*(?:пожалуйста|давай|ну|please|now)\b", " ", stripped,
                      flags=re.IGNORECASE)
    return " ".join(stripped.split())


def classify(text: str, *, mode: str | None = None, has_active_run: bool = False) -> Intent:
    """Classify one chat message.

    *mode* is the conversation's chosen mode, used only as a *hint* for the
    ambiguous case: an explicit ``ask``/``plan``/``analysis`` mode never starts
    an autonomous run, whatever the words say, because the person chose that
    mode. ``auto`` (and ``autonomous``) leave the decision to the message.
    *has_active_run* lets "продолжи" be recognised as continuing real work.

    The rules are ordered; the first that matches decides. Read them as: a
    question is never an order; an order is only an order when it names work.
    """
    raw = (text or "").strip()
    if not raw:
        return Intent(CHAT, question=True, reason="empty")
    mode = (mode or "auto").lower()
    if mode not in CONVERSATION_MODES:
        mode = "auto"

    question = _looks_like_question(raw)
    greeting = bool(_GREETING.match(raw))
    discuss = bool(_DISCUSS.search(raw))
    analysis = bool(_ANALYSIS.search(raw))
    order = bool(_ORDER.search(raw))
    continue_run = bool(_CONTINUE.search(raw))
    why = bool(_WHY.search(raw))
    state = bool(_STATE.search(raw))
    explicit_order = _has_explicit_order(raw)
    analysis_lead = bool(_ANALYSIS_LEAD.match(raw))

    # 1. A greeting is a conversation.
    if greeting:
        return Intent(CHAT, reason="greeting")

    # 2. An explicit non-execution mode is the person's own choice: respect it.
    #    ("analysis" mode still reads; the others answer.)
    if mode in _QUIET_MODES:
        if mode == "analysis" or (analysis and not explicit_order):
            return Intent(ANALYSIS, reason=f"mode {mode}, read-only request")
        return Intent(CHAT, reason=f"mode {mode}")

    # 3. A discussion request is a conversation, even when it names an order
    #    verb ("давай обсудим, как лучше реализовать") - unless the message
    #    opens with a direct order ("реализуй и объясни, как") or with a
    #    read-only verb ("посмотри логи и объясни проблему" is analysis).
    if discuss and not explicit_order:
        if analysis_lead:
            return Intent(ANALYSIS, reason="read-only request")
        return Intent(CHAT, reason="discussion")

    # 4. "Продолжи..." is about work already under way.
    if continue_run:
        if has_active_run:
            return Intent(AUTONOMOUS, continue_run=True, reason="continue existing run")
        residual = _residual_after_continue(raw)
        if explicit_order and residual:
            # "Продолжай работу до завершения": an order that names the work.
            return Intent(AUTONOMOUS, continue_run=True, reason="continue named work")
        # A bare "Продолжи" with nothing to continue is a question, not a task.
        return Intent(CHAT, ambiguous=not question, question=question,
                      reason="continue with no known run")

    # 5. A question is answered, never executed - even when it contains an
    #    order verb ("почему прогон ломается?", "почему ты не исправил это?").
    #    A question that asks *why* about real state, or uses an analysis verb,
    #    is analysis; any other question is chat. A question whose *only* trace
    #    of work is an order verb buried inside it ("а не лучше ли переписать?")
    #    is ambiguous: ask, do not act.
    if question and not explicit_order:
        if analysis or (why and state):
            return Intent(ANALYSIS, question=True, reason="question about real state")
        if order and not discuss and _short(raw):
            return Intent(CHAT, ambiguous=True, question=True, reason="question with a buried order")
        return Intent(CHAT, question=True, reason="question")

    # 6. A request for a plan is answered in the chat.
    if _PLAN.search(raw) and not explicit_order:
        return Intent(CHAT, reason="plan request")

    # 7. A read-only request ("проанализируй", "найди причину") is analysis.
    if analysis and not order:
        return Intent(ANALYSIS, reason="read-only request")
    if analysis and order and not explicit_order and _short(raw):
        # "проверь и исправь?" - an order hidden in a read-only phrase: ask.
        # A *long* text that mixes read-only and order verbs is a specification,
        # not an ambiguous remark: it is read (analysis), never intercepted.
        return Intent(ANALYSIS, ambiguous=True, reason="mixed read-only and order")

    # 8. An order to do work.
    if order:
        return Intent(AUTONOMOUS, reason="order to perform work")

    # 9. Nothing matched: a statement, an opinion, a remark, a greeting that was
    #    not bare. It carries no order and no read-only verb, so it is answered
    #    in the chat. It is *not* flagged ambiguous: ambiguity is reserved for a
    #    message that does carry a work signal but whose shape leaves it unclear
    #    (see the buried-order case above), because only there would asking be
    #    better than answering.
    return Intent(CHAT, reason="statement")


def effective_mode(mode: str | None, intent: Intent) -> str:
    """The mode the *turn* should run in, given the conversation's mode.

    An explicit ``ask``/``plan``/``analysis``/``code`` conversation keeps its
    mode. ``manual`` is the deliberate mode: a work order becomes a *plan* (the
    caller proposes it and waits for a confirmation) - except a continuation of
    work already approved, which is handled by the caller. An ``autonomous`` or
    ``auto`` conversation runs the mode the router chose, so a question in it is
    answered instead of starting a run.
    """
    mode = (mode or "auto").lower()
    if mode in ("ask", "plan"):
        return mode
    if mode == "analysis":
        # The person chose "Разбор", which is read-only by definition: it never
        # starts a run, even when the message is phrased as an order. Work is
        # asked for in "Авто" or "Автономно" mode.
        return "analysis"
    if mode == "manual":
        # The person chose "Вручную": they want to see and approve the plan.
        # An order to do work becomes a plan request; a question is answered;
        # a continuation is left for the caller (it resumes the approved run).
        if intent.continue_run:
            return "manual"
        if intent.starts_work or (intent.intent == AUTONOMOUS and not intent.question):
            return "plan"
        return "analysis" if intent.intent == ANALYSIS else "ask"
    if mode == "code":
        # The person chose "Code" (one OpenHands workflow). An order stays a
        # code task; a question is still a question and is answered, not run.
        if intent.starts_work:
            return "code"
        return "analysis" if intent.intent == ANALYSIS else "ask"
    if mode == "autonomous":
        # The person chose "Автономно": they are asking for work. A question is
        # still answered and an explicit discussion is still a discussion, but
        # anything else - including a stated goal with no order verb ("Цель: 25
        # задач") - is honoured as work: they selected the mode, so an unclear
        # goal runs rather than being turned into a chat reply.
        if intent.question:
            return "analysis" if intent.intent == ANALYSIS else "ask"
        if intent.intent == CHAT and intent.reason == "discussion":
            return "ask"
        return "autonomous"
    # auto: the message alone decides.
    if intent.starts_work:
        return "autonomous"
    if intent.intent == ANALYSIS:
        return "analysis"
    return "ask"


def clarifying_question() -> str:
    """One short question to ask when the intent is ambiguous.

    The requirement is explicit: do not start a run on an unclear message, and
    do not turn every message into a form. One sentence, with the two real
    options, is what is sent.
    """
    return ("Уточните, пожалуйста: объяснить и обсудить это в чате, "
            "или сразу выполнить работу и запустить автономный прогон?")
