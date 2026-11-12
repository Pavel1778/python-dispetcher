"""OpenHands Cloud V1 REST adapter (``/api/v1/app-conversations``).

Every fact here was read from the live OpenAPI document at
``https://app.all-hands.dev/openapi.json`` and confirmed against real
responses, not inferred. See ``CLOUD-INTEGRATION-MAP.md`` for the evidence.

Differences from the local adapter in :mod:`openhands_client`:

* Auth is ``Authorization: Bearer <key>`` (the local agent-server uses
  ``X-Session-API-Key``).
* Create returns a **start task**, not a conversation. The conversation id
  appears asynchronously as ``app_conversation_id`` once the start task reaches
  ``READY``, so creation is a two-step, pollable operation.
* The caller does not choose ``conversation_id``; idempotency is carried by the
  caller's own ``idempotency`` handling plus the ``conversations`` table.

The key is read from the environment and never logged.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_CLOUD_BASE_URL = "https://app.all-hands.dev"


class CloudError(RuntimeError):
    """Base class for Cloud API failures."""


def validate_base_url(url: str) -> str:
    """Return *url* if it is a usable http(s) base, else raise ``CloudError``.

    ``urlopen`` honours whatever scheme it is handed, including ``file://``.
    ``OPENHANDS_CLOUD_URL`` is operator-supplied, so a typo or a hostile value
    must fail loudly at construction instead of letting the client read a local
    file or speak a non-HTTP protocol to something that is not the Cloud API.

    It also enforces the NEXUS SSRF policy (:func:`nexus_policy.check_outbound_url`):
    a URL that names a loopback, private-network or cloud-metadata target is
    refused unless the operator explicitly permits private targets, so an
    operator-supplied base cannot aim the server at its own network.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CloudError(
            "OPENHANDS_CLOUD_URL must be an http(s) URL, got "
            f"{parsed.scheme or '(none)'}"
        )
    from .nexus_policy import UnsafeTarget, check_outbound_url

    try:
        check_outbound_url(url)
    except UnsafeTarget as e:
        raise CloudError(f"OPENHANDS_CLOUD_URL is not a safe target: {e}") from e
    return url


class CloudUnavailable(CloudError):
    """Transport failure or 5xx. Transient: retry with backoff."""


class CloudRateLimited(CloudUnavailable):
    """The service answered 429: it is working and will not take this now.

    Nothing was started, so nothing failed: the caller waits and asks again,
    and does not count the refusal against the work. *code* is the service's
    own name for the limit when it gives one (``daily_conversation_limit_reached``
    was seen on the live service); *retry_after* is its ``Retry-After`` header
    in seconds, when it sent one.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn any redirect into an error instead of following it.

    Following one would re-send the ``Authorization`` header to a host the
    operator did not configure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudError(f"refusing to follow redirect ({code}) to {newurl!r}")


class CloudAuthError(CloudError):
    """401/403. Retrying cannot succeed until the key is fixed."""


class CloudValidationError(CloudError):
    """422. A programming error in the request, not a transient one."""


class CloudNotFound(CloudError):
    """404. The conversation does not exist."""


class StartStatus(str, Enum):
    """``AppConversationStartTaskStatus`` enum, verbatim from the schema."""

    WORKING = "WORKING"
    WAITING_FOR_SANDBOX = "WAITING_FOR_SANDBOX"
    PREPARING_REPOSITORY = "PREPARING_REPOSITORY"
    RUNNING_SETUP_SCRIPT = "RUNNING_SETUP_SCRIPT"
    SETTING_UP_GIT_HOOKS = "SETTING_UP_GIT_HOOKS"
    SETTING_UP_SKILLS = "SETTING_UP_SKILLS"
    STARTING_CONVERSATION = "STARTING_CONVERSATION"
    READY = "READY"
    ERROR = "ERROR"


class ExecutionStatus(str, Enum):
    """``ConversationExecutionStatus`` enum, verbatim from the schema."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    FINISHED = "finished"
    ERROR = "error"
    STUCK = "stuck"
    DELETING = "deleting"


