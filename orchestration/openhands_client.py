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
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ApiUnavailable(RuntimeError):
    """The runtime could not be reached, or answered with a server error.

    Callers must treat this as transient: the task stays leased and is retried.
    """


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
    ):
        self.base_url = base_url.rstrip("/")
        # Resolved from the environment when not passed explicitly. Never stored
        # in a log line or an exception message.
        self._api_key = api_key or os.environ.get("SESSION_API_KEY") or ""
        self.timeout = timeout

    # ------------------------------------------------------------------ http
    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._api_key:
            req.add_header("X-Session-API-Key", self._api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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

    def agent_settings(self) -> dict[str, Any]:
        """Return the server's configured ``agent_settings``.

        Read from the runtime so the dispatcher never has to hold or forward an
        API key itself; whatever the server already trusts is reused as-is.
        """
        status, payload = self._request("GET", "/api/settings")
        if status != 200:
            raise ApiUnavailable(f"agent_settings -> {status}")
        settings = (payload or {}).get("agent_settings")
        if not isinstance(settings, dict):
            raise ApiUnavailable("server returned no agent_settings")
        return settings

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
