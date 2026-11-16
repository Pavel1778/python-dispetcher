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
from typing import Any, Callable, Iterable

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

#: The agent-server cuts a terminal observation at this many characters
#: (``MAX_CMD_OUTPUT_SIZE``, v1.60.1). An observation whose content reaches it
#: was cut, whether or not a marker is present, so this length is a second,
#: independent truncation signal.
TERMINAL_OUTPUT_CAP_CHARS = 30_000


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
    #: The event's own (uncleaned) truncated text, used only to match the saved
    #: file by prefix. Never serialised into an API response.
    match_prefix: str = ""
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
    """Whether an observation's text was cut by the agent-server.

    Two independent signals, either of which means "truncated": the SDK's own
    clip marker, or a body that reaches the server's cut-off length (a body that
    long was cut at the boundary, marker or not).
    """
    if any(marker in text for marker in _CLIP_MARKERS):
        return True
    return len(text) >= TERMINAL_OUTPUT_CAP_CHARS


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
            match_prefix=text,
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


#: The whole conversation directory as a ZIP; bounded so one request cannot
#: pull an unbounded archive into memory.
TRAJECTORY_MAX_BYTES = 64 * 1024 * 1024
#: The decompressed size one output file may have. The compressed archive is
#: already bounded, but a small archive can expand enormously (a ZIP bomb), so
#: the member is bounded on its own too.
OUTPUT_MAX_BYTES = 64 * 1024 * 1024


def download_trajectory(client: Any, conversation_id: str, *, limit: int) -> tuple[int, bytes]:
    """The conversation's own directory as a ZIP, via the scoped route.

    ``GET /api/conversations/{id}/file/download-trajectory/{id}`` on the
    sandbox the conversation lives on. The route is **conversation scoped**:
    the server refuses a path outside that conversation's own directory, so the
    archive is the safe way to reach the saved output files, and it is the
    transport this module uses - not the global ``/api/file/download?path=``
    endpoint, which would let a caller name any path on the sandbox.

    Raises :class:`OutputUnavailable` when the transport cannot read files.
    """
    from .cloud_direct import CloudDirectClient
    from .local_runtime import LocalCloudClient
    from .openhands_client import ApiUnavailable, OpenHandsClient

    try:
        if isinstance(client, CloudDirectClient):
            return client._http.download_trajectory(
                client._route_for(conversation_id).agent_server_url,
                client._route_for(conversation_id).session_api_key,
                conversation_id, limit=limit)
        if isinstance(client, LocalCloudClient):
            return client.download_trajectory(
                client.base_url, client._auth_key(), conversation_id, limit=limit)
        if isinstance(client, OpenHandsClient):
            return client.download_trajectory(
                client._base_url(), client._auth_key(), conversation_id, limit=limit)
        fn = getattr(client, "download_trajectory", None)
        if callable(fn):
            return fn(conversation_id, limit=limit)
        raise OutputUnavailable("this transport cannot read sandbox files")
    except OutputUnavailable:
        raise
    except ApiUnavailable as e:
        raise OutputUnavailable(str(e)) from e
    except Exception as e:  # noqa: BLE001 - never leak transport internals
        raise OutputUnavailable(f"{type(e).__name__}") from e


