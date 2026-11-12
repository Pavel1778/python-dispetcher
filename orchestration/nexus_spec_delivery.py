"""Handing a task's original text to the agent: whole, and checked to be whole.

A run task is a planner's summary: a title, a description of at most 4 000
characters and a few acceptance criteria. Until now that summary was all an
agent ever saw; the text the user wrote stopped at the planner. This module
builds what is added to the agent's first message so the original reaches it:

* **Short text travels in the message.** Up to ``NEXUS_SPEC_INLINE_CHARS``
  characters the original is embedded between two marker lines, unchanged.
* **Long text travels as a file.** Above that the original is placed in the
  agent's sandbox as a UTF-8 file, with a second file that indexes it (every
  section and every addressable piece with its line range), and the message
  names the file, its size and SHA-256, the lines this task covers and how to
  read them. The transport reads the file back and compares it before it
  creates the conversation (``cloud_direct``); if that fails, no conversation
  is created.
* **Where there can be no file, long text travels part by part.** The ``app``
  transport starts a conversation and nothing else; it cannot put a file in
  the sandbox. A text up to ``NEXUS_SPEC_MESSAGE_CHARS`` characters is then
  still embedded whole. Of a longer one each task's message carries the part
  that task was planned from - the same bytes, between marker lines that name
  the part's own SHA-256 - with the outline of the whole, and it says in so
  many words that the rest is not in the conversation. Every part is carried
  by the tasks planned from it, so between them the tasks are given all of the
  text; no single agent is. That is the weaker hand-over, and it is recorded
  as such (``mode: part``).

How much text fits in a *message* and how much an agent's model can *read at
once* are different limits. The first decides inline or file. The second is why
a long text is never pasted into the message whole even where the API would
take it: the agent reads the file in ranges, starting with its own part.

A task of a run planned before specifications existed refers to none. Its
agent is given the request its run's goal holds (:func:`prepare_request`).

Nothing here talks to a sandbox. :func:`prepare` returns a :class:`Delivery`;
``nexus_lab.OpenHandsExecutor`` sends it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .cloud_client import FILES_DIR_TOKEN, DeliveryError, SandboxFile
from .cloud_store import NotFound
from .nexus_specs import (
    SpecCorrupted,
    SpecService,
    expand_keys,
    inline_chars,
    message_chars,
    requirement_key,
    section_key,
)

#: Sections listed in the message itself. The index file has all of them.
OUTLINE_LINES = 60
#: Pieces of a task named in the message with their lines and first words.
PIECE_LINES = 40


@dataclass
class Delivery:
    """What one task's agent is given of the original text."""

    mode: str                                  # "inline" | "file" | "part"
    #: Text for the agent's first message.
    block: str
    #: Files to place in the sandbox first (empty for ``inline``).
    files: list[SandboxFile] = field(default_factory=list)
    #: What is recorded on the task as evidence of the hand-over.
    proof: dict[str, Any] = field(default_factory=dict)


def markers(sha256: str, kind: str = "TEXT") -> tuple[str, str]:
    """The two lines an inline text sits between.

    They carry the start of the text's own SHA-256, so the text cannot contain
    them by accident, and a reader can cut the text out exactly: it is
    everything between the newline after the first marker and the newline
    before the second. *kind* is ``TEXT`` for a whole text and ``PART`` for one
    part of it; the SHA-256 is then the part's.
    """
    tag = sha256[:12]
    return f"<<<NEXUS-TASK-{kind} {tag}", f"NEXUS-TASK-{kind} {tag}>>>"


def extract_inline(message: str, sha256: str, kind: str = "TEXT") -> str | None:
    """The original text out of a first message built by :func:`prepare`."""
    opening, closing = markers(sha256, kind)
    start = message.find(opening + "\n")
    end = message.rfind("\n" + closing)
    if start < 0 or end < 0 or end < start + len(opening) + 1:
        return None
    return message[start + len(opening) + 1:end]


