"""Large task documents ("specifications"): verbatim, fingerprinted, indexed, staged.

A task used to be a string that had to fit ``nexus_goals.text`` (20 000
characters) and that only a planner ever read. This module is what lets a task
be a document of any size up to the server's cap and still reach the agent
whole:

* **The original is kept exactly.** :func:`check_text` rejects only what cannot
  be stored (a NUL, an unpaired surrogate, text over the cap); it never trims
  or normalises. :func:`fingerprint` is the SHA-256 of the UTF-8 bytes, and the
  database re-checks it on every write (migration ``0021``).
* **It is indexed without a model.** :func:`build_index` cuts the text into
  sections (headings), gives every list item, paragraph, table row and code
  block a stable id, and records explicit references between parts. The index
  is deterministic and total: sections partition the text exactly, and every
  non-blank line outside a heading belongs to exactly one addressable piece.
  A wrong guess about what is a heading costs navigation, never content.
* **It is cut into stages by size.** :func:`build_stages` produces contiguous
  parts that each fit a token budget, breaking at section boundaries when it
  can, then at pieces, lines and - for a line longer than the budget - at
  characters. The stages also partition the text exactly.

Token counts here are estimates (``providers.estimate_tokens``), chosen to err on
the high side. The limit on how much text the HTTP API accepts and the limit on
how much a model can read at once are different things and are kept apart: the
first is :func:`max_spec_chars`, the second is the stage budget, which comes
from the planner's context window (``nexus_spec_planner``).

:class:`SpecService` is the storage half; everything above it is pure.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import IdempotencyConflict, ProjectService
from .nexus_projects import _text as _short_text
from .providers import estimate_tokens

#: How long a task text may be, in characters, unless ``NEXUS_SPEC_MAX_CHARS``
#: says otherwise. The request body cap in ``web_server`` is sized to match.
DEFAULT_MAX_SPEC_CHARS = 1_000_000

#: A task this short travels inside the agent's first message; a longer one is
#: handed over as a file. Also the size above which a chat message is stored as
#: a preview that points at the specification. ``NEXUS_SPEC_INLINE_CHARS``.
DEFAULT_INLINE_CHARS = 20_000

#: Characters of a specification kept in a chat message or a goal as a label.
PREVIEW_CHARS = 2_000


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def max_spec_chars() -> int:
    return _positive_env("NEXUS_SPEC_MAX_CHARS", DEFAULT_MAX_SPEC_CHARS)


def inline_chars() -> int:
    return _positive_env("NEXUS_SPEC_INLINE_CHARS", DEFAULT_INLINE_CHARS)


class SpecError(ValueError):
    """The text cannot be accepted as it is. The message says exactly why."""


class SpecTooLarge(SpecError):
    """The text is longer than this server accepts."""


class SpecCorrupted(RuntimeError):
    """Stored text no longer matches its fingerprint. Never served as if it did."""


def check_text(value: Any) -> str:
    """Return *value* unchanged if it can be stored exactly, else raise.

    Nothing is repaired here on purpose: a text that would have to be altered
    to be stored is refused with the position of the problem, so what is kept
    is always what was sent.
    """
    if not isinstance(value, str):
        raise SpecError("text must be a string")
    if not value.strip():
        raise SpecError("text is required")
    limit = max_spec_chars()
    if len(value) > limit:
        raise SpecTooLarge(
            f"text is {len(value)} characters; this server accepts up to {limit} "
            "(NEXUS_SPEC_MAX_CHARS)")
    nul = value.find("\x00")
    if nul >= 0:
        raise SpecError(
            f"text contains a NUL character (U+0000) at position {nul}; it cannot be stored")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise SpecError(
            f"text contains an unpaired surrogate (U+{ord(value[e.start]):04X}) at "
            f"position {e.start}; it is not valid Unicode") from None
    return value


def fingerprint(text: str) -> tuple[str, int]:
    """``(sha256 hex, byte length)`` of the UTF-8 encoding of *text*."""
    data = text.encode("utf-8")
    return hashlib.sha256(data).hexdigest(), len(data)


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """The first *limit* characters of *text*, for a label. Never the task itself."""
    head = text.strip()
    if len(head) <= limit:
        return head
    return head[:limit].rstrip() + "…"


#: ``nexus_goals.text`` holds at most this many characters (migration 0009).
GOAL_TEXT_CHARS = 20_000


def goal_label(text: str) -> str:
    """What a goal row says about a task whose text is kept as a specification.

    A text that fits the goal column is the label itself, as it always was. A
    longer one is represented by its beginning; the text is in ``nexus_specs``.
    """
    head = text.strip()
    return head if len(head) <= GOAL_TEXT_CHARS else preview(text)


def requirement_key(ordinal: int, count: int) -> str:
    """The id of piece *ordinal* in a specification that has *count* pieces."""
    return f"R-{ordinal + 1:0{max(4, len(str(count)))}d}"


def section_key(ordinal: int, count: int) -> str:
    return f"S-{ordinal + 1:0{max(3, len(str(count)))}d}"


def compact_keys(ordinals: Iterable[int], count: int) -> list[str]:
    """Piece ids for *ordinals*, consecutive runs written ``R-0012..R-0040``."""
    out: list[str] = []
    run: list[int] = []
    for ordinal in [*sorted(set(ordinals)), None]:
        if run and ordinal is not None and ordinal == run[-1] + 1:
            run.append(ordinal)
            continue
        if run:
            first, last = requirement_key(run[0], count), requirement_key(run[-1], count)
            out.append(first if len(run) == 1 else f"{first}..{last}")
        run = [] if ordinal is None else [ordinal]
    return out


_KEY_RANGE = re.compile(
    r"^\[?R[-_ ]?(\d{1,7})\]?(?:\s*(?:\.{2,}|…|-|–|—|to)\s*\[?R?[-_ ]?(\d{1,7})\]?)?$",
    re.IGNORECASE)


def expand_keys(values: Any, count: int) -> tuple[set[int], int]:
    """Piece ordinals named by *values*, and how many entries named nothing.

    Accepts what a model writes: ``"R-0012"``, ``"R-12"``, ``"R-0012..R-0040"``,
    ``"R-0012-R-0040"``. An entry that is not an id of this specification is
    counted and skipped, never guessed at.
    """
    found: set[int] = set()
    ignored = 0
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return found, 0 if values is None else 1
    for value in values:
        match = _KEY_RANGE.match(value.strip()) if isinstance(value, str) else None
        if match is None:
            ignored += 1
            continue
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if not 1 <= first <= last <= count:
            ignored += 1
            continue
        found.update(range(first - 1, last))
    return found, ignored


# --------------------------------------------------------------------- index
@dataclass(frozen=True)
class Section:
    ordinal: int
    key: str
    level: int            # 0: text before the first heading
    number: str           # "3.2" when the heading carries one
    title: str
    parent: int | None
    start: int            # [start, end) in characters
    end: int
    start_line: int       # 1-based, inclusive
    end_line: int
    tokens: int


@dataclass(frozen=True)
class Requirement:
    ordinal: int
    key: str
    section: int
    kind: str             # item | paragraph | row | code
    normative: bool
    start: int
    end: int
    start_line: int
    end_line: int
    excerpt: str


@dataclass(frozen=True)
class Link:
    ordinal: int
    requirement: int
    target_section: int
    reference: str


@dataclass(frozen=True)
class Stage:
    ordinal: int
    start: int
    end: int
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    tokens: int


@dataclass
class SpecIndex:
    char_count: int
    byte_count: int
    line_count: int
    sha256: str
    tokens: int
    sections: list[Section] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    #: Character offset at which each line starts, plus ``char_count`` at the end.
    line_starts: list[int] = field(default_factory=list)
    #: ``line_tokens[i]`` is the estimate for lines ``[0, i)``.
    line_tokens: list[int] = field(default_factory=list)
    #: ``line_bytes[i]`` is the UTF-8 length of lines ``[0, i)``.
    line_bytes: list[int] = field(default_factory=list)


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_KEYWORD = re.compile(
    r"^(?:раздел|глава|часть|этап|шаг|приложение|section|chapter|part|stage|phase|step|appendix)"
    r"[ \t]+(\d{1,3}(?:\.\d{1,3}){0,5}|[IVXLC]{1,6}|[A-ZА-ЯЁ])(?![\w])[.:)]?", re.IGNORECASE)
_NUMBERED = re.compile(r"^(?:(\d{1,3}(?:\.\d{1,3}){1,5})[.)]?|(\d{1,3})[.)])[ \t]+(\S.*)$")
_LEADING_NUMBER = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})[.)]?[ \t]+(\S.*)$")
_BOLD = re.compile(r"^\*\*([^*].{0,158}?)\*\*:?[ \t]*$")
_BULLET = re.compile(
    r"^[ \t]*(?:[-*+•·▪◦‣–—]|\[[ xX]\]|\d{1,3}(?:\.\d{1,3}){0,5}[.)]|[A-Za-zА-Яа-яЁё][.)])[ \t]+\S")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
_NORMATIVE = re.compile(
    r"долж(?:ен|н)|обязан|обязательн|необходим|требуется|требуют|нужно|следует|нельзя|запрещ|"
    r"не допуска|никогда|"
    r"\b(?:must|shall|should|required?|needs? to|never|do not|don't|may not|cannot)\b",
    re.IGNORECASE)
_IMPERATIVE = re.compile(
    r"^[\s\-*+•–—\d.)\[\]xX]*(?:не[ \t]+)?[А-ЯЁа-яё]{2,}(?:ть|ться|ти|тись|чь|чься)\b")
#: Orders, the way a brief is usually written ("Проведи аудит", "Исправь", "Не
#: останавливайся"). A list of stems, not a rule about endings: "Если" and
#: "Цель" end like an imperative and are not one. A wrong match only widens
#: what the plan is checked against, so the list errs towards more.
_ORDER = re.compile(
    r"^[\s\-*+•–—\d.)\[\]xX]*(?:не[ \t]+)?(?:(?:"
    r"сдела|исправ|добав|реализу|напиш|провер|убеди|обеспеч|созда[йв]|удал[ия]|провед|установ|"
    r"найд|запус|покро|обнов|перепиш|внедр|настро|подключ|протестир|опиш|выполн|сохран|верн|"
    r"использу|примен|замен|перенес|состав|предостав|докаж|покаж|подготов|разработа|постро|"
    r"собер|убер|почин|изуч|проанализир|задокументир|зафиксир|отправ|вывед|учти|учит|соблюда|"
    r"останавлива|продолж|довед|закрой|открой|оформ|раздел[ия]|объедин|сравн|измер"
    r")[а-яё]*|(?:"
    r"fix|add|implement|create|remove|delete|write|run|make|ensure|verify|check|update|"
    r"replace|refactor|test|document|deploy|build|prove|keep|use"
    r"))(?![\w-])", re.IGNORECASE)
_REFERENCE = re.compile(
    r"(?:раздел[а-яё]{0,3}|пункт[а-яё]{0,3}|пп?\.|глав[а-яё]{0,2}|§|этап[а-яё]{0,2}|"
    r"шаг[а-яё]{0,2}|приложени[а-яё]{1,2}|section|sec\.|clause|chapter|step|stage|phase|"
    r"appendix)[ \t]*№?[ \t]*(\d{1,3}(?:\.\d{1,3}){0,5})", re.IGNORECASE)


def _is_caps_heading(line: str) -> bool:
    """A short line written entirely in capitals, the way formal documents title a part."""
    if not 8 <= len(line) <= 100 or line[-1] in ".;,":
        return False
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 8 and all(c.isupper() for c in letters)


def build_index(text: str) -> SpecIndex:
    """Index *text*: lines, sections, addressable pieces and references.

    A line ends at ``"\\n"`` and nowhere else, so line numbers are the ones
    ``sed -n`` and ``cat -n`` show an agent reading the file.
    """
    sha, byte_count = fingerprint(text)
    total = len(text)

    # ---- lines ---------------------------------------------------------
    starts = [0]
    position = text.find("\n")
    while position != -1:
        starts.append(position + 1)
        position = text.find("\n", position + 1)
    if starts[-1] == total and total:
        starts.pop()                      # the text ends with a newline
    line_count = len(starts)
    starts.append(total)
    lines = [text[starts[i]:starts[i + 1]].rstrip("\r\n") for i in range(line_count)]

    line_tokens = [0] * (line_count + 1)
    line_bytes = [0] * (line_count + 1)
    for i in range(line_count):
        raw = text[starts[i]:starts[i + 1]]
        line_tokens[i + 1] = line_tokens[i] + estimate_tokens(raw)
        line_bytes[i + 1] = line_bytes[i] + len(raw.encode("utf-8"))

    # ---- classify every line -------------------------------------------
    # blank | fence | text | bullet | table | rule, or ("num", number, depth, title)
    kinds: list[Any] = [None] * line_count
    fence: str | None = None
    for i, line in enumerate(lines):
        if fence is not None:
            kinds[i] = "fence"
            closing = _FENCE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            kinds[i] = "fence"
            continue
        if not line.strip():
            kinds[i] = "blank"
            continue
        numbered = _NUMBERED.match(line)
        if numbered:
            number = numbered.group(1) or numbered.group(2)
            kinds[i] = ("num", number, number.count(".") + 1, numbered.group(3))
        elif _TABLE_ROW.match(line):
            kinds[i] = "rule" if _TABLE_RULE.match(line) else "table"
        elif _BULLET.match(line):
            kinds[i] = "bullet"
        else:
            kinds[i] = "text"

    def neighbour(i: int, step: int) -> Any:
        j = i + step
        while 0 <= j < line_count and kinds[j] == "blank":
            j += step
        return kinds[j] if 0 <= j < line_count else None

    # ---- headings ------------------------------------------------------
    headings: dict[int, tuple[int, str, str]] = {}      # line -> (level, number, title)
    for i, line in enumerate(lines):
        kind = kinds[i]
        if kind in ("fence", "blank", "table", "rule"):
            continue
        atx = _ATX.match(line)
        if atx:
            title = atx.group(2).strip()
            number = ""
            leading = _LEADING_NUMBER.match(title)
            if leading:
                number, title = leading.group(1), leading.group(2)
            headings[i] = (len(atx.group(1)), number, title[:300])
            continue
        # Every other kind of heading has to stand apart from the text before
        # it, or a wrapped line that happens to begin "3. " would start a section.
        apart = i == 0 or kinds[i - 1] == "blank" or (i - 1) in headings
        if not apart or len(line) > 160:
            continue
        if isinstance(kind, tuple):
            _, number, depth, title = kind
            before, after = neighbour(i, -1), neighbour(i, 1)
            in_a_list = any(isinstance(n, tuple) and n[2] == depth for n in (before, after))
            if not in_a_list:
                headings[i] = (min(depth, 6), number, title.strip()[:300])
            continue
        if kind == "bullet":
            continue
        keyword = _KEYWORD.match(line.strip())
        if keyword:
            headings[i] = (1, keyword.group(1) if keyword.group(1)[0].isdigit() else "",
                           line.strip()[:300])
            continue
        bold = _BOLD.match(line.strip())
        if bold:
            headings[i] = (5, "", bold.group(1).strip()[:300])
            continue
        if _is_caps_heading(line.strip()) and neighbour(i, 1) is not None \
                and (i + 1 >= line_count or kinds[i + 1] == "blank"):
            headings[i] = (1, "", line.strip()[:300])

    # ---- sections: the text from one heading to the next ----------------
    heading_lines = sorted(headings)
    first_lines = list(heading_lines)
    if not first_lines or first_lines[0] != 0:
        # Text before the first heading. If it is only whitespace it joins the
        # first section instead of becoming an empty one of its own.
        if first_lines and not text[:starts[first_lines[0]]].strip():
            pass
        else:
            first_lines.insert(0, 0)
    width = max(3, len(str(len(first_lines))))
    sections: list[Section] = []
    stack: list[tuple[int, int]] = []                    # (level, ordinal)
    for ordinal, line_index in enumerate(first_lines):
        level, number, title = headings.get(line_index, (0, "", ""))
        begin = 0 if ordinal == 0 else starts[line_index]
        first_line = 0 if ordinal == 0 else line_index
        last_line = (first_lines[ordinal + 1] if ordinal + 1 < len(first_lines)
                     else line_count)
        end = starts[last_line]
        parent: int | None = None
        if level:
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            stack.append((level, ordinal))
        sections.append(Section(
            ordinal=ordinal, key=f"S-{ordinal + 1:0{width}d}", level=level, number=number,
            title=title, parent=parent, start=begin, end=end,
            start_line=first_line + 1, end_line=last_line,
            tokens=line_tokens[last_line] - line_tokens[first_line]))

    # ---- addressable pieces --------------------------------------------
    pieces: list[tuple[int, int, int, str]] = []        # (section, first line, last line + 1, kind)
    for section in sections:
        i, stop = section.start_line - 1, section.end_line
        while i < stop:
            kind = kinds[i]
            if kind == "blank" or i in headings:
                i += 1
                continue
            j = i + 1
            if kind == "fence":
                while j < stop and kinds[j] == "fence":
                    j += 1
                pieces.append((section.ordinal, i, j, "code"))
            elif kind in ("table", "rule"):
                # A rule line ("|---|") belongs to the row above it.
                while j < stop and kinds[j] == "rule":
                    j += 1
                pieces.append((section.ordinal, i, j, "row"))
            elif kind == "bullet" or isinstance(kind, tuple):
                while j < stop and kinds[j] == "text" and j not in headings:
                    j += 1
                pieces.append((section.ordinal, i, j, "item"))
            else:
                while j < stop and kinds[j] == "text" and j not in headings:
                    j += 1
                pieces.append((section.ordinal, i, j, "paragraph"))
            i = j
    width = max(4, len(str(len(pieces))))
    requirements: list[Requirement] = []
    for ordinal, (section_ordinal, first, last, kind) in enumerate(pieces):
        body = text[starts[first]:starts[last]]
        normative = kind in ("item", "row") or (
            kind == "paragraph" and bool(_NORMATIVE.search(body) or _IMPERATIVE.match(body)
                                         or _ORDER.match(body)))
        requirements.append(Requirement(
            ordinal=ordinal, key=f"R-{ordinal + 1:0{width}d}", section=section_ordinal,
            kind=kind, normative=normative, start=starts[first], end=starts[last],
            start_line=first + 1, end_line=last,
            excerpt=" ".join(body[:2000].split())[:240]))

    # ---- explicit references to numbered sections ------------------------
    by_number: dict[str, int] = {}
    for section in sections:
        if section.number:
            by_number.setdefault(section.number, section.ordinal)
    links: list[Link] = []
    if by_number:
        for requirement in requirements:
            if requirement.kind == "code":
                continue
            seen: set[int] = set()
            for match in _REFERENCE.finditer(text, requirement.start, requirement.end):
                target = by_number.get(match.group(1))
                if target is None or target == requirement.section or target in seen:
                    continue
                seen.add(target)
                links.append(Link(ordinal=len(links), requirement=requirement.ordinal,
                                  target_section=target, reference=match.group(0)[:120]))
                if len(links) >= 20_000:
                    break
            if len(links) >= 20_000:
                break

    return SpecIndex(char_count=total, byte_count=byte_count, line_count=line_count,
                     sha256=sha, tokens=line_tokens[line_count], sections=sections,
                     requirements=requirements, links=links, line_starts=starts,
                     line_tokens=line_tokens, line_bytes=line_bytes)


# -------------------------------------------------------------------- stages
def build_stages(text: str, index: SpecIndex, budget: int, *, start: int = 0,
                 end: int | None = None) -> list[Stage]:
    """Cut ``text[start:end]`` into contiguous stages of at most *budget* tokens.

    A stage ends at the last section boundary that fits; failing that at the
    last piece boundary; failing that at the last line; and a single line that
    is over the budget on its own is cut by characters (at a space when there
    is one near). The stages returned cover ``[start, end)`` with no gap and no
    overlap. *end* defaults to the end of the text.
    """
    if budget < 1:
        raise ValueError("budget must be positive")
    total, starts = index.char_count, index.line_starts
    end = total if end is None else end
    if not 0 <= start < end <= total:
        raise ValueError("start and end must delimit a non-empty part of the text")
    line_count = index.line_count
    section_lines = sorted({s.start_line - 1 for s in index.sections})
    piece_lines = sorted({r.start_line - 1 for r in index.requirements})
    stages: list[Stage] = []

    def emit(begin: int, end: int, tokens: int) -> None:
        first = bisect.bisect_right(starts, begin, 0, line_count) - 1
        last = bisect.bisect_left(starts, end, 0, line_count + 1) - 1
        first_byte = index.line_bytes[first] + len(text[starts[first]:begin].encode("utf-8"))
        last_byte = first_byte + len(text[begin:end].encode("utf-8"))
        stages.append(Stage(ordinal=len(stages), start=begin, end=end,
                            start_line=first + 1, end_line=max(first, last) + 1,
                            start_byte=first_byte, end_byte=last_byte, tokens=tokens))

    def cut_line(begin: int, end: int) -> None:
        """One line (or what is left of it) that does not fit a stage."""
        position = begin
        while position < end:
            rest = text[position:end]
            cost = estimate_tokens(rest)
            if cost <= budget:
                emit(position, end, cost)
                return
            size = max(1, int(len(rest) * budget / cost))
            while size > 1 and estimate_tokens(rest[:size]) > budget:
                size = max(1, int(size * 0.9))
            space = rest.rfind(" ", int(size * 0.8), size)
            if space > 0:
                size = space + 1
            emit(position, position + size, estimate_tokens(rest[:size]))
            position += size

    line = bisect.bisect_right(starts, start, 0, line_count) - 1
    if starts[line] != start:
        # *start* is inside a line (a previous stage ended there): finish it.
        cut_line(start, min(starts[line + 1], end))
        line += 1
    # Lines [line, whole) lie entirely inside the range.
    whole = bisect.bisect_right(starts, end, 0, line_count + 1) - 1
    while line < whole:
        base = index.line_tokens[line]
        # The furthest line boundary whose text still fits.
        fit = bisect.bisect_right(index.line_tokens, base + budget, line, whole + 1) - 1
        if fit <= line:
            cut_line(starts[line], starts[line + 1])
            line += 1
            continue
        if fit >= whole:
            stop = whole
        else:
            stop = fit
            for boundaries in (section_lines, piece_lines):
                at = bisect.bisect_right(boundaries, fit) - 1
                if at >= 0 and boundaries[at] > line:
                    stop = boundaries[at]
                    break
        emit(starts[line], starts[stop], index.line_tokens[stop] - base)
        line = stop
    if line == whole and starts[whole] < end:
        # The range ends inside a line (it is itself a cut of one).
        cut_line(starts[whole], end)
    return stages


def stage_rows(stages: list[Stage], sections: list[Section]) -> list[dict[str, Any]]:
    """*stages* as rows of ``nexus_spec_stages``, each titled by what it holds."""
    rows: list[dict[str, Any]] = []
    for stage in stages:
        names = [f"{section.number} {section.title}".strip() for section in sections
                 if section.start < stage.end and section.end > stage.start]
        names = [name for name in names if name]
        title = names[0] if names else ""
        if len(names) > 1:
            title = f"{title[:95]} … {names[-1][:95]}"
        rows.append({"title": title[:200], "start_char": stage.start, "end_char": stage.end,
                     "start_line": stage.start_line, "end_line": stage.end_line,
                     "start_byte": stage.start_byte, "end_byte": stage.end_byte,
                     "token_estimate": stage.tokens})
    return rows


def annotate(text: str, begin: int, end: int, requirements: list[dict[str, Any]]) -> str:
    """``text[begin:end]`` with each piece's id written before it, for a planner.

    *requirements* are rows that overlap the range, in order. The original text
    is not changed anywhere else: only this copy, shown to the planning model,
    carries the ids, so that its tasks can name what they cover.
    """
    out: list[str] = []
    position = begin
    for row in requirements:
        at = row["start_char"]
        if at < begin:
            out.append(f"[{row['key']}, continued] ")
            continue
        if at >= end:
            break
        out.append(text[position:at])
        out.append(f"[{row['key']}] ")
        position = at
    out.append(text[position:end])
    return "".join(out)


# ------------------------------------------------------------------- storage
_META = ("id, project_id, conversation_id, sha256, char_count, byte_count, line_count, "
         "token_estimate, section_count, requirement_count, link_count, source, state, "
         "stage_budget, plan_options, run_id, last_error, created_by, created_at, updated_at")


_GOAL = "id, project_id, text, state, created_by, created_at, spec_id"


class SpecService:
    """Specifications over the shared PostgreSQL store.

    Membership is checked in the same transaction that reads or writes a row.
    Methods whose name ends in ``_internal`` skip it: they are for the planning
    loop and the executor, which are not project members and never hand what
    they read to a caller.
    """

    def __init__(self, store: PostgresStore):
        self.store = store

    # ------------------------------------------------------------- writing
    def create(self, principal: str, project_id: str, text: Any, *, source: str = "chat",
               conversation_id: str | None = None) -> tuple[dict, bool]:
        """Store *text* exactly, with its index. Returns ``(spec, created)``.

        Within one conversation the same text is the same specification: a
        repeated send finds the row the first one made (``created`` is False),
        which is what keeps a retry from planning or running anything twice.
        """
        text = check_text(text)
        index = build_index(text)
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            return self._insert(cur, principal, project_id, text, index, source, conversation_id)

    def _insert(self, cur: Any, principal: str, project_id: str, text: str, index: SpecIndex,
                source: str, conversation_id: str | None) -> tuple[dict, bool]:
        if source not in ("chat", "api"):
            raise ValueError("source must be chat or api")
        if conversation_id is not None:
            cur.execute("SELECT 1 FROM nexus_conversations WHERE id=%s AND project_id=%s",
                        (conversation_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("conversation not found")
        cur.execute(
            "INSERT INTO nexus_specs(project_id, conversation_id, content, sha256, "
            "char_count, byte_count, line_count, token_estimate, section_count, "
            "requirement_count, link_count, source, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (conversation_id, sha256) WHERE conversation_id IS NOT NULL "
            f"DO NOTHING RETURNING {_META}",
            (project_id, conversation_id, text, index.sha256, index.char_count,
             index.byte_count, index.line_count, index.tokens, len(index.sections),
             len(index.requirements), len(index.links), source, principal))
        row = cur.fetchone()
        if row is None:
            cur.execute(f"SELECT {_META} FROM nexus_specs "
                        "WHERE conversation_id=%s AND sha256=%s AND project_id=%s",
                        (conversation_id, index.sha256, project_id))
            existing = cur.fetchone()
            if existing is None:
                raise NotFound("specification not found")
            return self._view(existing), False
        self._store_index(cur, row["id"], index)
        ProjectService._event(cur, project_id, principal, "spec.created", row["id"])
        return self._view(row), True

    def create_with_goal(self, principal: str, project_id: str, text: Any, *,
                         source: str = "chat", conversation_id: str | None = None,
                         key: str | None = None) -> tuple[dict, dict, bool]:
        """Store *text* as a specification and give it a goal, in one transaction.

        Returns ``(goal, spec, created)``. The goal row carries a label (see
        :func:`goal_label`) and points at the specification. Asking twice for
        the same thing returns the first answer: in a chat the same text in the
        same conversation is the same specification and the same goal; over the
        API the ``Idempotency-Key`` is what identifies a repeated request.
        """
        text = check_text(text)
        if key is not None:
            key = _short_text(key, "Idempotency-Key", maximum=200)
        index = build_index(text)
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            if key is not None:
                cur.execute(f"SELECT {_GOAL} FROM nexus_goals "
                            "WHERE project_id=%s AND idempotency_key=%s", (project_id, key))
                goal = cur.fetchone()
                if goal is not None:
                    spec = self._meta(cur, goal["spec_id"]) if goal["spec_id"] else None
                    if (spec is None or spec["sha256"] != index.sha256
                            or goal["created_by"] != principal):
                        raise IdempotencyConflict(
                            "idempotency key already used for a different request")
                    return self._goal_view(goal), spec, False
            spec, _new = self._insert(cur, principal, project_id, text, index, source,
                                      conversation_id)
            cur.execute(f"SELECT {_GOAL} FROM nexus_goals WHERE spec_id=%s", (spec["id"],))
            goal = cur.fetchone()
            if goal is not None:
                return self._goal_view(goal), spec, False
            cur.execute(
                "INSERT INTO nexus_goals(project_id, text, created_by, idempotency_key, spec_id) "
                f"VALUES (%s,%s,%s,%s,%s) RETURNING {_GOAL}",
                (project_id, goal_label(text), principal, key, spec["id"]))
            goal = cur.fetchone()
            ProjectService._event(cur, project_id, principal, "goal.created", goal["id"])
            return self._goal_view(goal), spec, True

    def ensure_for_goal(self, principal: str, project_id: str, goal_id: str) -> tuple[dict, dict]:
        """``(goal, spec)`` for a goal, giving it a specification if it has none.

        A goal created before specifications existed, or a short one created
        over the API, has only its text. That text becomes its specification
        the first time the goal is run, so every run hands the agent the task
        the same way.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(f"SELECT {_GOAL} FROM nexus_goals WHERE id=%s AND project_id=%s "
                        "FOR UPDATE", (goal_id, project_id))
            goal = cur.fetchone()
            if goal is None:
                raise NotFound("goal not found")
            if goal["spec_id"] is not None:
                spec = self._meta(cur, goal["spec_id"])
                if spec is not None:
                    return self._goal_view(goal), spec
            text = check_text(goal["text"])
            spec, _new = self._insert(cur, principal, project_id, text, build_index(text),
                                      "api", None)
            cur.execute(f"UPDATE nexus_goals SET spec_id=%s WHERE id=%s RETURNING {_GOAL}",
                        (spec["id"], goal_id))
            return self._goal_view(cur.fetchone()), spec

    def _meta(self, cur: Any, spec_id: Any) -> dict | None:
        cur.execute(f"SELECT {_META} FROM nexus_specs WHERE id=%s", (spec_id,))
        row = cur.fetchone()
        return self._view(row) if row else None

    @staticmethod
    def _goal_view(row: Any) -> dict:
        goal = dict(row)
        if goal.get("spec_id") is not None:
            goal["spec_id"] = str(goal["spec_id"])
        return goal

    @staticmethod
    def _store_index(cur: Any, spec_id: Any, index: SpecIndex) -> None:
        s = index.sections
        if s:
            cur.execute(
                "INSERT INTO nexus_spec_sections(spec_id, ordinal, key, level, number, title, "
                "parent, start_char, end_char, start_line, end_line, token_estimate) "
                "SELECT %s, * FROM unnest(%s::int[], %s::text[], %s::int[], %s::text[], "
                "%s::text[], %s::int[], %s::int[], %s::int[], %s::int[], %s::int[], %s::int[])",
                (spec_id, [x.ordinal for x in s], [x.key for x in s], [x.level for x in s],
                 [x.number for x in s], [x.title for x in s], [x.parent for x in s],
                 [x.start for x in s], [x.end for x in s], [x.start_line for x in s],
                 [x.end_line for x in s], [x.tokens for x in s]))
        r = index.requirements
        if r:
            cur.execute(
                "INSERT INTO nexus_spec_requirements(spec_id, ordinal, key, section, kind, "
                "normative, start_char, end_char, start_line, end_line, excerpt) "
                "SELECT %s, * FROM unnest(%s::int[], %s::text[], %s::int[], %s::text[], "
                "%s::bool[], %s::int[], %s::int[], %s::int[], %s::int[], %s::text[])",
                (spec_id, [x.ordinal for x in r], [x.key for x in r], [x.section for x in r],
                 [x.kind for x in r], [x.normative for x in r], [x.start for x in r],
                 [x.end for x in r], [x.start_line for x in r], [x.end_line for x in r],
                 [x.excerpt for x in r]))
        links = index.links
        if links:
            cur.execute(
                "INSERT INTO nexus_spec_links(spec_id, ordinal, requirement, target_section, "
                "reference) SELECT %s, * FROM unnest(%s::int[], %s::int[], %s::int[], %s::text[])",
                (spec_id, [x.ordinal for x in links], [x.requirement for x in links],
                 [x.target_section for x in links], [x.reference for x in links]))

    # ------------------------------------------------------------- reading
    @staticmethod
    def _view(row: Any) -> dict:
        out = dict(row)
        for key in ("id", "project_id", "conversation_id", "run_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

    @staticmethod
    def summary(spec: dict) -> dict:
        """The few facts about a specification that travel in message metadata."""
        return {"id": str(spec["id"]), "chars": spec["char_count"], "bytes": spec["byte_count"],
                "lines": spec["line_count"], "sha256": spec["sha256"],
                "sections": spec["section_count"], "requirements": spec["requirement_count"]}

    def get(self, principal: str, project_id: str, spec_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(f"SELECT {_META} FROM nexus_specs WHERE id=%s AND project_id=%s",
                        (spec_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("specification not found")
            spec = self._view(row)
            spec["stages"] = self._progress(cur, spec_id)
            return spec

    @staticmethod
    def _progress(cur: Any, spec_id: str) -> dict:
        cur.execute("SELECT state, count(*) AS n FROM nexus_spec_stages WHERE spec_id=%s "
                    "GROUP BY state", (spec_id,))
        counts = {r["state"]: r["n"] for r in cur.fetchall()}
        return {"total": sum(counts.values()), "planned": counts.get("planned", 0),
                "planning": counts.get("planning", 0), "pending": counts.get("pending", 0),
                "failed": counts.get("failed", 0)}

    def content(self, principal: str, project_id: str, spec_id: str) -> tuple[dict, str]:
        """The original text, checked against its fingerprint before it is returned."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(f"SELECT {_META}, content FROM nexus_specs WHERE id=%s AND project_id=%s",
                        (spec_id, project_id))
            row = cur.fetchone()
        if row is None:
            raise NotFound("specification not found")
        return self._checked(row)

    def content_internal(self, spec_id: str) -> tuple[dict, str]:
        with self.store._tx() as cur:
            cur.execute(f"SELECT {_META}, content FROM nexus_specs WHERE id=%s", (spec_id,))
            row = cur.fetchone()
        if row is None:
            raise NotFound("specification not found")
        return self._checked(row)

    def _checked(self, row: Any) -> tuple[dict, str]:
        row = dict(row)
        text = row.pop("content")
        sha, size = fingerprint(text)
        if sha != row["sha256"] or size != row["byte_count"] or len(text) != row["char_count"]:
            raise SpecCorrupted(
                f"specification {row['id']} no longer matches its fingerprint; refusing to use it")
        return self._view(row), text

    def sections(self, principal: str, project_id: str, spec_id: str) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            self._exists(cur, project_id, spec_id)
            return self._sections(cur, spec_id)

    def sections_internal(self, spec_id: str) -> list[dict]:
        with self.store._tx() as cur:
            return self._sections(cur, spec_id)

    @staticmethod
    def _sections(cur: Any, spec_id: str) -> list[dict]:
        cur.execute("SELECT ordinal, key, level, number, title, parent, start_char, end_char, "
                    "start_line, end_line, token_estimate FROM nexus_spec_sections "
                    "WHERE spec_id=%s ORDER BY ordinal", (spec_id,))
        return [dict(r) for r in cur.fetchall()]

    def requirements(self, principal: str, project_id: str, spec_id: str, *,
                     offset: int = 0, limit: int = 500,
                     section: int | None = None) -> list[dict]:
        if offset < 0 or not 0 < limit <= 2000:
            raise ValueError("offset must be >= 0 and limit between 1 and 2000")
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            self._exists(cur, project_id, spec_id)
            cur.execute(
                "SELECT ordinal, key, section, kind, normative, start_char, end_char, "
                "start_line, end_line, excerpt FROM nexus_spec_requirements "
                "WHERE spec_id=%s AND (%s::int IS NULL OR section=%s) "
                "ORDER BY ordinal OFFSET %s LIMIT %s", (spec_id, section, section, offset, limit))
            return [dict(r) for r in cur.fetchall()]

    def requirements_internal(self, spec_id: str, begin: int = 0,
                              end: int | None = None) -> list[dict]:
        """Pieces that overlap ``[begin, end)``, in document order."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT ordinal, key, section, kind, normative, start_char, end_char, "
                "start_line, end_line, excerpt FROM nexus_spec_requirements "
                "WHERE spec_id=%s AND end_char > %s AND (%s::int IS NULL OR start_char < %s) "
                "ORDER BY ordinal", (spec_id, begin, end, end))
            return [dict(r) for r in cur.fetchall()]

    def pieces_internal(self, spec_id: str, ordinals: list[int]) -> list[dict]:
        """The pieces with these ordinals, in document order."""
        if not ordinals:
            return []
        with self.store._tx() as cur:
            cur.execute(
                "SELECT ordinal, key, section, kind, normative, start_char, end_char, "
                "start_line, end_line, excerpt FROM nexus_spec_requirements "
                "WHERE spec_id=%s AND ordinal = ANY(%s) ORDER BY ordinal",
                (spec_id, list(ordinals)))
            return [dict(r) for r in cur.fetchall()]

    def links_internal(self, spec_id: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute("SELECT ordinal, requirement, target_section, reference "
                        "FROM nexus_spec_links WHERE spec_id=%s ORDER BY ordinal", (spec_id,))
            return [dict(r) for r in cur.fetchall()]

    def stages(self, principal: str, project_id: str, spec_id: str) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            self._exists(cur, project_id, spec_id)
            return self._stages(cur, spec_id)

    def stages_internal(self, spec_id: str) -> list[dict]:
        with self.store._tx() as cur:
            return self._stages(cur, spec_id)

    @staticmethod
    def _stages(cur: Any, spec_id: str) -> list[dict]:
        # In document order. An ordinal only names a row: a stage that proved
        # too large for the model is replaced by smaller ones with new ordinals.
        cur.execute("SELECT ordinal, title, start_char, end_char, start_line, end_line, "
                    "start_byte, end_byte, token_estimate, state, attempts, provider, plan, "
                    "last_error, updated_at FROM nexus_spec_stages WHERE spec_id=%s "
                    "ORDER BY start_char", (spec_id,))
        return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _exists(cur: Any, project_id: str, spec_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_specs WHERE id=%s AND project_id=%s",
                    (spec_id, project_id))
        if cur.fetchone() is None:
            raise NotFound("specification not found")

    def for_goal_internal(self, goal_id: str) -> str | None:
        """The specification a goal's text is kept in, if it has one."""
        with self.store._tx() as cur:
            cur.execute("SELECT spec_id FROM nexus_goals WHERE id=%s", (goal_id,))
            row = cur.fetchone()
            return str(row["spec_id"]) if row and row["spec_id"] else None

    # ------------------------------------------------------- planning state
    # A specification is planned stage by stage. Each stage is a row that is
    # claimed with a lease, so the request that started planning, the web
    # process's planning loop and a second process can all work on the same
    # specification without planning a stage twice, and a process that dies
    # leaves nothing but a lease that runs out.

    def decide_start(self, spec_id: str, goal_id: str, *, stages: list[dict], budget: int,
                     options: dict, owner: str, lease_seconds: int = 90) -> dict:
        """What a request to run this specification amounts to, decided once.

        Under a lock on the specification row, so two identical requests arriving
        together get two different answers and only one of them plans:

        * ``existing`` - the goal already has a live run (``run_id``);
        * ``planning`` - planning is already under way;
        * ``ready``    - every stage was planned before (an earlier run of the
          same text has ended); the plan is reused. The state is left as it
          is: the caller creates the run at once (:meth:`reuse_in`), and the
          planning loop, which finishes whatever is in ``planning``, must not
          get there first;
        * ``begin``    - planning starts now: the stages are queued. When there
          is exactly one, it is handed to *owner* already claimed (``claimed``
          is the stage row), in this same transaction, so the request that
          asked plans it itself and nothing else can take it first.
        """
        from .nexus_lab_store import ACTIVE_RUN_STATES

        with self.store._tx() as cur:
            cur.execute("SELECT state FROM nexus_specs WHERE id=%s FOR UPDATE", (spec_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound("specification not found")
            cur.execute("SELECT id FROM nexus_runs WHERE goal_id=%s AND state = ANY(%s)",
                        (goal_id, list(ACTIVE_RUN_STATES)))
            live = cur.fetchone()
            if live is not None:
                return {"decision": "existing", "run_id": str(live["id"])}
            if row["state"] == "planning":
                return {"decision": "planning", **self._progress(cur, spec_id)}
            cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE state='planned') AS done "
                        "FROM nexus_spec_stages WHERE spec_id=%s", (spec_id,))
            have = cur.fetchone()
            decision = "begin"
            if have["n"] and have["n"] == have["done"]:
                cur.execute("UPDATE nexus_specs SET plan_options=%s, updated_at=now() "
                            "WHERE id=%s", (_json(options), spec_id))
                return {"decision": "ready", "claimed": None, **self._progress(cur, spec_id)}
            elif have["n"]:
                # A failed attempt left stages behind. What was planned is kept;
                # the rest is tried again from scratch.
                cur.execute("UPDATE nexus_spec_stages SET state='pending', attempts=0, "
                            "lease_owner=NULL, lease_expires=NULL, last_error=NULL, "
                            "updated_at=now() WHERE spec_id=%s AND state <> 'planned'", (spec_id,))
            else:
                self._insert_stages(cur, spec_id, stages, first_ordinal=0)
            cur.execute("UPDATE nexus_specs SET state='planning', stage_budget=%s, "
                        "plan_options=%s, last_error=NULL, updated_at=now() WHERE id=%s",
                        (budget, _json(options), spec_id))
            claimed = None
            progress = self._progress(cur, spec_id)
            if decision == "begin" and progress["total"] == 1:
                cur.execute(
                    "UPDATE nexus_spec_stages SET state='planning', attempts=attempts+1, "
                    "lease_owner=%s, lease_expires=now() + %s * interval '1 second', "
                    "updated_at=now() WHERE spec_id=%s AND state='pending' RETURNING *",
                    (owner, lease_seconds, spec_id))
                row = cur.fetchone()
                claimed = dict(row) if row else None
                progress = self._progress(cur, spec_id)
            return {"decision": decision, "claimed": claimed, **progress}

    @staticmethod
    def _insert_stages(cur: Any, spec_id: str, stages: list[dict], *, first_ordinal: int) -> None:
        if not stages:
            return
        cur.execute(
            "INSERT INTO nexus_spec_stages(spec_id, ordinal, title, start_char, end_char, "
            "start_line, end_line, start_byte, end_byte, token_estimate) "
            "SELECT %s, * FROM unnest(%s::int[], %s::text[], %s::int[], %s::int[], %s::int[], "
            "%s::int[], %s::int[], %s::int[], %s::int[])",
            (spec_id, [first_ordinal + i for i in range(len(stages))],
             [s.get("title", "")[:200] for s in stages], [s["start_char"] for s in stages],
             [s["end_char"] for s in stages], [s["start_line"] for s in stages],
             [s["end_line"] for s in stages], [s["start_byte"] for s in stages],
             [s["end_byte"] for s in stages], [s["token_estimate"] for s in stages]))

    def claim_stage(self, owner: str, *, lease_seconds: int = 90,
                    spec_id: str | None = None) -> dict | None:
        """Take the next stage that needs planning, or ``None``.

        A stage is free when it is pending (and any retry delay has passed) or
        when whoever was planning it let the lease run out.

        Within one specification stages are planned strictly in document order:
        only the first stage without a plan can be taken. Each stage is planned
        knowing the tasks of the stages before it, and the stages that have a
        plan always form the beginning of the document. Between specifications
        the one served longest ago goes first.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT st.spec_id, st.ordinal FROM nexus_spec_stages st "
                "JOIN nexus_specs sp ON sp.id = st.spec_id "
                "WHERE sp.state='planning' AND (%s::uuid IS NULL OR sp.id=%s) "
                "AND st.state IN ('pending', 'planning') "
                "AND (st.lease_expires IS NULL OR st.lease_expires < now()) "
                "AND NOT EXISTS (SELECT 1 FROM nexus_spec_stages e WHERE e.spec_id = st.spec_id "
                "                AND e.start_char < st.start_char AND e.state <> 'planned') "
                "ORDER BY sp.updated_at, sp.id LIMIT 1 FOR UPDATE OF st SKIP LOCKED",
                (spec_id, spec_id))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE nexus_spec_stages SET state='planning', attempts=attempts+1, "
                "lease_owner=%s, lease_expires=now() + %s * interval '1 second', updated_at=now() "
                "WHERE spec_id=%s AND ordinal=%s RETURNING *",
                (owner, lease_seconds, row["spec_id"], row["ordinal"]))
            stage = dict(cur.fetchone())
            cur.execute("UPDATE nexus_specs SET updated_at=now() WHERE id=%s", (row["spec_id"],))
            return stage

    def renew_stage(self, spec_id: str, ordinal: int, owner: str, *,
                    lease_seconds: int = 90) -> bool:
        """Extend the lease on a stage that is still being planned by *owner*.

        Leases are short so that a process that dies gives its stage back
        within a minute or two; whoever is planning keeps the lease alive while
        the model is thinking. False when the stage is no longer *owner*'s.
        """
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_spec_stages SET lease_expires=now() + %s * interval '1 second' "
                "WHERE spec_id=%s AND ordinal=%s AND state='planning' AND lease_owner=%s",
                (lease_seconds, spec_id, ordinal, owner))
            return cur.rowcount == 1

    def stage_internal(self, spec_id: str, ordinal: int) -> dict | None:
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_spec_stages WHERE spec_id=%s AND ordinal=%s",
                        (spec_id, ordinal))
            row = cur.fetchone()
            return dict(row) if row else None

    def stage_planned(self, spec_id: str, ordinal: int, owner: str, *, plan: dict,
                      provider: str) -> bool:
        """Record a stage's plan. False when the lease was lost in the meantime."""
        with self.store._tx() as cur:
            return self.stage_planned_in(cur, spec_id, ordinal, owner, plan=plan,
                                         provider=provider)

    @staticmethod
    def stage_planned_in(cur: Any, spec_id: str, ordinal: int, owner: str, *, plan: dict,
                         provider: str) -> bool:
        cur.execute(
            "UPDATE nexus_spec_stages SET state='planned', plan=%s, provider=%s, "
            "lease_owner=NULL, lease_expires=NULL, last_error=NULL, updated_at=now() "
            "WHERE spec_id=%s AND ordinal=%s AND state='planning' AND lease_owner=%s",
            (_json(plan), provider, spec_id, ordinal, owner))
        return cur.rowcount == 1

    def progress_internal(self, spec_id: str) -> dict:
        with self.store._tx() as cur:
            return self._progress(cur, spec_id)

    def stage_retry(self, spec_id: str, ordinal: int, owner: str, *, error: str,
                    delay_seconds: int) -> bool:
        """Give a stage back to be tried again, not before *delay_seconds*."""
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_spec_stages SET state='pending', lease_owner=NULL, "
                "lease_expires=now() + %s * interval '1 second', last_error=%s, updated_at=now() "
                "WHERE spec_id=%s AND ordinal=%s AND state='planning' AND lease_owner=%s",
                (delay_seconds, error[:1000], spec_id, ordinal, owner))
            return cur.rowcount == 1

    def stage_failed(self, spec_id: str, ordinal: int, owner: str, *, error: str) -> bool:
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_spec_stages SET state='failed', lease_owner=NULL, "
                "lease_expires=NULL, last_error=%s, updated_at=now() "
                "WHERE spec_id=%s AND ordinal=%s AND state='planning' AND lease_owner=%s",
                (error[:1000], spec_id, ordinal, owner))
            return cur.rowcount == 1

    def split_stage(self, spec_id: str, ordinal: int, owner: str, *, pieces: list[dict],
                    budget: int) -> bool:
        """Replace a stage that proved too large for the model by smaller ones.

        The pieces cover exactly the range the stage covered. Stages are ordered
        by where they start, so the new ordinals only have to be unused.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT start_char, end_char FROM nexus_spec_stages WHERE spec_id=%s "
                        "AND ordinal=%s AND state='planning' AND lease_owner=%s FOR UPDATE",
                        (spec_id, ordinal, owner))
            row = cur.fetchone()
            if row is None:
                return False
            if (not pieces or pieces[0]["start_char"] != row["start_char"]
                    or pieces[-1]["end_char"] != row["end_char"]
                    or any(a["end_char"] != b["start_char"] for a, b in zip(pieces, pieces[1:]))):
                raise ValueError("the pieces do not cover the stage exactly")
            cur.execute("DELETE FROM nexus_spec_stages WHERE spec_id=%s AND ordinal=%s",
                        (spec_id, ordinal))
            cur.execute("SELECT COALESCE(max(ordinal), -1) + 1 AS next FROM nexus_spec_stages "
                        "WHERE spec_id=%s", (spec_id,))
            self._insert_stages(cur, spec_id, pieces, first_ordinal=cur.fetchone()["next"])
            cur.execute("UPDATE nexus_specs SET stage_budget=LEAST(COALESCE(stage_budget, %s), %s), "
                        "updated_at=now() WHERE id=%s", (budget, budget, spec_id))
            return True

    def ready_to_finish(self) -> list[str]:
        """Specifications being planned whose every stage now has a plan."""
        with self.store._tx() as cur:
            cur.execute(
                "SELECT sp.id FROM nexus_specs sp WHERE sp.state='planning' "
                "AND EXISTS (SELECT 1 FROM nexus_spec_stages st WHERE st.spec_id=sp.id) "
                "AND NOT EXISTS (SELECT 1 FROM nexus_spec_stages st WHERE st.spec_id=sp.id "
                "                AND st.state <> 'planned') ORDER BY sp.created_at")
            return [str(r["id"]) for r in cur.fetchall()]

    def meta_internal(self, spec_id: str) -> dict | None:
        with self.store._tx() as cur:
            cur.execute(f"SELECT {_META} FROM nexus_specs WHERE id=%s", (spec_id,))
            row = cur.fetchone()
            if row is None:
                return None
            spec = self._view(row)
            spec["stages"] = self._progress(cur, spec_id)
            cur.execute("SELECT id FROM nexus_goals WHERE spec_id=%s", (spec_id,))
            goal = cur.fetchone()
            spec["goal_id"] = str(goal["id"]) if goal else None
            return spec

    def finish(self, spec_id: str, run_id: str) -> bool:
        """Planning is over and the run exists. True for exactly one caller."""
        with self.store._tx() as cur:
            return self.finish_in(cur, spec_id, run_id)

    @staticmethod
    def finish_in(cur: Any, spec_id: str, run_id: Any) -> bool:
        """:meth:`finish` inside the caller's transaction - the one that creates
        the run, so the run and "this specification has its run" are one fact."""
        cur.execute("UPDATE nexus_specs SET state='planned', run_id=%s, last_error=NULL, "
                    "updated_at=now() WHERE id=%s AND state='planning'", (run_id, spec_id))
        return cur.rowcount == 1

    @staticmethod
    def reuse_in(cur: Any, spec_id: str, run_id: Any) -> bool:
        """A specification planned earlier gets a new run from the same plan."""
        cur.execute("UPDATE nexus_specs SET state='planned', run_id=%s, last_error=NULL, "
                    "updated_at=now() WHERE id=%s AND state IN ('planned', 'failed')",
                    (run_id, spec_id))
        return cur.rowcount == 1

    def fail(self, spec_id: str, error: str, *, discard_plans: bool = False,
             while_in: tuple[str, ...] = ("planning",)) -> bool:
        """Planning stopped. True for exactly one caller.

        Stage plans that exist are kept, so sending the text again continues
        from them. *discard_plans* is for a failure of the plans themselves
        (together they do not make a run): the next attempt plans afresh.
        """
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_specs SET state='failed', last_error=%s, updated_at=now() "
                        "WHERE id=%s AND state = ANY(%s)",
                        (error[:2000], spec_id, list(while_in)))
            failed = cur.rowcount == 1
            if failed and discard_plans:
                cur.execute("UPDATE nexus_spec_stages SET state='failed', plan=NULL, "
                            "lease_owner=NULL, lease_expires=NULL, updated_at=now() "
                            "WHERE spec_id=%s", (spec_id,))
            return failed

    def goal_for(self, principal: str, project_id: str, spec_id: str) -> tuple[dict, dict, bool]:
        """``(goal, spec, created)``: the goal of a stored specification.

        A chat message is stored as a specification as soon as it arrives; the
        goal is created only once the run may start. One specification has one
        goal, so asking again returns the same row.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(f"SELECT {_META}, content FROM nexus_specs WHERE id=%s AND project_id=%s "
                        "FOR UPDATE", (spec_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("specification not found")
            spec, text = self._checked(row)
            cur.execute(f"SELECT {_GOAL} FROM nexus_goals WHERE spec_id=%s", (spec_id,))
            goal = cur.fetchone()
            if goal is not None:
                return self._goal_view(goal), spec, False
            cur.execute(
                "INSERT INTO nexus_goals(project_id, text, created_by, spec_id) "
                f"VALUES (%s,%s,%s,%s) RETURNING {_GOAL}",
                (project_id, goal_label(text), principal, spec_id))
            goal = cur.fetchone()
            ProjectService._event(cur, project_id, principal, "goal.created", goal["id"])
            return self._goal_view(goal), spec, True

    def coverage_internal(self, spec_id: str) -> dict:
        """How the pieces of a specification are accounted for by its plan so far."""
        totals = {"pieces": 0, "in_tasks": 0, "declared_context": 0, "implicit_context": 0,
                  "in_verification_tasks": 0, "tasks": 0}
        for stage in self.stages_internal(spec_id):
            plan = stage.get("plan") if isinstance(stage.get("plan"), dict) else None
            if stage["state"] != "planned" or plan is None:
                continue
            for key in ("pieces", "in_tasks", "declared_context", "implicit_context",
                        "in_verification_tasks"):
                totals[key] += int((plan.get("coverage") or {}).get(key, 0))
            totals["tasks"] += len(plan.get("tasks") or [])
        return totals


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
