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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_CLOUD_BASE_URL = "https://app.all-hands.dev"


class CloudError(RuntimeError):
    """Base class for Cloud API failures."""


class CloudUnavailable(CloudError):
    """Transport failure or 5xx. Transient: retry with backoff."""


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


@dataclass(frozen=True)
class StartTask:
    """A ``AppConversationStartTask``."""

    id: str
    status: str
    app_conversation_id: str | None = None
    sandbox_id: str | None = None
    agent_server_url: str | None = None
    detail: str | None = None

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

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = (
            base_url or os.environ.get("OPENHANDS_CLOUD_URL") or DEFAULT_CLOUD_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OPENHANDS_API_KEY") or ""
        self.timeout = timeout

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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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
                raise CloudUnavailable(
                    f"{method} {path} -> 429: {json.dumps(payload)[:200]}"
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

    def delete_conversation(self, conversation_id: str) -> bool:
        """``DELETE /api/v1/app-conversations/{id}``."""
        status, payload = self._request(
            "DELETE", f"/api/v1/app-conversations/{conversation_id}"
        )
        return status == 200 and bool((payload or {}).get("success", True))

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

    def final_response(
        self, conversation_id: str, *, limit: int = MAX_EVENTS_PER_PAGE
    ) -> str | None:
        """Last non-empty assistant text, or None when there is none.

        A missing answer is never completion: the caller must treat None as
        "unverified", not as success.
        """
        events = self.list_events(conversation_id, limit=limit)
        for e in reversed(events):
            if e.get("kind") != "MessageEvent":
                continue
            lm = e.get("llm_message") or {}
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
