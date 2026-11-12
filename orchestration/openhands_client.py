"""Adapter for the OpenHands agent-server REST API (v1.50.1).

Only endpoints confirmed against the live ``/openapi.json`` of build
``1e1390a`` are used here; see ``INTEGRATION-MAP.md``. The adapter is
deliberately small: it translates HTTP into typed outcomes and never decides
what a task's state should become. That decision belongs to the dispatcher.

Design rules enforced by this module:

* the API key is read from the environment and never logged or echoed;
* a network error is never reported as success;
* ``run`` returning 409 means "already running" and is treated as success;
* a missing final response is ``None``, never an empty "done".
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .nexus_policy import UnsafeTarget, check_outbound_url


#: Scheme default ports, so an omitted port and an explicit default compare equal.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int]:
    """Return ``(scheme, hostname, port)`` with the default port filled in.

    Used to decide whether a redirect stays on the same origin. A missing port
    and an explicit default port are the same origin, so both normalise to the
    default number rather than to ``None``. An unparsable port yields ``-1``,
    which can never equal a real origin, so it is refused rather than trusted.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return scheme, (parts.hostname or "").lower(), -1
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, -1)
    return scheme, (parts.hostname or "").lower(), port


class ApiUnavailable(RuntimeError):
    """The runtime could not be reached, or answered with a server error.

    Callers must treat this as transient: the task stays leased and is retried.
    """


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while scheme, host and port all stay identical.

    The session key is sent as a header, and ``urlopen`` re-sends headers to
    whatever host a ``3xx`` names, so a cross-origin redirect leaks the key.
    Checking the hostname alone is not enough: a downgrade from https to http,
    or a move to another port, is still a different destination and is refused.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise ApiUnavailable(
                f"refusing cross-origin redirect ({code}): "
                f"{_origin(req.full_url)} -> {_origin(newurl)}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ConversationGone(RuntimeError):
    """The runtime answered 404: the conversation does not exist any more."""


class ConversationBlocked(RuntimeError):
    """The conversation is waiting for a human decision."""


class Outcome(str, Enum):
    """What the runtime says about a conversation right now."""

    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    BLOCKED = "blocked"
    IDLE = "idle"


#: Live ``execution_status`` values mapped to adapter outcomes.
_STATUS_MAP: dict[str, Outcome] = {
    "idle": Outcome.IDLE,
    "running": Outcome.RUNNING,
    "paused": Outcome.IDLE,
    "waiting_for_confirmation": Outcome.BLOCKED,
    "finished": Outcome.FINISHED,
    "error": Outcome.FAILED,
    "stuck": Outcome.FAILED,
    "deleting": Outcome.IDLE,
}


@dataclass(frozen=True)
class ConversationView:
    conversation_id: str
    outcome: Outcome
    raw_status: str
    title: str | None = None


@dataclass(frozen=True)
class SendOutcome:
    """Result of asking a conversation to run."""

    started: bool
    already_running: bool = False


def task_conversation_id(task_id: str, *, namespace: str = "openhands-orchestrator") -> str:
    """Derive a stable conversation id from a task id.

    The server makes creation idempotent when the caller supplies
    ``conversation_id``, so a deterministic id is what stops a dispatcher
    restart from creating a second conversation for the same task.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{task_id}"))


class OpenHandsClient:
    """Minimal REST client. No external dependencies, no hidden retries."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        address_policy: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # A sandbox/agent-server base URL is operator-supplied, so it obeys the
        # SSRF policy: a loopback or private target is refused unless the
        # operator explicitly permits private targets (the local runtime and the
        # tests do; a Cloud deployment does not). An empty base URL is the
        # documented placeholder the direct transport constructs and then points
        # at a sandbox, so it is not a target yet.
        if self.base_url:
            try:
                check_outbound_url(self.base_url)
            except UnsafeTarget as e:
                raise ApiUnavailable(f"refusing agent-server base URL: {e}") from e
        # Resolved from the environment when not passed explicitly. Never stored
        # in a log line or an exception message.
        self._api_key = api_key or os.environ.get("SESSION_API_KEY") or ""
        self.timeout = timeout
        #: When set, every socket is opened only where the policy allows
        #: (``net_guard``). The URLs this client is sent to come from the
        #: service it talks to; for a connection a person added, that service
        #: is not trusted to name an address inside this server's network.
        self.address_policy = address_policy

    def _opener(self) -> urllib.request.OpenerDirector:
        """An opener that sends the session key only to the configured origin.

        ``urlopen`` follows a ``3xx`` and re-sends every request header, so a
        redirect to another host would hand ``X-Session-API-Key`` to that host.
        A same-origin redirect is still followed, because that keeps the key on
        the host it was meant for.
        """
        if self.address_policy is not None:
            from .net_guard import guarded_handlers

            return urllib.request.build_opener(_SameOriginRedirect,
                                               *guarded_handlers(self.address_policy))
        return urllib.request.build_opener(_SameOriginRedirect)

    # ------------------------------------------------------------------ http
    def _base_url(self) -> str:
        """The server root for requests. Overridable for lazily-resolved hosts."""
        return self.base_url

    def _auth_key(self) -> str:
        """The API key for requests. Overridable for lazily-resolved keys."""
        return self._api_key

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        return self._request_to(self._base_url(), self._auth_key(), method, path, body)

    def _request_to(
        self,
        base_url: str,
        key: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Issue a request against an explicit host and key.

        The default ``_request`` uses the client's own host; adapters that route
        different conversations to different sandboxes call this directly.
        """
        url = f"{base_url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Session-API-Key", key)
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            # A 4xx is the server's answer, not a transport failure: read it and
            # let the caller classify. Only 5xx is treated as unavailability.
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = None
            if e.code >= 500:
                raise ApiUnavailable(f"{method} {path} -> {e.code}") from e
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiUnavailable(f"{method} {path}: {type(e).__name__}") from e

    def _upload_to(self, base_url: str, key: str, path: str, data: bytes,
                   *, filename: str = "file") -> tuple[int, Any]:
        """``POST /api/file/upload?path=<absolute path>`` with *data* as the file.

        The agent-server's own file route: it writes the bytes to *path* inside
        the sandbox, creating the directories above it. Returns the status and
        the decoded body for **every** HTTP answer, 5xx included: the server
        reports a path it cannot write to as a 500, and the caller has to tell
        that from a sandbox that is unreachable (:class:`ApiUnavailable`).
        """
        boundary = "nexus-" + uuid.uuid4().hex
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "file"
        body = b"".join((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ))
        url = (f"{base_url.rstrip('/')}/api/file/upload?"
               + urllib.parse.urlencode({"path": path}))
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(body)))
        if key:
            req.add_header("X-Session-API-Key", key)
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(64_000), e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiUnavailable(f"POST /api/file/upload: {type(e).__name__}") from e
        try:
            return status, (json.loads(raw) if raw else None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return status, None

    def _download_from(self, base_url: str, key: str, path: str,
                       *, limit: int) -> tuple[int, bytes]:
        """``GET /api/file/download?path=<absolute path>`` -> the file's bytes.

        At most *limit* + 1 bytes are read, so a caller comparing against an
        expected size sees "too long" without buffering whatever is there.
        """
        url = (f"{base_url.rstrip('/')}/api/file/download?"
               + urllib.parse.urlencode({"path": path}))
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/octet-stream")
        if key:
            req.add_header("X-Session-API-Key", key)
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(limit + 1)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiUnavailable(f"GET /api/file/download: {type(e).__name__}") from e

    # ----------------------------------------------------------- operations
    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/health")
        except ApiUnavailable:
            return False
        return status == 200

    def create_conversation(
        self,
        *,
        conversation_id: str,
        working_dir: str,
        title: str | None = None,
        max_iterations: int | None = None,
        agent_settings: dict[str, Any] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
    ) -> tuple[ConversationView, bool]:
        """Create a conversation, or return the existing one.

        Returns ``(view, created)``. ``created`` is False when the server
        answered 200 for an id that already existed, which is the signal that a
        previous attempt already did this work.

        ``agent_settings`` is required by the live server: it rejects a create
        with 422 unless one of ``agent``, ``agent_settings`` or
        ``agent_profile_id`` is present. Callers pass what
        :meth:`agent_settings` returned; the value is forwarded opaquely and
        never logged.

        ``client_tools`` is the documented ``ClientToolSpec`` list. When it is
        present the runtime injects those tools into the agent and emits an
        ``ActionEvent`` (``action.kind == "ClientAction_<name>"``) when the agent
        calls one. This is the mechanism the Sr bridge relies on and it is
        available on the local agent-server v1.50.1, unlike the Cloud V1 app
        endpoint which has no such field on the app-server request.
        """
        body: dict[str, Any] = {
            "conversation_id": conversation_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": working_dir},
        }
        if title is not None:
            body["title"] = title
        if max_iterations is not None:
            body["max_iterations"] = max_iterations
        if agent_settings is not None:
            body["agent_settings"] = agent_settings
        if client_tools is not None:
            body["client_tools"] = client_tools
        status, payload = self._request("POST", "/api/conversations", body)
        if status == 429:
            raise ApiUnavailable("conversation capacity full")
        if status == 422:
            # A validation error is a programming error, not a transient one.
            raise ValueError(
                "create_conversation rejected: "
                + json.dumps(payload)[:300]
            )
        if status not in (200, 201):
            raise ApiUnavailable(f"create_conversation -> {status}")
        return self._view(payload), status == 201

    def settings(self) -> dict[str, Any]:
        """Return the server's full settings document (``GET /api/settings``)."""
        status, payload = self._request("GET", "/api/settings")
        if status != 200:
            raise ApiUnavailable(f"settings -> {status}")
        if not isinstance(payload, dict):
            raise ApiUnavailable("server returned no settings")
        return payload

    def agent_settings(self) -> dict[str, Any]:
        """Return the server's configured ``agent_settings``.

        Read from the runtime so the dispatcher never has to hold or forward an
        API key itself; whatever the server already trusts is reused as-is.
        """
        settings = self.settings().get("agent_settings")
        if not isinstance(settings, dict):
            raise ApiUnavailable("server returned no agent_settings")
        return settings

    def patch_settings(self, diff: dict[str, Any]) -> dict[str, Any]:
        """Merge a settings diff (``PATCH /api/settings``) and return the result.

        The endpoint merges ``agent_settings_diff`` field by field, so a patch
        that sets only ``agent_context`` leaves every other field alone. A
        rejected body (a diff key the server does not accept) comes back as a
        4xx, which this raises as a programming error rather than retrying.
        """
        status, payload = self._request("PATCH", "/api/settings", diff)
        if status == 404:
            raise ApiUnavailable("PATCH /api/settings -> 404 (unsupported)")
        if status >= 400:
            raise ValueError(
                f"PATCH /api/settings rejected: {status} {json.dumps(payload)[:200]}"
            )
        if not isinstance(payload, dict):
            raise ApiUnavailable("PATCH /api/settings returned no body")
        return payload

    def send_task(
        self,
        conversation_id: str,
        text: str,
        *,
        run: bool = True,
    ) -> SendOutcome:
        """Send the task text and optionally start the agent loop."""
        body = {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "run": run,
        }
        status, _ = self._request(
            "POST", f"/api/conversations/{conversation_id}/events", body
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status == 429:
            raise ApiUnavailable("conversation capacity full")
        if status not in (200, 201):
            raise ApiUnavailable(f"send_task -> {status}")
        return SendOutcome(started=run)

    def start(self, conversation_id: str) -> SendOutcome:
        """Start the loop without sending a message."""
        status, _ = self._request(
            "POST", f"/api/conversations/{conversation_id}/run"
        )
        if status == 409:
            # Confirmed: means the conversation is already running. Not an error.
            return SendOutcome(started=False, already_running=True)
        if status == 404:
            raise ConversationGone(conversation_id)
        if status == 429:
            raise ApiUnavailable("conversation capacity full")
        if status not in (200, 201):
            raise ApiUnavailable(f"start -> {status}")
        return SendOutcome(started=True)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        status, payload = self._request(
            "GET", f"/api/conversations/{conversation_id}"
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status != 200:
            raise ApiUnavailable(f"get_conversation -> {status}")
        return self._view(payload)

    def final_response(self, conversation_id: str) -> str | None:
        """Return the agent's final text, or None when there is none yet.

        An empty response is not evidence of success, so it is reported as
        None rather than an empty string the caller might mistake for a result.
        """
        status, payload = self._request(
            "GET", f"/api/conversations/{conversation_id}/agent_final_response"
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status != 200:
            raise ApiUnavailable(f"final_response -> {status}")
        text = (payload or {}).get("response") or ""
        return text.strip() or None

    def event_count(self, conversation_id: str) -> int:
        """Number of events in the conversation. Used to confirm delivery."""
        status, payload = self._request(
            "GET", f"/api/conversations/{conversation_id}/events/count"
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status != 200:
            raise ApiUnavailable(f"event_count -> {status}")
        if isinstance(payload, dict):
            return int(payload.get("count", 0))
        return int(payload or 0)

    def failure_classification(self, conversation_id: str) -> dict[str, Any] | None:
        """Return the runtime's own classification of the last error, if any.

        The runtime emits ``ConversationErrorEvent`` with a ``classification``
        object carrying ``kind``, ``retryable`` and ``user_action``. Retrying an
        error the runtime marked ``retryable: false`` only burns the task's
        attempt budget, so the dispatcher reads this before deciding.

        The ``kind`` query parameter does not match the event class name on
        v1.50.1 (it returns nothing for ``kind=ConversationErrorEvent``), so the
        events are fetched unfiltered and matched here instead.

        Returns None when there is no error event to classify.
        """
        status, payload = self._request(
            "GET",
            f"/api/conversations/{conversation_id}/events/search"
            "?limit=50&sort_order=TIMESTAMP_DESC",
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status != 200:
            raise ApiUnavailable(f"failure_classification -> {status}")
        items = (payload or {}).get("items") or []
        for event in items:
            classification = event.get("classification")
            if isinstance(classification, dict):
                return classification
        return None

    def pause(self, conversation_id: str) -> None:
        status, _ = self._request(
            "POST", f"/api/conversations/{conversation_id}/pause"
        )
        if status == 404:
            raise ConversationGone(conversation_id)
        if status not in (200, 201):
            raise ApiUnavailable(f"pause -> {status}")

    # ----------------------------------------------------------------- utils
    @staticmethod
    def _view(payload: dict[str, Any]) -> ConversationView:
        raw = str((payload or {}).get("execution_status", "idle"))
        return ConversationView(
            conversation_id=str((payload or {}).get("id", "")),
            outcome=_STATUS_MAP.get(raw, Outcome.IDLE),
            raw_status=raw,
            title=(payload or {}).get("title"),
        )
