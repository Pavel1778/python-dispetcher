"""Fetching the file OpenHands saves for a terminal observation.

OpenHands clips what the terminal tool returns. The agent-server shipped in
``ghcr.io/openhands/agent-server:1.60.1`` sets ``MAX_CMD_OUTPUT_SIZE = 30000``
(``openhands.tools.terminal.constants``, read from the exact wheel on 2026-11-15),
and every ``terminal_session`` truncates with **no** ``save_dir``, so the raw
untrimmed stdout/stderr of a command is lost at the session layer: what the
observation stores is head + truncation notice + tail, never more than 30,000
characters.

``TerminalObservation.to_llm_content`` (openhands-tools 1.60.1) then applies
``maybe_truncate(ret, 30000, save_dir=observation.full_output_save_dir,
tool_prefix="terminal")`` where ``ret`` is::

    ret = f"{metadata.prefix}{content_text}{metadata.suffix}"
    if metadata.working_dir:      ret += f"\\n[Current working directory: {…}]"
    if metadata.py_interpreter_path: ret += f"\\n[Python interpreter: {…}]"
    if metadata.exit_code != -1:  ret += f"\\n[Command finished with exit code {…}]"

When ``len(ret) > 30000`` - which happens precisely when the session already
clipped (its output is exactly 30,000 chars) - the complete ``ret`` is written
to ``terminal_output_<sha256(ret)[:8]>.txt`` inside
``observation.full_output_save_dir`` (the SDK's ``maybe_truncate`` with
``tool_prefix="terminal"``). **That file contains the clipped rendering, not
the raw command output**: it adds the prefix/suffix/wrapper lines around the
same ≤30,000-character body the event already shows. The raw, untrimmed output
is not persisted anywhere by agent-server 1.60.1, and there is no external API
to it. NEXUS therefore does **not** claim to recover raw stdout; what this
module recovers is the SDK's saved rendering, byte-exact, or nothing.

Every component of ``ret`` is present in the event JSON, so the file name is
recomputable. What NEXUS does **not** know - and never guesses - is where on
the sandbox that directory lives: it is whatever the event says, and the
event is untrusted input. This module is the boundary:

* the save directory must be absolute POSIX, at most 512 characters, no
  backslashes or NULs, every component matching ``[A-Za-z0-9][A-Za-z0-9._-]*``
  and none of them ``.`` or ``..``;
* the file name is never taken from the event. It is recomputed from the
  event's own text and metadata, or refused;
* at most :data:`MAX_OUTPUT_BYTES` are read, and a body that does not fit
  within the cap is refused whole - never silently truncated to fit;
* the path never leaves this module. The API answers with text, a size and a
  flag, never with a location on the sandbox's filesystem.

Only the ``direct`` and local agent-server transports can read the file (both
speak ``GET /api/file/download?path=`` with the sandbox's own key). The Cloud
``app`` transport exposes workspace views, not arbitrary paths, so full output
is reported ``unsupported`` there rather than claimed.

Nothing here is written to PostgreSQL: a multi-megabyte log belongs on the
sandbox it came from, not in the database. The text is delivered to the
caller and, on read, recorded as ``terminal_output.read`` in the project's
event log.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Hard cap on a recovered file. Enough for any log worth reading; small
#: enough that a hostile or broken sandbox cannot make this server buffer an
#: unbounded body.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: How much of the stored observation text is shown per row in listings.
PREVIEW_CHARS = 600

#: Events scanned per conversation when looking for outputs.
MAX_EVENTS = 300

#: Terminal outputs returned per run listing.
MAX_REFS = 50

#: Events fetched per page from the agent-server (its own cap is 100).
EVENTS_PAGE = 100

_SAVE_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,510}$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OUTPUT_FILE_RE = re.compile(r"^terminal_output_[0-9a-f]{8}\.txt$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Markers the SDK leaves in a clipped observation. The base notice (no file
#: reference) is what ends up stored in the event stream; the persist notice
#: (with the file reference) is only ever in the model-facing copy.
_CLIP_MARKERS = ("<response clipped>",)


class OutputRefused(Exception):
    """The event cannot yield a readable file (bad shape, bad path)."""


class OutputUnavailable(Exception):
    """The transport cannot reach the file (unsupported, or the request failed)."""


class RuntimeFetchError(Exception):
    """The sandbox could not be read (transient from the caller's view)."""


class UnsupportedTransport(Exception):
    """This transport cannot read sandbox files at all (the Cloud app API)."""


class OutputTooLarge(Exception):
    """The saved output exceeds :data:`MAX_OUTPUT_BYTES`."""


class FileTooLarge(Exception):
    """The saved output exceeds :data:`MAX_OUTPUT_BYTES` (internal signal)."""


@dataclass(frozen=True)
class TerminalOutputRef:
    """One terminal observation worth recovering, with its safe preview."""

    event_id: str
    conversation_id: str
    action_id: str | None
    tool_call_id: str | None
    command: str
    exit_code: int | None
    is_error: bool
    at: str | None
    output_chars: int
    clipped: bool
    #: ``saved`` / ``missing`` / ``unknown`` / ``unsupported`` - the status
    #: of the SDK's saved rendering file, never of raw output (which the
    #: session layer has already discarded).
    saved_output: str = "unknown"
    preview: str = ""
    #: Resolved path, never serialised into an API response. Kept on the
    #: Python side so the fetch does not have to recompute it.
    _path: str | None = field(default=None, repr=False)


def _text_parts(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def clipped_in_stream(text: str) -> bool:
    return any(marker in text for marker in _CLIP_MARKERS)


def normalize_save_dir(raw: object) -> str | None:
    """Validate an untrusted ``full_output_save_dir`` from an event.

    Returns the normalised absolute POSIX path, or ``None`` when the value is
    not one a sandbox directory may be. Nothing about the host filesystem is
    consulted - the check is purely lexical - because the directory belongs
    to the sandbox, not to this server.
    """
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    if not path or len(path) > 512 or "\x00" in path or "\\" in path:
        return None
    if not _SAVE_DIR_RE.fullmatch(path):
        return None
    components = path.split("/")[1:]
    if not components or any(part in (".", "..") for part in components):
        return None
    if any(not _COMPONENT_RE.fullmatch(part) for part in components):
        return None
    # Lexically absolute, no dot segments anywhere: nothing to escape with.
    return "/" + "/".join(components)


def render_observation(observation: dict[str, Any]) -> str | None:
    """Rebuild the exact string the SDK hashes for ``terminal_output_<sha8>.txt``.

    Mirrors ``TerminalObservation.to_llm_content`` (agent-server 1.60.1); the
    hash preimage is the *whole* string, stored text included. Returns ``None``
    when the observation does not carry enough to rebuild it exactly - never a
    guess.
    """
    if not isinstance(observation, dict):
        return None
    metadata = observation.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    text = _text_parts(observation.get("content"))
    prefix = metadata.get("prefix")
    suffix = metadata.get("suffix")
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        return None
    ret = f"{prefix}{text}{suffix}"
    working_dir = metadata.get("working_dir")
    if isinstance(working_dir, str) and working_dir:
        ret += f"\n[Current working directory: {working_dir}]"
    interpreter = metadata.get("py_interpreter_path")
    if isinstance(interpreter, str) and interpreter:
        ret += f"\n[Python interpreter: {interpreter}]"
    exit_code = metadata.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = -1
    if exit_code != -1:
        ret += f"\n[Command finished with exit code {exit_code}]"
    return ret


def expected_file_name(observation: dict[str, Any]) -> str | None:
    rendered = render_observation(observation)
    if rendered is None:
        return None
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:8]
    return f"terminal_output_{digest}.txt"


def output_path(observation: dict[str, Any]) -> str | None:
    """The one path this event may yield, fully validated.

    ``None`` when the event does not carry a usable directory or the rendering
    cannot be rebuilt; the caller reports "missing" rather than inventing a
    path.
    """
    directory = normalize_save_dir(observation.get("full_output_save_dir"))
    if directory is None:
        return None
    name = expected_file_name(observation)
    if name is None or not _OUTPUT_FILE_RE.fullmatch(name):
        return None
    return f"{directory}/{name}"


def _event_id(event: dict[str, Any]) -> str | None:
    raw = event.get("id")
    if isinstance(raw, str) and _EVENT_ID_RE.fullmatch(raw):
        return raw
    return None


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…"


def is_terminal_observation(event: dict[str, Any]) -> bool:
    if event.get("kind") != "ObservationEvent":
        return False
    tool = event.get("tool_name")
    observation = event.get("observation")
    inner_kind = observation.get("kind") if isinstance(observation, dict) else None
    return tool == "terminal" or inner_kind == "TerminalObservation"


def extract_refs(
    events: Iterable[dict[str, Any]],
    conversation_id: str,
    *,
    limit: int = MAX_REFS,
) -> list[TerminalOutputRef]:
    """Turn raw events into recoverable-output refs, newest first."""
    refs: list[TerminalOutputRef] = []
    for event in events:
        if not isinstance(event, dict) or not is_terminal_observation(event):
            continue
        event_id = _event_id(event)
        observation = event.get("observation")
        if event_id is None or not isinstance(observation, dict):
            continue
        text = _text_parts(observation.get("content"))
        metadata = observation.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        command = observation.get("command")
        if not isinstance(command, str):
            command = ""
        exit_code = observation.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = metadata.get("exit_code") if isinstance(metadata.get("exit_code"), int) else None
        path = output_path(observation)
        refs.append(TerminalOutputRef(
            event_id=event_id,
            conversation_id=conversation_id,
            action_id=event.get("action_id") if isinstance(event.get("action_id"), str) else None,
            tool_call_id=event.get("tool_call_id") if isinstance(event.get("tool_call_id"), str) else None,
            command=command[:2000],
            exit_code=exit_code if isinstance(exit_code, int) else None,
            is_error=bool(observation.get("is_error")),
            at=event.get("timestamp") if isinstance(event.get("timestamp"), str) else None,
            output_chars=len(text),
            clipped=clipped_in_stream(text),
            saved_output="unknown" if path else "missing",
            preview=_preview(text),
            _path=path,
        ))
    refs.sort(key=lambda r: r.at or "", reverse=True)
    return refs[:limit]


def ref_public(ref: TerminalOutputRef) -> dict[str, Any]:
    """The API view of a ref: never the path, never the hash."""
    return {
        "id": ref.event_id,
        "conversation_id": ref.conversation_id,
        "action_id": ref.action_id,
        "tool_call_id": ref.tool_call_id,
        "command": ref.command,
        "exit_code": ref.exit_code,
        "is_error": ref.is_error,
        "at": ref.at,
        "output_chars": ref.output_chars,
        "clipped": ref.clipped,
        "saved_output": ref.saved_output,
        "preview": ref.preview,
    }


# ---------------------------------------------------------------- client side

def capability(client: Any) -> str:
    """``agent-server`` when the client can read sandbox files, else ``unsupported``."""
    from .cloud_direct import CloudDirectClient
    from .openhands_client import OpenHandsClient

    if isinstance(client, (CloudDirectClient, OpenHandsClient)):
        return "agent-server"
    # A test double that honours the same contract is accepted; the caller
    # still gets the same validation and the same caps applied.
    if callable(getattr(client, "download_file", None)):
        return "agent-server"
    return "unsupported"


def download_file(client: Any, conversation_id: str, path: str, *, limit: int) -> tuple[int, bytes]:
    """One bounded read of ``path`` from the sandbox the conversation lives on.

    Returns ``(status, body)`` with at most ``limit + 1`` bytes in ``body``.
    Raises :class:`OutputUnavailable` when the transport cannot read files or
    the sandbox cannot be reached - never reports a failure as content.
    """
    from .cloud_direct import CloudDirectClient
    from .openhands_client import ApiUnavailable, OpenHandsClient

    try:
        if isinstance(client, CloudDirectClient):
            return client.download_file(conversation_id, path, limit=limit)
        if isinstance(client, OpenHandsClient):
            return client.download_file(path, limit=limit)
        fn = getattr(client, "download_file", None)
        if callable(fn):
            try:
                return fn(path, limit=limit)
            except TypeError:
                return fn(conversation_id, path, limit=limit)
        raise OutputUnavailable("this transport cannot read sandbox files")
    except OutputUnavailable:
        raise
    except ApiUnavailable as e:
        raise OutputUnavailable(str(e)) from e
    except Exception as e:  # noqa: BLE001 - never leak transport internals
        raise OutputUnavailable(f"{type(e).__name__}") from e


def fetch_full_output(client: Any, ref: TerminalOutputRef) -> bytes:
    """Read the file behind ``ref``, whole, bounded by :data:`MAX_OUTPUT_BYTES`.

    Raises :class:`OutputRefused` for a ref whose path is not valid (nothing to
    fetch), :class:`OutputUnavailable` for transport failures and for a 404, and
    :class:`FileTooLarge` when the file exceeds the cap.
    """
    if not ref._path:
        raise OutputRefused("the event carries no usable full-output reference")
    status, body = download_file(client, ref.conversation_id, ref._path,
                                 limit=MAX_OUTPUT_BYTES)
    if status == 404:
        raise OutputUnavailable("the saved output is not there any more")
    if status != 200:
        raise OutputUnavailable(f"the sandbox answered {status}")
    if len(body) > MAX_OUTPUT_BYTES:
        raise FileTooLarge(f"the saved output is larger than {MAX_OUTPUT_BYTES} bytes")
    return body


def find_ref(events: Iterable[dict[str, Any]], conversation_id: str,
             event_id: str) -> TerminalOutputRef | None:
    for ref in extract_refs(events, conversation_id, limit=MAX_REFS * 4):
        if ref.event_id == event_id:
            return ref
    return None