def file_names(sha256: str) -> tuple[str, str]:
    tag = sha256[:12]
    return f"spec-{tag}.md", f"spec-{tag}.index.tsv"


def index_file(spec: dict, sections: list[dict], requirements: list[dict],
               links: list[dict]) -> str:
    """The specification's index as a tab-separated file an agent can ``grep``."""
    def clean(value: Any) -> str:
        return " ".join(str(value or "").split())

    count = len(sections)
    out = [
        "# NEXUS specification index",
        f"# spec {spec['id']}  sha256 {spec['sha256']}",
        f"# {spec['char_count']} characters, {spec['byte_count']} bytes, "
        f"{spec['line_count']} lines",
        "# Lines are 1-based and inclusive: the numbers `sed -n` and `cat -n` use.",
        "#",
        "# SECTIONS: key, level, first line, last line, parent, number, title",
    ]
    for s in sections:
        parent = section_key(s["parent"], count) if s["parent"] is not None else "-"
        out.append("\t".join((s["key"], str(s["level"]), str(s["start_line"]),
                              str(s["end_line"]), parent, clean(s["number"]) or "-",
                              clean(s["title"]))))
    out += ["#", "# PIECES: key, section, kind, obligation (1/0), first line, last line, start"]
    for r in requirements:
        out.append("\t".join((r["key"], section_key(r["section"], count), r["kind"],
                              "1" if r["normative"] else "0", str(r["start_line"]),
                              str(r["end_line"]), clean(r["excerpt"])[:80])))
    out += ["#", "# LINKS: piece, section it refers to, the words that refer"]
    keys = {r["ordinal"]: r["key"] for r in requirements}
    for link in links:
        out.append("\t".join((keys.get(link["requirement"], "-"),
                              section_key(link["target_section"], count),
                              clean(link["reference"]))))
    return "\n".join(out) + "\n"


def _outline(sections: list[dict], first_line: int, last_line: int,
             wanted: set[str]) -> tuple[list[str], int]:
    """At most ``OUTLINE_LINES`` section lines: the top levels, this task's own
    sections and the ones it refers to. Returns the lines and how many sections
    were left out."""
    def mine(s: dict) -> bool:
        return s["start_line"] <= last_line and s["end_line"] >= first_line

    keep = [s for s in sections if mine(s) or s["key"] in wanted]
    must = {s["ordinal"] for s in keep}
    for level in sorted({s["level"] for s in sections}):
        more = [s for s in sections if s["level"] <= level or s["ordinal"] in must]
        if len(more) > OUTLINE_LINES:
            break
        keep = more
    keep = sorted(keep, key=lambda s: s["ordinal"])[:OUTLINE_LINES]
    lines = []
    for s in keep:
        name = " ".join(f"{s['number']} {s['title']}".split())[:100]
        lines.append(f"  {'>' if mine(s) else ' '} {s['key']}  lines {s['start_line']}-"
                     f"{s['end_line']}  {name or '(before the first heading)'}")
    return lines, len(sections) - len(keep)


def _pieces(specs: SpecService, spec: dict, ref: dict) -> list[str]:
    """The pieces a task covers, each with its lines and how it begins.

    The ids (``R-0012``) are the plan's, not the author's: they are not written
    in the text, so each is shown with where it is and its first words.
    """
    ordinals, _unknown = expand_keys(ref.get("requirements"), spec["requirement_count"])
    if not ordinals:
        return []
    wanted = sorted(ordinals)
    rows = specs.pieces_internal(str(spec["id"]), wanted[:PIECE_LINES])
    kind = "must carry out or confirm" if ref.get("verification") else "covers"
    lines = [f"The pieces of the text this step {kind} ({len(wanted)}):"]
    for row in rows:
        where = (f"line {row['start_line']}" if row["start_line"] == row["end_line"]
                 else f"lines {row['start_line']}-{row['end_line']}")
        lines.append(f"  {row['key']}  {where}: {' '.join(str(row['excerpt']).split())[:90]}")
    if len(wanted) > len(rows):
        last = requirement_key(wanted[-1], spec["requirement_count"])
        after = f" after {rows[-1]['key']}" if rows else ""
        lines.append(f"  ... and {len(wanted) - len(rows)} more{after}, up to {last}")
    return lines


