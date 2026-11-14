"""The chat command system: slash commands, hashtags and typed parameters.

One parser, one registry, one vocabulary. A message typed in the chat is
parsed *before* the Intent Router sees it, so ``/plan #backend #security`` is
a plan of a backend security task and not a sentence to guess at. The same
parser serves the interface buttons: a button posts ``{"action": "/plan"}``
and the server runs it through exactly this code, so a button and a slash
command can never drift apart (the requirement's "one action mechanism").

Nothing here talks to a database, a provider or the network: it is a pure
function from a message to a decision, which is what makes it testable and
what keeps the vocabulary in one place. The caller decides what to do with
the result.

Vocabulary
----------
*Commands* (``/name``) are of two kinds. A **mode** command picks how the
turn runs (``/chat`` answers, ``/plan`` plans, ``/autorun`` executes). A
**task** command names the kind of work (``/audit``, ``/fix``, ``/test``,
``/review``, ``/deploy``, ``/research``, ``/status``). One of each may be
combined - ``/plan /audit`` is a plan for an audit - and the mode command
wins for the mode while the task command wins for the task type.

*Hashtags* (``#name``) are of four groups: technology, quality, execution and
restriction. They travel with the request as metadata for the planner and the
executors; the restriction tags are enforced (``#read-only`` forbids a change,
``#no-merge``/``#no-deploy``/``#no-delete`` forbid those operations).

**Restrictions have priority.** ``#read-only`` beside a command that would
change code does not fail: it wins, the command is downgraded to a read-only
analysis, and the parser says so in ``warnings``. A restriction that cannot be
honoured at all - ``/deploy #no-deploy`` - is an ``errors`` entry and ``ok`` is
``False``, because no downgrade makes it true.

*Parameters* (``--name value`` or ``--name=value``) are typed and validated
per command; an unknown parameter or a wrong type is an ``errors`` entry.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------- tags

#: Tag group -> the tags it holds, in the order the interface should show them.
TAG_GROUPS: dict[str, tuple[str, ...]] = {
    "technology": ("frontend", "backend", "database", "api", "infrastructure",
                   "devops", "mobile", "desktop"),
    "quality": ("testing", "security", "performance", "refactoring",
                "documentation", "accessibility"),
    "execution": ("parallel", "sequential", "deep", "fast", "critical"),
    "restriction": ("no-merge", "no-deploy", "no-delete", "read-only"),
}

#: tag -> group, built from the table above.
TAG_GROUP: dict[str, str] = {
    tag: group for group, tags in TAG_GROUPS.items() for tag in tags}

#: The tags that limit what the work may do. They are enforced, not advisory.
RESTRICTION_TAGS = frozenset(TAG_GROUPS["restriction"])

#: Human labels, Russian, for the interface. Not translated server-side.
TAG_LABELS: dict[str, str] = {
    "frontend": "фронтенд", "backend": "бэкенд", "database": "база данных",
    "api": "API", "infrastructure": "инфраструктура", "devops": "DevOps",
    "mobile": "мобильные", "desktop": "десктоп",
    "testing": "тесты", "security": "безопасность",
    "performance": "производительность", "refactoring": "рефакторинг",
    "documentation": "документация", "accessibility": "доступность",
    "parallel": "параллельно", "sequential": "последовательно",
    "deep": "глубоко", "fast": "быстро", "critical": "критично",
    "no-merge": "без слияния", "no-deploy": "без деплоя",
    "no-delete": "без удаления", "read-only": "только чтение",
}

# --------------------------------------------------------------- parameters


@dataclass(frozen=True)
class Param:
    """One typed parameter a command accepts."""

    name: str
    kind: str            # "str" | "int" | "bool"
    summary: str
    required: bool = False

    def coerce(self, raw: str) -> Any:
        """*raw* as the declared type, or ``ValueError`` naming the problem."""
        if self.kind == "str":
            return raw
        if self.kind == "int":
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"--{self.name} must be a whole number") from None
        if self.kind == "bool":
            lowered = str(raw).strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"--{self.name} must be true or false")
        raise ValueError(f"unknown parameter type {self.kind!r}")


# ---------------------------------------------------------------- commands

#: The effect a command has on the repository, used for restriction checks.
#: "none" answers in words, "plan" only proposes, "execute" starts real work
#: and may change files.
NONE, PLAN, EXECUTE = "none", "plan", "execute"


@dataclass(frozen=True)
class Command:
    """One slash command."""

    name: str                       # without the leading slash
    kind: str                       # "mode" | "task" | "meta"
    summary: str
    group: str
    example: str
    mode: str | None = None         # the chat mode this command runs in
    task_type: str | None = None    # READ_ONLY / IMPLEMENTATION / ... (planner hint)
    effect: str = NONE
    read_only: bool = False         # the command never changes code by itself
    params: tuple[Param, ...] = ()
    aliases: tuple[str, ...] = ()
    hidden: bool = False            # shown only on request (e.g. /help)

    def label(self) -> str:
        return "/" + self.name


_COMMON = (
    Param("branch", "str", "ветка вместо выбранной у проекта"),
    Param("repo", "str", "репозиторий вида owner/name"),
)


def _command(name: str, kind: str, summary: str, group: str, example: str, **kw: Any) -> Command:
    return Command(name=name, kind=kind, summary=summary, group=group, example=example, **kw)


#: The registry. A new command is one entry; nothing else has to change. The
#: order here is the order the interface shows them in.
COMMANDS: dict[str, Command] = {c.name: c for c in (
    _command("chat", "mode", "Ответ ассистента. Ничего не запускает.", "режим",
             "/chat объясни архитектуру модуля", mode="ask"),
    _command("plan", "mode", "План работ с зависимостями. Ничего не запускает.",
             "режим", "/plan #backend #security починить авторизацию",
             mode="plan", effect=PLAN),
    _command("autorun", "mode", "Автономный прогон: агенты выполняют работу.",
             "режим", "/autorun #frontend #testing починить форму входа",
             mode="autonomous", effect=EXECUTE),
    _command("audit", "task", "Аудит: разбор без изменений.", "задача",
             "/audit #deep #security", task_type="audit", read_only=True,
             params=_COMMON),
    _command("fix", "task", "Исправление дефекта.", "задача",
             "/fix #critical #backend #testing", task_type="bugfix", effect=EXECUTE,
             params=_COMMON),
    _command("test", "task", "Написать или починить тесты.", "задача",
             "/test #backend", task_type="testing", effect=EXECUTE, params=_COMMON),
    _command("review", "task", "Ревью изменений без правок.", "задача",
             "/review #security", task_type="audit", read_only=True, params=_COMMON),
    _command("deploy", "task", "Развёртывание.", "задача",
             "/deploy #devops", task_type="deployment", effect=EXECUTE, params=_COMMON),
    _command("research", "task", "Изучить вопрос и отчитаться.", "задача",
             "/research #performance", task_type="audit", read_only=True, params=_COMMON),
    _command("status", "task", "Состояние проекта и прогонов.", "задача",
             "/status", task_type="audit", read_only=True),
    _command("help", "meta", "Список команд и тегов.", "прочее", "/help", hidden=True),
)}

#: Aliases resolve to a canonical command name.
ALIASES: dict[str, str] = {
    "ask": "chat", "question": "chat", "вопрос": "chat",
    "auto": "autorun", "run": "autorun", "авто": "autorun",
    "plans": "plan", "план": "plan",
    "bugfix": "fix", "исправить": "fix",
    "tests": "test", "тесты": "test",
    "audit-deep": "audit",
    "deploy": "deploy",
    "помощь": "help",
}

#: Commands that pick the mode of the turn.
MODE_COMMANDS = frozenset(c.name for c in COMMANDS.values() if c.kind == "mode")
#: Commands that name the kind of work.
TASK_COMMANDS = frozenset(c.name for c in COMMANDS.values() if c.kind == "task")

#: The mode a task command runs in when no mode command is given.
_TASK_DEFAULT_MODE: dict[str, str] = {
    "audit": "analysis", "review": "analysis", "research": "analysis",
    "status": "analysis", "fix": "autonomous", "test": "autonomous",
    "deploy": "autonomous",
}


@dataclass
class Parsed:
    """The decision a message produced. ``ok`` is False when it is unusable."""

    ok: bool = True
    commands: list[Command] = field(default_factory=list)
    unknown_commands: list[str] = field(default_factory=list)
    unknown_tags: list[str] = field(default_factory=list)
    tags: dict[str, list[str]] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    text: str = ""                       # the request, command/tag tokens removed
    mode: str | None = None              # the mode this turn runs in
    task_type: str | None = None         # planner hint
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    help: bool = False                   # /help was asked for

    @property
    def restrictions(self) -> set[str]:
        return set(self.tags.get("restriction", ()))

    def tag_list(self) -> list[str]:
        return [tag for group in TAG_GROUPS for tag in self.tags.get(group, ())]

    @property
    def primary(self) -> Command | None:
        for command in self.commands:
            if command.kind == "mode":
                return command
        return self.commands[0] if self.commands else None

    @property
    def task(self) -> Command | None:
        for command in self.commands:
            if command.kind == "task":
                return command
        return None


def _split_tokens(text: str) -> list[str]:
    """Split *text* into tokens as it was written.

    ``shlex`` is used with ``posix=False`` so a quoted value stays one token
    (``--repo "owner/name"``); a malformed quote falls back to a plain split.
    Newlines are ordinary separators here - the request itself is recovered
    from the raw text, not from these tokens.
    """
    try:
        lexer = shlex.shlex(text, posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return text.split()


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


#: A token that is a command attempt: ``/name`` where the name is word
#: characters or hyphens and nothing else follows. A path (``/tmp/file``) is
#: not a command attempt and is left in the request untouched.
_COMMAND_TOKEN_RE = re.compile(r"/[\w-]+\Z", re.UNICODE)
#: A token that is a tag attempt: ``#name``. ``#1234`` is a tag attempt too (it
#: is reported as unknown) but is left in the request.
_TAG_TOKEN_RE = re.compile(r"#[\w-]+\Z", re.UNICODE)


def _remove_tokens(text: str, literals: set[str]) -> str:
    """*text* with the exact *literals* removed, only where they stand alone.

    Working on the raw text (not on a token list) keeps newlines and internal
    spacing of a pasted specification exactly as they were; only the tokens
    that were recognised as NEXUS instructions are taken out. A token is only
    removed at a word boundary, so ``/tmp`` inside a path is never touched.

    The one and only guarantee this makes: when *nothing* was recognised the
    text is returned **byte for byte**. A specification with no command and no
    tag must reach the planner and the agents exactly as it was sent - that is
    what the long-task tests assert. When something *is* removed, only the
    token and the single whitespace character before it are taken, so no space
    the person wrote is ever collapsed.
    """
    if not literals:
        return text
    parts = sorted(literals, key=len, reverse=True)
    token_re = re.compile(r"(?<!\S)(?:" + "|".join(re.escape(p) for p in parts) + r")(?=\s|$)")
    out: list[str] = []
    last = 0
    removed_from_start = False
    for match in token_re.finditer(text):
        start, end = match.start(), match.end()
        if start < last:
            continue
        # Take one neighbouring whitespace character with the token, so "a
        # /plan b" becomes "a b" and not "a  b". The neighbour is not taken if
        # the previous removal already consumed it. Nothing else is touched: a
        # space the person wrote between two other words is never collapsed.
        if start > 0 and start - 1 >= last and text[start - 1] in " \t":
            start -= 1
        elif end < len(text) and text[end] in " \t":
            end += 1
        elif end < len(text) and text[end] == "\n":
            # A command on its own line: take the line break too, so the
            # request does not begin (or continue) with a blank line the
            # person never wrote.
            end += 1
        if start == 0 and not out:
            removed_from_start = True
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    result = "".join(out)
    return result.lstrip() if removed_from_start else result


def parse(text: str, *, mode: str | None = None) -> Parsed:
    """Read *text* into a :class:`Parsed` decision.

    *mode* is the conversation's mode; it is used only when no mode command
    is given, so an explicit ``/plan`` overrides the conversation's setting
    for this turn. The result is pure data: the caller applies it.
    """
    result = Parsed()
    raw = text or ""
    tokens = _split_tokens(raw)
    # Pass 1: commands and tags. What is left is the request, still as tokens
    # so that a parameter and its value stay adjacent.
    request_tokens: list[str] = []
    stripped: set[str] = set()          # the exact tokens recognised and removed
    mode_commands: list[Command] = []
    task_commands: list[Command] = []
    seen_commands: set[str] = set()
    for token in tokens:
        # Only a bare ``/word`` is a command attempt. ``/tmp/file.txt`` is a
        # path a person wrote, not a command, and stays in the request.
        if _COMMAND_TOKEN_RE.fullmatch(token):
            name = token[1:].strip().lower()
            canonical = ALIASES.get(name, name)
            command = COMMANDS.get(canonical)
            if command is None:
                result.unknown_commands.append(token)
                continue
            stripped.add(token)
            if canonical in seen_commands:
                continue
            seen_commands.add(canonical)
            if command.name == "help":
                result.help = True
            elif command.kind == "mode":
                mode_commands.append(command)
            else:
                task_commands.append(command)
            continue
        # Likewise only a bare ``#word`` is a tag; ``#1234`` (an issue) is not.
        if _TAG_TOKEN_RE.fullmatch(token):
            tag = token[1:].strip().lower()
            if tag in TAG_GROUP:
                stripped.add(token)
                result.tags.setdefault(TAG_GROUP[tag], [])
                if tag not in result.tags[TAG_GROUP[tag]]:
                    result.tags[TAG_GROUP[tag]].append(tag)
            else:
                result.unknown_tags.append(token)
            continue
        request_tokens.append(_unquote(token))

    result.commands = mode_commands + task_commands

    # Pass 2: parameters, validated against the chosen command's declarations.
    # ``--name=value`` and ``--name value`` are both accepted; a bare ``--name``
    # is a flag only for a boolean parameter.
    declared = {p.name: p for command in result.commands for p in command.params}
    index = 0
    while index < len(request_tokens):
        part = request_tokens[index]
        index += 1
        if not part.startswith("--") or len(part) <= 2:
            continue
        body = part[2:]
        name, sep, value = body.partition("=")
        name = name.strip().lower()
        param = declared.get(name)
        if param is None:
            result.errors.append(f"неизвестный параметр --{name}")
            continue
        removed = [part]
        if not sep:
            # A bare ``--name``: a flag for a boolean, otherwise the next token
            # is its value (``--repo owner/name``).
            if param.kind == "bool":
                value = "true"
            elif index < len(request_tokens):
                value = request_tokens[index]
                removed.append(value)
                index += 1
            else:
                result.errors.append(f"--{name} требует значение, например --{name}=значение")
                continue
        try:
            result.params[name] = param.coerce(value)
            stripped.update(removed)
        except ValueError as e:
            result.errors.append(str(e))

    # The request as it was written, minus exactly the recognised tokens.
    # Working on the raw text (not on the token list) keeps newlines and inner
    # spacing of a pasted specification exactly as they were.
    result.text = _remove_tokens(raw, stripped)

    if len(mode_commands) > 1:
        result.errors.append("можно указать только одну команду режима")
    if len(task_commands) > 1:
        result.errors.append("можно указать только одну задачу")

    # --- mode and task type, with restriction priority ---
    restrictions = result.restrictions
    if mode_commands:
        chosen_mode = mode_commands[0].mode
    elif task_commands:
        chosen_mode = _TASK_DEFAULT_MODE.get(task_commands[0].name, mode or "auto")
    else:
        chosen_mode = mode
    result.mode = chosen_mode
    if task_commands:
        result.task_type = task_commands[0].task_type

    # #read-only wins over anything that would change code: the work is
    # downgraded to a read-only analysis rather than refused, and the downgrade
    # is reported so the person is not surprised.
    if "read-only" in restrictions and any(c.effect == EXECUTE for c in result.commands):
        result.warnings.append(
            "#read-only: изменения запрещены, задача переведена в разбор без правок")
        result.mode = "analysis"
        result.task_type = "audit"
    # A restriction that no downgrade can satisfy is an error, because honouring
    # it is impossible: deploying without deploying is not a thing.
    if "no-deploy" in restrictions and any(c.name == "deploy" for c in result.commands):
        result.errors.append("/deploy несовместим с #no-deploy")
    if "no-merge" in restrictions and any(c.name == "deploy" for c in result.commands):
        result.warnings.append("#no-merge: слияние запрещено, деплой выполняется без слияния")

    result.ok = not result.errors
    return result


# ------------------------------------------------------------- suggestions


def registry() -> dict[str, Any]:
    """The whole vocabulary, JSON-serialisable, for the interface."""
    return {
        "commands": [
            {"name": c.name, "label": c.label(), "kind": c.kind, "group": c.group,
             "summary": c.summary, "example": c.example, "mode": c.mode,
             "task_type": c.task_type, "read_only": c.read_only,
             "aliases": list(c.aliases),
             "params": [{"name": p.name, "kind": p.kind, "summary": p.summary,
                         "required": p.required} for p in c.params]}
            for c in COMMANDS.values() if not c.hidden
        ],
        "tags": [
            {"name": tag, "group": group, "label": TAG_LABELS.get(tag, tag),
             "restriction": tag in RESTRICTION_TAGS}
            for group, tags in TAG_GROUPS.items() for tag in tags
        ],
        "groups": [{"name": group, "tags": list(tags)}
                   for group, tags in TAG_GROUPS.items()],
        "modes": sorted({c.mode for c in COMMANDS.values() if c.mode}),
        "help": help_text(),
    }


def suggest(prefix: str, *, limit: int = 20) -> list[dict[str, str]]:
    """Completions for a partial token *prefix* (with or without ``/``/``#``).

    A prefix beginning ``/`` completes commands; ``#`` completes tags; an
    empty prefix lists both. Matching is a case-insensitive substring of the
    name, so ``/au`` finds ``audit`` and ``#sec`` finds ``security``.
    """
    prefix = (prefix or "").strip()
    kind = None
    body = prefix
    if prefix.startswith("/"):
        kind, body = "command", prefix[1:]
    elif prefix.startswith("#"):
        kind, body = "tag", prefix[1:]
    body = body.lower()
    out: list[dict[str, str]] = []
    if kind in (None, "command"):
        for c in COMMANDS.values():
            if c.hidden:
                continue
            if body in c.name or any(body in a for a in c.aliases):
                out.append({"kind": "command", "value": c.label(),
                            "label": c.label(), "summary": c.summary})
    if kind in (None, "tag"):
        for group, tags in TAG_GROUPS.items():
            for tag in tags:
                if body in tag:
                    out.append({"kind": "tag", "value": "#" + tag, "label": "#" + tag,
                                "summary": TAG_LABELS.get(tag, tag), "group": group})
    return out[:limit]


def help_text() -> str:
    """The text ``/help`` answers with: every command and tag group."""
    lines = ["**Команды NEXUS**", ""]
    for kind, title in (("mode", "Режим (как выполняется)"), ("task", "Задача (что делать)")):
        lines.append(f"_{title}_")
        for c in COMMANDS.values():
            if c.hidden or c.kind != kind:
                continue
            lines.append(f"- `{c.label()}` — {c.summary}")
        lines.append("")
    lines.append("_Теги_")
    for group, tags in TAG_GROUPS.items():
        rendered = ", ".join(f"`#{t}`" for t in tags)
        lines.append(f"- {group}: {rendered}")
    lines.append("")
    lines.append("Пример: `/plan #backend #security починить вход в систему`")
    return "\n".join(lines)


def apply_to_text(text: str, parsed: Parsed) -> str:
    """The request with command and tag tokens removed (``parsed.text``).

    A message that was *only* commands has no request; the caller answers with
    a preview or with the help text instead of sending an empty prompt.
    """
    return parsed.text or ""


def preview(parsed: Parsed) -> str:
    """A short human summary of what the parsed message will do."""
    bits: list[str] = []
    if parsed.primary is not None:
        bits.append(f"команда: {parsed.primary.label()}")
    if parsed.task is not None and parsed.task is not parsed.primary:
        bits.append(f"задача: {parsed.task.label()}")
    if parsed.mode:
        bits.append(f"режим: {parsed.mode}")
    tags = parsed.tag_list()
    if tags:
        bits.append("теги: " + " ".join("#" + t for t in tags))
    return "; ".join(bits)


def known_names() -> Iterable[str]:
    """Every canonical command name (for tests and diagnostics)."""
    return tuple(COMMANDS)


#: How many characters of a command's metadata block may go into a task text.
METADATA_MAX_CHARS = 1_200


def metadata(parsed: Parsed) -> dict[str, Any]:
    """The parsed decision as JSON-serialisable metadata for a stored message."""
    return {
        "commands": [c.name for c in parsed.commands],
        "mode": parsed.mode,
        "task_type": parsed.task_type,
        "tags": parsed.tag_list(),
        "restrictions": sorted(parsed.restrictions),
        "params": parsed.params,
        "warnings": parsed.warnings,
    }


def task_text(parsed: Parsed, request: str) -> str:
    """The task text for a turn that starts work: *request* plus its metadata.

    The planner and the coding agents read the specification as it is stored, so
    the tags and the command that produced the task travel with the request.
    The block is a *description* of the task, not an obligation: it names the
    focus areas and the restrictions, so an agent knows what "backend" or
    "#no-merge" means here. When there is nothing to add (no command, no tag)
    the request is returned unchanged, byte for byte.
    """
    if not parsed.commands and not parsed.tag_list() and not parsed.params:
        return request
    lines = []
    if parsed.primary is not None:
        lines.append(f"Команда: {parsed.primary.label()}")
    if parsed.task is not None and parsed.task is not parsed.primary:
        lines.append(f"Задача: {parsed.task.label()}")
    if parsed.task_type:
        lines.append(f"Тип задачи: {parsed.task_type}")
    tags = parsed.tag_list()
    if tags:
        lines.append("Фокус: " + " ".join("#" + t for t in tags if t not in RESTRICTION_TAGS))
    restrictions = sorted(parsed.restrictions)
    if restrictions:
        lines.append("Ограничения: " + ", ".join("#" + t for t in restrictions))
    if parsed.params:
        lines.append("Параметры: " + ", ".join(f"{k}={v}" for k, v in parsed.params.items()))
    block = "\n".join(lines)[:METADATA_MAX_CHARS]
    return f"{request}\n\n[NEXUS] {block}" if request else f"[NEXUS] {block}"