def _bounded_zip_member(archive: Any, name: str, *, limit: int) -> bytes | None:
    """Read one member of *archive*, or ``None`` if it is larger than *limit*.

    Mirrors ``OpenHandsClient.bounded_zip_member`` so this module does not
    depend on which client shape the caller passed. ``ZipInfo.file_size`` is
    read first and refused when over the limit, and the stream is read with a
    cap as well, because a hostile archive can understate the size it declares.
    Returns ``None`` (never a partial buffer) for a member over the limit.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    out = bytearray()
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(min(65536, limit - len(out) + 1))
            if not chunk:
                break
            out += chunk
            if len(out) > limit:
                return None
    return bytes(out)


def fetch_full_output(client: Any, ref: TerminalOutputRef) -> bytes:
    """Read the file behind ``ref`` from the conversation-scoped archive.

    The output file is read through the trajectory archive (a conversation's own
    directory, never a caller-named path). Two independent checks decide which
    member it is, so a substituted file cannot pass as the event's output:

    * when the event carries enough metadata to recompute the SDK's file name
      (``terminal_output_<sha256(rendered)[:8]>.txt``), the member must have
      exactly that name **and** its bytes must start with the event's clipped
      text; and
    * otherwise (an event with no recomputable name) the member whose bytes
      start with the event's clipped text is used - the Phase 4 rule - so a real
      output is still recovered, but a member that does not match the event's
      own text is never returned.

    Raises :class:`OutputRefused` when the event carries no usable reference,
    :class:`OutputUnavailable` when the transport cannot read the archive or no
    member matches, and :class:`FileTooLarge` when the archive or a matching
    member exceeds its cap.
    """
    import io
    import zipfile

    if not ref.match_prefix:
        raise OutputRefused("the event carries no usable full-output reference")
    try:
        status, blob = download_trajectory(client, ref.conversation_id,
                                           limit=TRAJECTORY_MAX_BYTES)
    except OutputUnavailable:
        raise
    if status in (404, 422):
        raise OutputUnavailable("the conversation archive is not there any more")
    if status != 200:
        raise OutputUnavailable(f"the sandbox answered {status}")
    if len(blob) > TRAJECTORY_MAX_BYTES:
        raise FileTooLarge(f"the conversation archive is larger than {TRAJECTORY_MAX_BYTES} bytes")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise OutputUnavailable("the conversation archive could not be read") from e

    prefix = ref.match_prefix.encode("utf-8", "replace")
    wanted_name = ref._path.rsplit("/", 1)[-1] if ref._path else None
    with archive:
        names = [n for n in archive.namelist()
                 if n.endswith(".txt") and ".." not in n.split("/")]
        # 1. The member whose name is exactly the one recomputed from the event
        #    (only when the event carried enough metadata to recompute it).
        if wanted_name:
            for name in names:
                if name.rsplit("/", 1)[-1] != wanted_name:
                    continue
                data = _bounded_zip_member(archive, name, limit=OUTPUT_MAX_BYTES)
                if data is None:
                    raise FileTooLarge(
                        f"the saved output is larger than {OUTPUT_MAX_BYTES} bytes")
                if data.startswith(prefix):
                    return data
        # 2. Fall back to the member whose bytes start with the event's text.
        for name in names:
            base = name.rsplit("/", 1)[-1]
            if not base.startswith("terminal_output_"):
                continue
            data = _bounded_zip_member(archive, name, limit=OUTPUT_MAX_BYTES)
            if data is None:
                continue
            if data.startswith(prefix):
                return data
    raise OutputUnavailable("no saved output matches the truncated event")


def find_ref(events: Iterable[dict[str, Any]], conversation_id: str,
             event_id: str) -> TerminalOutputRef | None:
    for ref in extract_refs(events, conversation_id, limit=MAX_REFS * 4):
        if ref.event_id == event_id:
            return ref
    return None


# ---------------------------------------------------------------- task scoping
#
# The HTTP routes, the CLI and the MCP tools all address a *task* of a run, not
# a conversation: a task is what a person starts and reads. These helpers resolve
# the task (honouring project membership) and then use the primitives above, so
# there is exactly one recovery mechanism - this module - reached three ways.

#: ``client_for(principal, account_id) -> a client that can read the sandbox``.
#: The caller supplies it because the server and the MCP process build it
#: differently (the server has its own account service and process client).
ClientFor = Callable[[str, "str | None"], Any]


def run_task(store: Any, principal: str, project_id: str, run_id: str,
             task_id: str) -> dict[str, Any]:
    """A task of *run_id* in *project_id*, or ``NotFound``.

    Membership is checked through the run, so a non-member never learns whether
    the task exists (``NotFound``, never ``Forbidden``).
    """
    from .cloud_store import NotFound
    from .nexus_lab_store import LabStore

    if not _is_uuid(task_id):
        raise NotFound("task id")
    lab = LabStore(store)
    lab.get_run(principal, project_id, run_id)  # membership + existence
    task = next((t for t in lab.list_tasks(principal, project_id, run_id)
                 if str(t["id"]) == task_id), None)
    if task is None:
        raise NotFound("task not found in this run")
    return task


def run_task_outputs(store: Any, principal: str, project_id: str, run_id: str,
                     task_id: str, client_for: ClientFor) -> dict[str, Any]:
    """Which terminal outputs of the task's conversation were cut off.

    An empty list (never an error) when the task has no conversation yet, when
    the transport cannot reach the archive (``unsupported`` says so), or when
    nothing was cut. Uses the same validation as the run-level listing.
    """
    from .cloud_client import CloudNotFound
    from .cloud_store import NotFound

    task = run_task(store, principal, project_id, run_id, task_id)
    conversation_id = task.get("conversation_id")
    base = {"task_id": task_id, "conversation_id": conversation_id, "outputs": []}
    if not conversation_id:
        return base
    client = client_for(principal, task.get("openhands_account_id"))
    if capability(client) != "agent-server":
        return {**base, "unsupported": True,
                "reason": "this transport cannot read sandbox files"}
    try:
        events = client.list_events(str(conversation_id), limit=EVENTS_PAGE, max_pages=3)
    except CloudNotFound as e:
        raise NotFound(str(e) or "conversation not found") from None
    refs = extract_refs(events, str(conversation_id))
    # The task route lists the outputs that were **cut off** - the ones worth
    # fetching. A short observation is not truncated, so it is not listed.
    outputs = [{"event_id": r.event_id, "shown_chars": r.output_chars,
                "truncated": True, "command": r.command,
                "exit_code": r.exit_code, "at": r.at}
               for r in refs if r.clipped]
    return {**base, "outputs": outputs}


def run_task_output(store: Any, principal: str, project_id: str, run_id: str,
                    task_id: str, event_id: str, client_for: ClientFor) -> dict[str, Any]:
    """The complete terminal output whose preview one event showed.

    The event is read first (its own text is the key the archive is searched
    with), so the file returned is the one its preview named - not a file
    guessed by name. ``NotFound`` when the event is unknown or the output is
    gone; ``ValueError`` when the transport cannot read it.
    """
    from .cloud_client import CloudNotFound
    from .cloud_store import NotFound

    task = run_task(store, principal, project_id, run_id, task_id)
    conversation_id = task.get("conversation_id")
    if not conversation_id:
        raise NotFound("the task has no conversation yet")
    client = client_for(principal, task.get("openhands_account_id"))
    if capability(client) != "agent-server":
        raise ValueError("this transport cannot read sandbox files")
    try:
        events = client.list_events(str(conversation_id), limit=EVENTS_PAGE, max_pages=3)
    except CloudNotFound as e:
        raise NotFound(str(e) or "conversation not found") from None
    ref = find_ref(events, str(conversation_id), event_id)
    if ref is None:
        raise NotFound("no such event in the task's conversation")
    try:
        data = fetch_full_output(client, ref)
    except OutputRefused as e:
        raise NotFound(str(e)) from None
    except UnsupportedTransport as e:
        raise ValueError(str(e)) from None
    except FileTooLarge as e:
        raise ValueError(str(e)) from None
    except OutputUnavailable as e:
        raise NotFound(str(e)) from None
    return {"task_id": task_id, "conversation_id": conversation_id,
            "event_id": event_id, "shown_chars": len(ref.match_prefix),
            "name": ref._path.rsplit("/", 1)[-1] if ref._path else "",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "text": data.decode("utf-8", "replace")}


def _is_uuid(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""))