def _part(ref: dict, spec: dict) -> tuple[int, int, int, int, int, int]:
    """``(part, parts, first line, last line, first byte, end byte)`` of a task."""
    lines = ref.get("lines") or [1, spec["line_count"]]
    span = ref.get("bytes") or [0, spec["byte_count"]]
    return (int(ref.get("stage") or 1), int(ref.get("stages") or 1),
            int(lines[0]), int(lines[1]), int(span[0]), int(span[1]))


def _whole(text: str, sha: str, lead: str, proof: dict[str, Any]) -> Delivery:
    """*text* embedded whole after *lead*."""
    opening, closing = markers(sha)
    block = f"{lead}\n{opening}\n{text}\n{closing}"
    if extract_inline(block, sha) != text:
        # Unreachable unless the text contains its own fingerprint; checked
        # because the alternative is an agent with a damaged task.
        raise DeliveryError("the original text could not be embedded in the message intact",
                            permanent=True)
    return Delivery("inline", block, [], {"mode": "inline", **proof})


def prepare_request(specs: SpecService, task: dict) -> Delivery | None:
    """For a task that refers to no specification: the request its run was
    planned from, whole, in the message. ``None`` when there is none to give.

    Such a task belongs to a run planned before specifications existed. Its
    plan was one model's reading of the request, and until now the agent saw
    only that reading - a title and a line or two. The request is at most what
    a goal held then (20 000 characters), so it always fits a message. This
    never refuses a start: a task that could be started without the request
    yesterday is not blocked for the lack of it today.
    """
    if not task.get("run_id"):
        return None
    text = specs.request_internal(str(task["run_id"]))
    if not text or not text.strip() or len(text) > message_chars():
        return None
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    sha = hashlib.sha256(data).hexdigest()
    lead = (
        "The task above is one step of a plan. The plan was made from the request below, "
        f"given here whole as it is stored ({len(text)} characters, SHA-256 {sha}). It is the "
        "source of truth - where the step and this text disagree, this text is right. The "
        "other steps of the plan are other tasks: do this step, not the whole request.\n")
    try:
        return _whole(text, sha, lead, {"source": "goal", "sha256": sha,
                                        "chars": len(text), "bytes": len(data)})
    except DeliveryError:
        return None