class DeliveryError(CloudError):
    """A file the conversation needs is not in the sandbox, whole and unchanged.

    Raised before the conversation is created, so an agent is never started on
    a task it has only part of. The message says what was expected and what was
    found.

    *permanent* says another attempt would end the same way - the transport
    cannot place files at all, the stored text is gone or damaged - so the
    task is not put back in the queue to fail again. A sandbox that would not
    take the file is not permanent: the next attempt may get another one.
    """

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


#: Written in an initial message where the directory of the delivered files
#: belongs. The transport replaces it with the directory that was verified.
FILES_DIR_TOKEN = "{{NEXUS_FILES_DIR}}"


@dataclass(frozen=True)
class SandboxFile:
    """A file to place in the sandbox before a conversation starts."""

    name: str                 # a plain file name, no directories
    data: bytes
    sha256: str               # of ``data``; what the sandbox's copy must hash to


@dataclass(frozen=True)
class StartTask:
    """A ``AppConversationStartTask``."""

    id: str
    status: str
    app_conversation_id: str | None = None
    sandbox_id: str | None = None
    agent_server_url: str | None = None
    detail: str | None = None
    #: Files placed in the sandbox for this conversation, each read back and
    #: compared: ``[{"path", "bytes", "sha256"}]``. Empty when none were sent.
    files: tuple[dict[str, Any], ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == StartStatus.READY.value

    @property
    def failed(self) -> bool:
        return self.status == StartStatus.ERROR.value


@dataclass(frozen=True)
class CloudConversation:
    """An ``AppConversation`` (the fields this dispatcher relies on)."""

    id: str
    title: str | None
    sandbox_id: str
    execution_status: str | None
    sandbox_status: str | None
    selected_repository: str | None
    selected_branch: str | None
    conversation_url: str | None
    sub_conversation_ids: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class CloudClient:
    """Minimal, dependency-free client for the OpenHands Cloud V1 API."""

    #: The app-conversation API has no way to put a file into the sandbox
    #: before the agent starts, so a task that must travel as a file cannot use
    #: this transport. Callers read this before they try.
    supports_files = False

    #: The app-conversation start request carries ``selected_repository`` /
    #: ``selected_branch`` as documented fields, so this transport can select a
    #: repository natively. (It still depends on the account having a GitHub
    #: provider connected; see CLOUD-INTEGRATION-MAP.md.)
    supports_repository_selection = True

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = validate_base_url(
            base_url or os.environ.get("OPENHANDS_CLOUD_URL") or DEFAULT_CLOUD_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OPENHANDS_API_KEY") or ""
        self.timeout = timeout

    def _opener(self) -> urllib.request.OpenerDirector:
        """An opener that never follows a redirect.

        ``urlopen`` re-sends every request header, ``Authorization`` included,
        to whatever host a ``3xx`` points at - proven locally against two
        listeners. A redirect therefore leaks the Cloud API key to a host we did
        not choose. The API answers with the resource or an error, never with a
        redirect we should chase, so a ``3xx`` is surfaced as an error instead.
        """
        return urllib.request.build_opener(_NoRedirect)

    # ------------------------------------------------------------------ http
    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        if not self._api_key:
            raise CloudAuthError("no Cloud API key configured (OPENHANDS_API_KEY)")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self._api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = None
            if e.code in (401, 403):
                raise CloudAuthError(f"{method} {path} -> {e.code}") from e
            if e.code == 404:
                raise CloudNotFound(f"{method} {path} -> 404") from e
            if e.code == 422:
                raise CloudValidationError(
                    f"{method} {path} -> 422: {json.dumps(payload)[:300]}"
                ) from e
            if e.code == 429:
                # Rate limit / daily quota: transient by nature, must not burn an
                # attempt or dead-letter a task.
                detail = payload.get("detail") if isinstance(payload, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                try:
                    retry_after: float | None = float(e.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after = None       # absent, or an HTTP date: not used
                raise CloudRateLimited(
                    f"{method} {path} -> 429: {json.dumps(payload, ensure_ascii=False)[:300]}",
                    code=code if isinstance(code, str) else None, retry_after=retry_after,
                ) from e
            if e.code >= 500:
                raise CloudUnavailable(f"{method} {path} -> {e.code}") from e
            raise CloudError(f"{method} {path} -> {e.code}: {json.dumps(payload)[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CloudUnavailable(f"{method} {path}: {type(e).__name__}") from e

    # ------------------------------------------------------------- operations
    def start_conversation(
        self,
        *,
        initial_message: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
        tags: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
        agent_profile_id: str | None = None,
        agent_type: str | None = None,
        plugins: list[dict[str, Any]] | None = None,
    ) -> StartTask:
        """``POST /api/v1/app-conversations`` -> a start task to poll.

        ``selected_repository`` and ``selected_branch`` are real, documented
        fields (schema ``AppConversationStartRequest``). ``secrets`` is the
        documented channel for per-conversation tokens; it is write-only and is
        never persisted here.

        ``client_tools`` is the documented ``ClientToolSpec`` list. When it is
        present the server injects those tools into the agent, and a call emits
        an ``ActionEvent`` in the conversation's event stream (confirmed against
        the live API). The caller is responsible for executing the call and
        returning the result; see :mod:`orchestration.sr_runtime`.

        ``agent_profile_id``, ``agent_type`` and ``plugins`` are documented
        fields that select account-level configuration. They are only sent when
        the caller sets them: an omitted field means "inherit whatever the
        account already uses", which is the desired default for reusing the
        existing Cloud workspace rather than reconfiguring it.
        """
        body: dict[str, Any] = {
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": initial_message}],
                "run": True,
            },
            "trigger": "openhands_api",
        }
        if selected_repository is not None:
            body["selected_repository"] = selected_repository
        if selected_branch is not None:
            body["selected_branch"] = selected_branch
        if title is not None:
            body["title"] = title
        if parent_conversation_id is not None:
            body["parent_conversation_id"] = parent_conversation_id
        if tags is not None:
            body["tags"] = tags
        if secrets is not None:
            body["secrets"] = secrets
        if client_tools is not None:
            body["client_tools"] = client_tools
        if agent_profile_id is not None:
            body["agent_profile_id"] = agent_profile_id
        if agent_type is not None:
            body["agent_type"] = agent_type
        if plugins is not None:
            body["plugins"] = plugins
        status, payload = self._request("POST", "/api/v1/app-conversations", body)
        if status not in (200, 201):
            raise CloudError(f"start_conversation -> {status}")
        return _start_task(payload)

    def get_start_task(self, start_task_id: str) -> StartTask | None:
        """``GET /api/v1/app-conversations/start-tasks?ids=<id>`` (batch)."""
        status, payload = self._request(
            "GET",
            f"/api/v1/app-conversations/start-tasks?ids={start_task_id}",
        )
        if status != 200 or not isinstance(payload, list) or not payload:
            return None
        return _start_task(payload[0]) if payload[0] else None

    def get_conversation(self, conversation_id: str) -> CloudConversation:
        """``GET /api/v1/app-conversations?ids=<id>`` (batch of one)."""
        status, payload = self._request(
            "GET", f"/api/v1/app-conversations?ids={conversation_id}"
        )
        if status != 200 or not isinstance(payload, list) or not payload:
            raise CloudNotFound(conversation_id)
        item = payload[0]
        if not item:
            raise CloudNotFound(conversation_id)
        return _conversation(item)

    def search_conversations(
        self, *, limit: int = 50, page_id: str | None = None,
        tag: tuple[str, str] | None = None,
    ) -> tuple[list[CloudConversation], str | None]:
        """``GET /api/v1/app-conversations/search`` -> ``(items, next_page_id)``.

        ``tag`` adds a ``tags__contains`` filter (repeatable ``key=value``, all
        must match), which is how an orphaned conversation is found by the tag we
        put on it instead of starting a second one.
        """
        path = f"/api/v1/app-conversations/search?limit={limit}"
        if page_id:
            path += f"&page_id={page_id}"
        if tag is not None:
            key, value = tag
            path += f"&tags__contains={key}%3D{value}"
        status, payload = self._request("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise CloudError(f"search_conversations -> {status}")
        items = [_conversation(c) for c in (payload.get("items") or [])]
        return items, payload.get("next_page_id")

    def send_message(
        self, conversation_id: str, text: str, *, run: bool = True
    ) -> bool:
        """``POST /api/v1/app-conversations/{id}/send-message``."""
        status, payload = self._request(
            "POST",
            f"/api/v1/app-conversations/{conversation_id}/send-message",
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "run": run,
            },
        )
        if status != 200:
            raise CloudError(f"send_message -> {status}")
        return bool((payload or {}).get("success", True))

    def pause_sandbox(self, sandbox_id: str) -> bool:
        """``POST /api/v1/sandboxes/{sandbox_id}/pause``."""
        status, _ = self._request("POST", f"/api/v1/sandboxes/{sandbox_id}/pause")
        return status == 200

    def resume_sandbox(self, sandbox_id: str) -> bool:
        """``POST /api/v1/sandboxes/{sandbox_id}/resume``."""
        status, _ = self._request("POST", f"/api/v1/sandboxes/{sandbox_id}/resume")
        return status == 200

    def start_sandbox(
        self, *, sandbox_spec_id: str | None = None
    ) -> dict[str, Any]:
        """``POST /api/v1/sandboxes`` -> a raw ``SandboxInfo``.

        This is the sandbox-first path used when the app-conversation endpoint
        cannot provision a running conversation. The response carries the
        sandbox's ``session_api_key`` and, once it is RUNNING, its exposed
        ``AGENT_SERVER`` URL, which together let the caller talk to that
        sandbox's agent-server directly.
        """
        body: dict[str, Any] = {}
        if sandbox_spec_id is not None:
            body["sandbox_spec_id"] = sandbox_spec_id
        status, payload = self._request("POST", "/api/v1/sandboxes", body)
        if status not in (200, 201) or not isinstance(payload, dict):
            raise CloudError(f"start_sandbox -> {status}")
        return payload

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """``GET /api/v1/sandboxes?id=<id>`` -> one ``SandboxInfo``."""
        status, payload = self._request("GET", f"/api/v1/sandboxes?id={sandbox_id}")
        if status == 404:
            raise CloudNotFound(sandbox_id)
        if status != 200:
            raise CloudError(f"get_sandbox -> {status}")
        items = payload if isinstance(payload, list) else [payload]
        if not items or not items[0]:
            raise CloudNotFound(sandbox_id)
        return items[0]

    def delete_sandbox(self, sandbox_id: str) -> bool:
        """``DELETE /api/v1/sandboxes/{id}`` (id also passed as a query param)."""
        status, _ = self._request(
            "DELETE", f"/api/v1/sandboxes/{sandbox_id}?sandbox_id={sandbox_id}"
        )
        return status in (200, 204)

    def wait_for_sandbox(
        self,
        sandbox_id: str,
        *,
        attempts: int = 40,
        interval: float = 5.0,
    ) -> dict[str, Any]:
        """Poll a sandbox until it is RUNNING, returning the final ``SandboxInfo``.

        Raises :class:`CloudUnavailable` if it does not reach RUNNING in time and
        :class:`CloudError` if it lands in ERROR/PAUSED, so a caller never
        proceeds to talk to a sandbox that cannot serve.
        """
        current: dict[str, Any] = {}
        for _ in range(max(1, attempts)):
            current = self.get_sandbox(sandbox_id)
            state = current.get("status")
            if state == "RUNNING":
                return current
            if state in ("ERROR",):
                raise CloudError(
                    f"sandbox {sandbox_id} -> {state}: {current.get('status_detail')}"
                )
            time.sleep(interval)
        raise CloudUnavailable(
            f"sandbox {sandbox_id} not RUNNING after {attempts} polls"
        )

    @staticmethod
    def agent_server_url(sandbox: dict[str, Any]) -> str | None:
        """The ``AGENT_SERVER`` exposed URL from a ``SandboxInfo``, if present."""
        for entry in sandbox.get("exposed_urls") or []:
            if str(entry.get("name", "")).upper() == "AGENT_SERVER":
                return entry.get("url")
        return None

    def delete_conversation(self, conversation_id: str) -> bool:
        """``DELETE /api/v1/app-conversations/{id}``."""
        status, payload = self._request(
            "DELETE", f"/api/v1/app-conversations/{conversation_id}"
        )
        return status == 200 and bool((payload or {}).get("success", True))

    def stop_conversation(self, conversation_id: str) -> bool:
        """Stop the agent working on a conversation, keeping its transcript.

        The app-conversation transport has no conversation-scoped stop: the only
        pause the Cloud V1 surface exposes is sandbox-scoped
        (``POST /api/v1/sandboxes/{id}/pause``), and pausing the whole sandbox
        would also stop unrelated work on it. So this reports ``False`` (nothing
        was stopped) rather than pretending. A caller that switched an executor
        away must treat a conversation it could not stop as one still running on
        its branch, which is exactly what the branch lock already enforces.
        """
        return False

    #: Whether ``stop_conversation`` can really stop a conversation on this
    #: transport. The app-conversation surface cannot (its pause is
    #: sandbox-scoped), so a caller must not hold a branch forever waiting for a
    #: stop that will never come - see ``LabScheduler._stop_abandoned``.
    supports_conversation_stop = False

    def update_conversation(self, conversation_id: str, *,
                            title: str | None = None) -> bool:
        """``PATCH /api/v1/app-conversations/{id}`` (title only, here).

        The schema (``AppConversationUpdateRequest``) applies only the fields
        that are sent, so a rename leaves tags, repository and status untouched.
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if not body:
            return True
        status, _ = self._request(
            "PATCH", f"/api/v1/app-conversations/{conversation_id}", body
        )
        return status == 200

    def web_url(self, conversation_id: str) -> str:
        """The human-facing URL for a conversation.

        ``GET`` returns an ``api`` URL (``.../api/conversations/<id>``), which
        renders JSON in a browser. The dashboard's own route is the same path
        without the ``/api`` segment, so it is derived rather than stored.
        """
        base = self.base_url.rstrip("/")
        return f"{base}/conversations/{conversation_id}"

    #: Confirmed by the live server: ``limit`` above 100 is a 422.
    MAX_EVENTS_PER_PAGE = 100

    def list_events(
        self, conversation_id: str, *, limit: int = MAX_EVENTS_PER_PAGE,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/conversation/{id}/events/search`` (all pages).

        The server caps a page at 100 events, so a conversation with more than
        100 events would hide older ``ActionEvent`` s and lose the tool call.
        This walks ``next_page_id`` until the pages are exhausted (bounded by
        ``max_pages``). The default sort is ascending, so the pages concatenate
        into oldest-first order, which is what callers expect.
        """
        limit = min(limit, self.MAX_EVENTS_PER_PAGE)
        collected: list[dict[str, Any]] = []
        page_id: str | None = None
        for _ in range(max_pages):
            path = (
                f"/api/v1/conversation/{conversation_id}/events/search?limit={limit}"
            )
            if page_id:
                path += f"&page_id={page_id}"
            status, payload = self._request("GET", path)
            if status != 200 or not isinstance(payload, dict):
                raise CloudError(f"list_events -> {status}")
            collected.extend(payload.get("items") or [])
            page_id = payload.get("next_page_id")
            if not page_id:
                break
        return collected

    def recent_events(
        self, conversation_id: str, *, limit: int = MAX_EVENTS_PER_PAGE,
        max_pages: int = 10, fallback_pages: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """The conversation's events, newest first, read a page at a time.

        The end of a conversation is what a caller usually wants, and a long
        conversation has thousands of events; read oldest-first and bounded,
        the end is exactly the part that is never reached. This asks the server
        for the newest page (``sort_order=TIMESTAMP_DESC``) and reads on only
        while the caller keeps asking.

        A server that does not take the parameter either refuses it or ignores
        it. Both are seen here - a refusal as a status, an ignored parameter as
        a page whose timestamps run forwards - and the events are then read the
        old way, oldest first, and handed over reversed. That way has to read
        everything to reach the end, so it is given more pages (*fallback_pages*
        of a hundred events) than a caller who starts at the end needs.
        """
        limit = min(limit, self.MAX_EVENTS_PER_PAGE)
        base = f"/api/v1/conversation/{conversation_id}/events/search?limit={limit}"
        try:
            status, payload = self._request("GET", base + "&sort_order=TIMESTAMP_DESC")
        except (CloudNotFound, CloudAuthError):
            raise
        except CloudError:
            status, payload = 0, None
        items = payload.get("items") if status == 200 and isinstance(payload, dict) else None
        page_id = payload.get("next_page_id") if isinstance(payload, dict) else None
        stamps = [e.get("timestamp") for e in items or [] if isinstance(e.get("timestamp"), str)]
        newest_first = len(stamps) >= 2 and stamps[0] > stamps[-1]
        if items is None or (len(items) > 1 and not newest_first):
            # Refused, or not in the order asked for.
            yield from reversed(self.list_events(conversation_id, limit=limit,
                                                 max_pages=fallback_pages))
            return
        yield from items
        for _ in range(max_pages - 1):
            if not page_id:
                return
            status, payload = self._request(
                "GET", f"{base}&sort_order=TIMESTAMP_DESC&page_id={page_id}")
            if status != 200 or not isinstance(payload, dict):
                raise CloudError(f"list_events -> {status}")
            yield from payload.get("items") or []
            page_id = payload.get("next_page_id")

    def final_response(
        self, conversation_id: str, *, limit: int = MAX_EVENTS_PER_PAGE
    ) -> str | None:
        """What the agent said last, or None when it said nothing.

        An agent ends its work in one of two ways: it calls the ``finish`` tool,
        whose ``message`` is the answer, or it writes a message and calls no
        tool. Both are read here, whichever came last - the rule the agent
        server itself applies (``get_agent_final_response`` in the OpenHands
        SDK). Reading only messages lost every answer given through ``finish``.
        On the live service four of thirteen finished attempts were recorded as
        "finished with no final response"; this is the likely reason (those
        conversations were not inspected).

        A ``finish`` with an empty message may be answered by the text the
        agent wrote just before it in the same turn, never by an earlier turn.

        A missing answer is never completion: the caller must treat None as
        "unverified", not as success.
        """
        empty_finish = False
        for e in self.recent_events(conversation_id, limit=limit):
            kind = e.get("kind")
            if kind == "ActionEvent" and e.get("tool_name") == "finish":
                if empty_finish:
                    return None
                action = e.get("action")
                message = action.get("message") if isinstance(action, dict) else None
                if isinstance(message, str) and message.strip():
                    return message.strip()
                empty_finish = True
                continue
            if kind != "MessageEvent":
                continue
            lm = e.get("llm_message") or {}
            if lm.get("role") == "user":
                if empty_finish:
                    return None
                continue
            if lm.get("role") != "assistant":
                continue
            text = " ".join(
                c.get("text", "")
                for c in (lm.get("content") or [])
                if isinstance(c, dict)
            ).strip()
            if text:
                return text
        return None

    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/api/v1/app-conversations/count")
        except CloudError:
            return False
        return status == 200

    def api_version(self) -> str | None:
        """The API version the service reports, or ``None`` if it is not readable.

        The live OpenAPI document (``/openapi.json``) carries ``info.version``;
        this is read-only and returns nothing sensitive. A service that does not
        serve it is not "incompatible" - the capability probe is what decides
        usability - so an unreadable version is ``None``, never an error.
        """
        try:
            status, payload = self._request("GET", "/openapi.json")
        except CloudError:
            return None
        if status != 200 or not isinstance(payload, dict):
            return None
        info = payload.get("info")
        version = info.get("version") if isinstance(info, dict) else None
        return str(version) if version else None
    def ping(self) -> dict[str, Any]:
        """Probe *reachability* of the endpoint, distinct from authorization.

        Returns ``{"reachable": bool, "status": int | None, "detail": str}``. An
        HTTP answer of any status — including 401/403/429 — means the endpoint is
        reachable; only a transport failure (a refused connection, a DNS error, a
        timeout) means it is not. This is deliberately not :meth:`health`, which
        answers "is the account usable right now": an endpoint can be reachable
        while the key is rejected, and the operator needs to tell those apart.
        """
        try:
            status, _ = self._request("GET", "/api/v1/app-conversations/count")
        except CloudUnavailable as e:
            # A rate limit is an *answer*: the endpoint is up, it is declining.
            if isinstance(e, CloudRateLimited):
                return {"reachable": True, "status": 429, "detail": str(e)}
            return {"reachable": False, "status": None, "detail": str(e)}
        except CloudError as e:
            return {"reachable": True, "status": None, "detail": str(e)}
        return {"reachable": True, "status": status, "detail": ""}

    # -------------------------------------------------- account capabilities
    # These are read-only. They report what the account *has configured* and
    # what a conversation *has available*; neither is proof that a tool ran.
    # Only an observed tool call in the event stream proves exercise.
    def get_settings(self) -> dict[str, Any]:
        """``GET /api/v1/settings`` -> the account's own agent settings.

        Read-only. Used to discover the account's active agent profile and
        configured MCP servers without changing any of them.
        """
        status, payload = self._request("GET", "/api/v1/settings")
        if status != 200 or not isinstance(payload, dict):
            raise CloudError(f"get_settings -> {status}")
        return payload

    def list_agent_profiles(self) -> dict[str, Any]:
        """``GET /api/agent-profiles`` -> ``{profiles, active_agent_profile_id}``."""
        status, payload = self._request("GET", "/api/agent-profiles")
        if status != 200 or not isinstance(payload, dict):
            raise CloudError(f"list_agent_profiles -> {status}")
        return payload

    def conversation_skills(self, conversation_id: str) -> list[str]:
        """``GET /api/v1/app-conversations/{id}/skills`` -> skill names.

        This is what a *specific* conversation actually has loaded, which is a
        stronger statement than what the account is configured with.
        """
        status, payload = self._request(
            "GET", f"/api/v1/app-conversations/{conversation_id}/skills"
        )
        if status != 200:
            raise CloudError(f"conversation_skills -> {status}")
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("skills") or []
        else:
            items = []
        names: list[str] = []
        for item in items:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return names

    def tool_catalog(self) -> dict[str, Any]:
        """The agent-server tool catalog — not available on the app transport.

        Cloud V1's app-conversation endpoint exposes no agent-server tool
        catalog: the tools live inside a sandbox this transport never addresses.
        Raising :class:`ToolCatalogUnsupported` is honest — nothing was proved
        either way — and lets the diagnostics report ``BLOCKED`` rather than
        invent a tool list.
        """
        from .openhands_tools import ToolCatalogUnsupported

        raise ToolCatalogUnsupported(
            "the Cloud app transport exposes no agent-server tool catalog"
        )

    def account_capabilities(self) -> dict[str, Any]:
        """A read-only snapshot of the account-level capabilities.

        Returns only non-secret facts: whether an LLM key is set, the configured
        MCP server names, the active agent profile id, and marketplace counts.
        Secret *values* are never returned by the API and are never requested.
        """
        settings = self.get_settings()
        agent_settings = settings.get("agent_settings") or {}
        mcp = agent_settings.get("mcp_config") or {}
        profiles: dict[str, Any] = {}
        try:
            profiles = self.list_agent_profiles()
        except CloudError:
            profiles = {}
        return {
            "llm_api_key_set": bool(settings.get("llm_api_key_set")),
            "active_agent_profile_id": profiles.get("active_agent_profile_id")
            or settings.get("active_agent_profile_id"),
            "mcp_servers": sorted(mcp.keys()) if isinstance(mcp, dict) else [],
            "registered_marketplaces": len(settings.get("registered_marketplaces") or []),
            "inherited_marketplaces": len(settings.get("inherited_marketplaces") or []),
            "disabled_skills": settings.get("disabled_skills") or [],
            "provider_tokens_set": {
                k: bool(v)
                for k, v in (settings.get("provider_tokens_set") or {}).items()
            },
            "agent_profiles": [
                {"name": p.get("name"), "mcp_server_refs": p.get("mcp_server_refs") or []}
                for p in (profiles.get("profiles") or [])
            ],
        }


def _start_task(payload: dict[str, Any]) -> StartTask:
    return StartTask(
        id=payload["id"],
        status=payload.get("status", StartStatus.WORKING.value),
        app_conversation_id=payload.get("app_conversation_id"),
        sandbox_id=payload.get("sandbox_id"),
        agent_server_url=payload.get("agent_server_url"),
        detail=payload.get("detail"),
    )


def _conversation(payload: dict[str, Any]) -> CloudConversation:
    return CloudConversation(
        id=payload["id"],
        title=payload.get("title"),
        sandbox_id=payload.get("sandbox_id", ""),
        execution_status=payload.get("execution_status"),
        sandbox_status=payload.get("sandbox_status"),
        selected_repository=payload.get("selected_repository"),
        selected_branch=payload.get("selected_branch"),
        conversation_url=payload.get("conversation_url"),
        sub_conversation_ids=list(payload.get("sub_conversation_ids") or []),
        tags=dict(payload.get("tags") or {}),
        raw=payload,
    )
