"""The chat-first workflow: discuss, plan, then run only on an explicit command.

The requirement this implements: NEXUS is an AI developer *and* a technical
interlocutor. A normal message is a message in the dialogue, never an order to
start work. Work starts only after the person has seen the plan and said so in
so many words.

The decision about *what a message asks for* is :mod:`orchestration.nexus_intent`
(a question is never an order). This module adds the two things the intent
router deliberately does not decide:

* whether a message is an **explicit launch command** ("Запускай", "Execute",
  "Run it") - and whether it is a *question about* launching ("когда
  запустишь?"), a *prohibition* ("не запускай пока") or a *condition* ("запускай
  только после проверки"), none of which start anything;
* the **dialogue state machine** that survives a restart: a conversation is in
  one of :data:`STATES`, and EXECUTING is reachable only from
  AWAITING_APPROVAL. Nothing here auto-promotes DISCOVERY/CLARIFICATION/PLANNING
  to EXECUTING.

The state is stored on the conversation row (``nexus_conversations.workflow_state``,
migration 0034), so the same decision is made after a reload or a restart.

This module is pure: it classifies text and validates transitions. The caller
(the chat turn) is what persists the state and starts the run.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

#: The dialogue states. The names are the requirement's own.
DISCOVERY = "DISCOVERY"
CLARIFICATION = "CLARIFICATION"
PLANNING = "PLANNING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
EXECUTING = "EXECUTING"
REPORTING = "REPORTING"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

STATES: tuple[str, ...] = (
    DISCOVERY, CLARIFICATION, PLANNING, AWAITING_APPROVAL,
    EXECUTING, REPORTING, COMPLETED, CANCELLED, FAILED,
)

#: States from which no further transition happens without a new request.
TERMINAL: frozenset[str] = frozenset({COMPLETED, CANCELLED, FAILED})

#: The states a run is at work in.
ACTIVE: frozenset[str] = frozenset({EXECUTING, REPORTING})

#: The states in which a *new* plan may be produced from a message.
_NEGOTIATING: frozenset[str] = frozenset({DISCOVERY, CLARIFICATION, PLANNING})

#: The legal transitions. Asking for the state a conversation is already in is a
#: no-op, not an error. A transition not listed here is refused.
_ALLOWED: dict[str, frozenset[str]] = {
    DISCOVERY: frozenset({DISCOVERY, CLARIFICATION, PLANNING, AWAITING_APPROVAL,
                          CANCELLED, FAILED}),
    CLARIFICATION: frozenset({CLARIFICATION, DISCOVERY, PLANNING,
                              AWAITING_APPROVAL, CANCELLED, FAILED}),
    PLANNING: frozenset({PLANNING, CLARIFICATION, DISCOVERY, AWAITING_APPROVAL,
                         CANCELLED, FAILED}),
    AWAITING_APPROVAL: frozenset({AWAITING_APPROVAL, PLANNING, CLARIFICATION,
                                  DISCOVERY, EXECUTING, CANCELLED, FAILED}),
    # EXECUTING is reachable only from AWAITING_APPROVAL. This is the invariant
    # the requirement is about: a discussion cannot become execution.
    EXECUTING: frozenset({EXECUTING, REPORTING, COMPLETED, CANCELLED, FAILED,
                          AWAITING_APPROVAL}),
    REPORTING: frozenset({REPORTING, COMPLETED, CANCELLED, FAILED, EXECUTING}),
    COMPLETED: frozenset({COMPLETED, DISCOVERY, PLANNING, AWAITING_APPROVAL,
                          EXECUTING}),
    CANCELLED: frozenset({CANCELLED, DISCOVERY, PLANNING, AWAITING_APPROVAL,
                          EXECUTING}),
    FAILED: frozenset({FAILED, DISCOVERY, PLANNING, AWAITING_APPROVAL, EXECUTING}),
}


def can_transition(current: str, target: str) -> bool:
    """Whether a conversation may move from *current* to *target*."""
    if target not in STATES:
        return False
    if current not in STATES:
        current = DISCOVERY
    return target in _ALLOWED[current]


def check_transition(current: str, target: str) -> str:
    """*target* if the move is legal, else ``ValueError``."""
    if not can_transition(current, target):
        raise ValueError(f"a {current} conversation cannot become {target}")
    return target


#: A direct command to start the work that was agreed. Matched at the start of
#: the message (after optional polite filler) so "когда запустишь?" - which is
#: not at the start, and carries a question mark - is not one.
_LAUNCH = re.compile(
    r"^\s*(?:пожалуйста[,\s]+|давай[,\s]+|ну[,\s]+|ок[,\s]+|окей[,\s]+|"
    r"хорошо[,\s]+|да[,\s]+|please[,\s]+|now[,\s]+|ok[,\s]+|okay[,\s]+|"
    # "не" is allowed here so "Не запускай пока" is *recognised* as a launch
    # phrase and then refused by the prohibition check below. Without it the
    # regex would simply not match, and a prohibition would be indistinguishable
    # from a message that never mentioned launching.
    r"не[,\s]+|don'?t[,\s]+|do\s+not[,\s]+)?"
    r"(?:"
    r"запускай|запускайте|запусти(?:ть)?(?:\s+задачу)?|запускаем|"
    r"начинай|начинайте|начни|начинаем|"
    r"приступай|приступайте|приступаем|"
    r"выполняй|выполняйте|выполни|выполняем|"
    r"старт|поехали|погнали|вперёд|вперед|"
    r"start|go|begin|proceed|execute|run(?:\s+it)?|"
    r"do\s+it|let'?s\s+go|carry\s+on"
    r")\b", re.IGNORECASE)

#: A prohibition - an explicit "do not start yet". Stems on purpose: no trailing
#: word boundary, so "не запускай" (where "запуска" is followed by "й", another
#: word character) is still a prohibition.
_PROHIBITION = re.compile(
    r"(?:не\s+запуска|не\s+начина|не\s+выполня|не\s+приступа|не\s+старту|"
    r"подожд|погоди|пока\s+не|ещё\s+не|еще\s+не|"
    r"don'?t\s+(?:start|run|begin|execute)|not\s+yet|hold\s+on|wait\s+for)",
    re.IGNORECASE)

#: A condition that must hold before the launch ("запускай только после проверки",
#: "запусти, когда тесты пройдут"). A launch with a condition is not a launch.
_CONDITION = re.compile(
    r"\b(?:только\s+(?:после|когда|если)|после\s+того\s+как|при\s+условии|"
    r"если\s+(?:всё|все|тесты|сборка|проверк)|когда\s+(?:тесты|сборка|всё|все|"
    r"проверк|законч|заверш|пройд)|"
    r"only\s+(?:after|when|if)|once\s+(?:the\s+)?tests?|when\s+the\s+tests?)\b",
    re.IGNORECASE)

#: A question *about* launching - "когда запустишь?", "запустишь?", "ты запустил?".
_LAUNCH_QUESTION = re.compile(r"[?？]")


@dataclass(frozen=True)
class Launch:
    """What a message says about starting the agreed work.

    ``command`` is True only for an explicit launch. ``question``,
    ``prohibition`` and ``conditional`` are the three shapes that *look* like a
    launch but are not one, each named so the caller can answer it in the chat.
    ``reason`` is a short note for logs and tests.
    """

    command: bool = False
    question: bool = False
    prohibition: bool = False
    conditional: bool = False
    reason: str = ""

    @property
    def is_command(self) -> bool:
        return self.command and not (self.question or self.prohibition or self.conditional)


def classify_launch(text: str) -> Launch:
    """Decide whether *text* is an explicit command to start the agreed work.

    The order of the checks is the requirement's: a prohibition and a condition
    are not commands; a question is not a command. Only a bare, unconditional
    imperative is one.
    """
    raw = (text or "").strip()
    if not raw:
        return Launch(reason="empty")
    prohibition = bool(_PROHIBITION.search(raw))
    conditional = bool(_CONDITION.search(raw))
    question = bool(_LAUNCH_QUESTION.search(raw))
    matches = bool(_LAUNCH.match(raw))
    if not matches:
        return Launch(question=question, prohibition=prohibition,
                      conditional=conditional, reason="not a launch phrase")
    # "Запускай только после проверки": the condition wins over the imperative.
    if conditional:
        return Launch(conditional=True, prohibition=prohibition, reason="conditional launch")
    if prohibition:
        return Launch(prohibition=True, reason="prohibition")
    if question:
        # "Запустить?" / "Запускай?" - a question about launching, not a launch.
        return Launch(question=True, reason="question about launching")
    return Launch(command=True, reason="explicit launch command")


def approval_matches(current_hash: str | None, approved_hash: str | None) -> bool:
    """Whether the plan a person approved is the plan on offer *now*.

    The confirmation is bound to a plan *version*: if the plan changed since it
    was approved (a replan after a requirement change), the stored approval no
    longer matches and the new version must be approved again.
    """
    if not current_hash or not approved_hash:
        return False
    return current_hash == approved_hash


def plan_hash_of(plan: list[dict]) -> str:
    """A stable version hash of a plan payload.

    Computed over the validated task list, key-sorted and separated, so the same
    plan always hashes the same and any change to a title, a dependency or an
    acceptance criterion produces a different hash. Written with the plan, so
    "which version did the person approve" is answerable exactly.
    """
    blob = json.dumps(plan or [], sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render_plan(plan: list[dict], *, repository: str = "", branch: str = "") -> str:
    """The plan as a person reads it in the chat, before approving it.

    Numbered tasks with their dependencies and acceptance criteria, and a line
    saying what will be touched. The text is a proposal: nothing has started.
    """
    lines = ["**Предлагаемый план**", ""]
    for i, task in enumerate(plan or [], 1):
        deps = task.get("dependencies") or []
        dep = f" _(после: {', '.join(deps)})_" if deps else ""
        lines.append(f"{i}. **{task.get('title') or task.get('id')}**{dep}")
        if task.get("description"):
            lines.append(f"   {task['description']}")
        acceptance = task.get("acceptance") or []
        if acceptance:
            lines.append("   Критерии: " + "; ".join(str(a) for a in acceptance))
    where = f"`{repository}`" + (f" · ветка `{branch}`" if branch else "")
    lines += ["", f"Затрагивается: {where}." if repository else
              "Репозиторий к проекту не привязан — работа не сможет быть запущена.",
              "", "_План ещё не запущен. Чтобы начать, напишите «Запускай»._"]
    return "\n".join(lines)