def _part_only(specs: SpecService, spec: dict, text: str, ref: dict,
               pieces: list[str]) -> Delivery:
    """The part of *text* a task was planned from, for a transport with no files."""
    spec_id, sha = str(spec["id"]), spec["sha256"]
    part, parts, first_line, last_line, first_byte, end_byte = _part(ref, spec)
    data = text.encode("utf-8")
    if not 0 <= first_byte < end_byte <= len(data):
        raise DeliveryError(
            f"the task's part of specification {spec_id} (bytes {first_byte}-{end_byte}) is "
            f"not inside the text ({len(data)} bytes)", permanent=True)
    own_data = data[first_byte:end_byte]
    try:
        own = own_data.decode("utf-8")
    except UnicodeDecodeError:
        raise DeliveryError(
            f"the task's part of specification {spec_id} (bytes {first_byte}-{end_byte}) "
            "does not begin and end between characters", permanent=True) from None
    own_sha = hashlib.sha256(own_data).hexdigest()
    opening, closing = markers(own_sha, "PART")
    sections = specs.sections_internal(spec_id)
    see = [s for s in (ref.get("see") or []) if isinstance(s, dict) and s.get("section")]
    outline, omitted = _outline(sections, first_line, last_line, {s["section"] for s in see})
    lines = [
        "The task above is one step of a plan. The plan was made from a specification of "
        f"{spec['char_count']} characters ({spec['line_count']} lines). That is too much for "
        "one message, and this OpenHands transport cannot put a file in your sandbox. So "
        "this message carries the part of the specification this step was planned from, "
        "byte for byte as its author wrote it:",
        "",
        f"  part {part} of {parts}: lines {first_line}-{last_line} of {spec['line_count']}, "
        f"{len(own)} characters, {len(own_data)} bytes, SHA-256 {own_sha}",
        "",
        "It is below, between the two NEXUS-TASK-PART lines. It is the "
        "source of truth - where the step and that text disagree, the text is right.",
        "",
        "The rest of the specification is NOT in this conversation: the other parts belong "
        "to other steps of the plan. Do not guess what they say. If this step cannot be done "
        f"correctly without text from outside lines {first_line}-{last_line}, do what can be "
        "done, and in your report name the sections you needed and what was left undone "
        "for the lack of them. The outline below lists what the specification contains.",
    ]
    if pieces:
        lines += ["", *pieces,
                  "  (line numbers are those of the whole specification; the part below "
                  f"begins at line {first_line})"]
    if see:
        lines.append("This part refers to sections outside it, which are not in this "
                     "conversation: " + "; ".join(
            f"{s['section']} {' '.join(str(s.get('title') or '').split())[:80]} "
            f"(lines {s['lines'][0]}-{s['lines'][1]})".replace("  ", " ")
            for s in see[:12] if isinstance(s.get("lines"), list) and len(s["lines"]) == 2))
    lines += ["", "Outline of the whole specification (\">\" marks this step's part):", *outline]
    if omitted:
        lines.append(f"  ({omitted} more sections are not listed)")
    block = "\n".join(lines) + f"\n\n{opening}\n{own}\n{closing}"
    if extract_inline(block, own_sha, "PART") != own:
        raise DeliveryError("the task's part of the text could not be embedded in the "
                            "message intact", permanent=True)
    return Delivery("part", block, [], {
        "mode": "part", "spec_id": spec_id, "sha256": sha,
        "chars": spec["char_count"], "bytes": spec["byte_count"],
        "part": {"stage": part, "stages": parts, "lines": [first_line, last_line],
                 "bytes": [first_byte, end_byte], "chars": len(own), "sha256": own_sha}})


def prepare(specs: SpecService, task: dict, *, files: bool = True) -> Delivery | None:
    """What *task*'s agent must be given, or ``None`` for a task with no
    specification (a row written before specifications existed).

    *files* says whether the transport can place a file in the sandbox. Where
    it cannot, a text too long for a message is handed over part by part (see
    the module's description).

    Raises :class:`~orchestration.cloud_client.DeliveryError` when the original
    cannot be handed over exactly: the row is gone, or it no longer matches its
    fingerprint. The task is then not started.
    """
    ref = task.get("spec_ref")
    if not isinstance(ref, dict) or not ref.get("spec_id"):
        return None
    spec_id = str(ref["spec_id"])
    try:
        spec, text = specs.content_internal(spec_id)
    except NotFound:
        raise DeliveryError(
            f"the task's original text (specification {spec_id}) no longer exists; "
            "the agent cannot be given the task", permanent=True) from None
    except SpecCorrupted as e:
        raise DeliveryError(str(e), permanent=True) from None
    sha = spec["sha256"]
    part, parts, first_line, last_line, first_byte, end_byte = _part(ref, spec)
    whole = parts == 1 and first_line == 1 and last_line == spec["line_count"]

    pieces = _pieces(specs, spec, ref)
    # In the message whole: always when it is short; and, where there can be no
    # file, also when it is not too long for a message - or when this task's
    # part is the whole of it, which would be sent in any case.
    if spec["char_count"] <= inline_chars() or (
            not files and (whole or spec["char_count"] <= message_chars())):
        where = "" if whole else (f" This step is part {part} of {parts}; its part of the "
                                  f"text is lines {first_line}-{last_line}.")
        lead = (
            "The task above is one step of a plan. The plan was made from the text below: "
            f"the request exactly as its author wrote it ({spec['char_count']} characters, "
            f"SHA-256 {sha}). It is the source of truth - where the step and this text "
            f"disagree, this text is right.{where}\n"
            + ("\n" + "\n".join(pieces) + "\n" if pieces else ""))
        return _whole(text, sha, lead, {"spec_id": spec_id, "sha256": sha,
                                        "chars": spec["char_count"],
                                        "bytes": spec["byte_count"]})
    if not files:
        return _part_only(specs, spec, text, ref, pieces)

    data = text.encode("utf-8")
    if len(data) != spec["byte_count"] or hashlib.sha256(data).hexdigest() != sha:
        raise DeliveryError(f"specification {spec_id} does not match its fingerprint",
                            permanent=True)
    sections = specs.sections_internal(spec_id)
    index_data = index_file(spec, sections, specs.requirements_internal(spec_id),
                            specs.links_internal(spec_id)).encode("utf-8")
    spec_name, index_name = file_names(sha)
    path, index_path = f"{FILES_DIR_TOKEN}/{spec_name}", f"{FILES_DIR_TOKEN}/{index_name}"

    see = [s for s in (ref.get("see") or []) if isinstance(s, dict) and s.get("section")]
    outline, omitted = _outline(sections, first_line, last_line, {s["section"] for s in see})
    lines = [
        "The task above is one step of a plan. The plan was made from a specification of "
        f"{spec['char_count']} characters ({spec['line_count']} lines). That is too much for a "
        "message, so the specification is a file in this sandbox, byte for byte as its author "
        "wrote it:",
        "",
        f"  {path}",
        f"      {spec['byte_count']} bytes, UTF-8, SHA-256 {sha}",
        f"  {index_path}",
        "      its index: every section and every piece, with line numbers",
        "",
        f"First run `sha256sum {path}` and compare the hash. If the file is missing or the "
        "hash differs, stop and say so in your report; do not work from this message alone.",
        "",
        "The specification is the source of truth - where this step and the file disagree, "
        f"the file is right. This step is part {part} of {parts}. Its part of the file:",
        f"  lines {first_line}-{last_line}:  sed -n '{first_line},{last_line}p' {path}",
        f"  bytes {first_byte}-{end_byte}:  tail -c +{first_byte + 1} {path} | "
        f"head -c {end_byte - first_byte}    (exact; use it when lines are very long)",
    ]
    if pieces:
        lines += ["", *pieces,
                  f"  (every piece and its lines: grep -P '^R-' {index_path})"]
    if see:
        lines.append("This part refers to: " + "; ".join(
            f"{s['section']} {' '.join(str(s.get('title') or '').split())[:80]} "
            f"(lines {s['lines'][0]}-{s['lines'][1]})".replace("  ", " ")
            for s in see[:12] if isinstance(s.get("lines"), list) and len(s["lines"]) == 2))
    lines += ["", "Outline (\">\" marks this step's part):", *outline]
    if omitted:
        lines.append(f"  ({omitted} more sections are in the index file)")
    lines += [
        "",
        "Read your part in full before you change anything, and read other parts when yours "
        "depends on them. The file may be more than you can hold at once: read it in ranges, "
        "not with one `cat`.",
    ]
    return Delivery(
        "file", "\n".join(lines),
        [SandboxFile(spec_name, data, sha),
         SandboxFile(index_name, index_data, hashlib.sha256(index_data).hexdigest())],
        {"mode": "file", "spec_id": spec_id, "sha256": sha,
         "chars": spec["char_count"], "bytes": spec["byte_count"]})
