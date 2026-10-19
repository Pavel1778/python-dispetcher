"""OpenHands Cloud V1 API client.

Endpoints used (all confirmed against the live OpenAPI spec at
``https://app.all-hands.dev/openapi.json``):

* ``POST /api/v1/app-conversations``            -> start a conversation (async)
* ``GET  /api/v1/app-conversations/start-tasks`` -> batch fetch start tasks
* ``GET  /api/v1/app-conversations``             -> batch fetch conversations
* ``GET  /api/v1/app-conversations/search``      -> search conversations
* ``GET  /api/v1/conversation/{id}/events``      -> conversation events

The start endpoint is asynchronous: it returns a ``start_task`` whose
``app_conversation_id`` is ``null`` until the sandbox/conversation is ready.
Callers must persist the start-task id and the conversation id separately.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

from .logging_config import get_logger, safe_extra
from .status import START_TASK_ERROR, START_TASK_READY

logger = get_logger(__name__)


class OpenHandsError(Exception):
    """Base class for OpenHands Cloud client errors."""


class OpenHandsAuthError(OpenHandsError):
    """Authentication or authorisation failure (401/403). Not retryable."""


class OpenHandsRateLimitError(OpenHandsError):
    """Rate limit or server-side throttling (429). Retryable."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OpenHandsTransientError(OpenHandsError):
    """Network failure, timeout, or 5xx. Retryable."""


class OpenHandsRequestError(OpenHandsError):
    """Non-retryable 4xx client error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class StartTask:
    """Parsed view of an ``AppConversationStartTask``.

    Only whitelisted, non-sensitive fields are retained.
    """

    id: str
    status: str
    app_conversation_id: str | None
    sandbox_id: str | None
    detail: str | None
    created_at: str | None
    updated_at: str | None
    raw_status: str = ""

    @property
    def is_ready(self) -> bool:
        return self.status == START_TASK_READY

    @property
    def is_error(self) -> bool:
        return self.status == START_TASK_ERROR

    @property
    def is_terminal(self) -> bool:
        return self.is_ready or self.is_error


@dataclass(frozen=True)
class Conversation:
    """Parsed view of an ``AppConversation`` (whitelisted fields only)."""

    id: str
    sandbox_id: str | None
    title: str | None
    sandbox_status: str | None
    execution_status: str | None
    parent_conversation_id: str | None
    sub_conversation_ids: list[str] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


def _parse_start_task(data: Mapping[str, Any]) -> StartTask:
    return StartTask(
        id=str(data.get("id", "")),
        status=str(data.get("status", "")),
        app_conversation_id=data.get("app_conversation_id"),
        sandbox_id=data.get("sandbox_id"),
        detail=data.get("detail"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        raw_status=str(data.get("status", "")),
    )


def _parse_conversation(data: Mapping[str, Any]) -> Conversation:
    return Conversation(
        id=str(data.get("id", "")),
        sandbox_id=data.get("sandbox_id"),
        title=data.get("title"),
        sandbox_status=data.get("sandbox_status"),
        execution_status=data.get("execution_status"),
        parent_conversation_id=data.get("parent_conversation_id"),
        sub_conversation_ids=list(data.get("sub_conversation_ids") or []),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


class OpenHandsClient:
    """Async client with bounded retries, backoff, and jitter.

    The client never logs request headers, response bodies, or the API key.
    """

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://app.all-hands.dev",
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep or asyncio.sleep
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    # --- lifecycle ---

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenHandsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- internals ---

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise OpenHandsAuthError("OpenHands API key is not configured")
        return {"Authorization": f"Bearer {self._api_key}"}

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self._backoff_max)
        delay = self._backoff_base * (2 ** attempt)
        delay = min(delay, self._backoff_max)
        return delay + random.uniform(0, delay * 0.1)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, params=params, json=json_body, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = OpenHandsTransientError(type(exc).__name__)
                logger.warning(
                    "openhands request transport error",
                    **safe_extra(method=method, path=path, attempt=attempt, error=type(exc).__name__),
                )
                if attempt >= self._max_retries:
                    raise last_error from exc
                await self._sleep(self._backoff_delay(attempt, None))
                continue

            status = response.status_code
            if status == 401 or status == 403:
                raise OpenHandsAuthError(f"authentication failed (HTTP {status})")
            if status == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_error = OpenHandsRateLimitError("rate limited (HTTP 429)", retry_after)
                logger.warning(
                    "openhands rate limited",
                    **safe_extra(method=method, path=path, attempt=attempt),
                )
                if attempt >= self._max_retries:
                    raise last_error
                await self._sleep(self._backoff_delay(attempt, retry_after))
                continue
            if 500 <= status < 600:
                last_error = OpenHandsTransientError(f"server error (HTTP {status})")
                logger.warning(
                    "openhands server error",
                    **safe_extra(method=method, path=path, status=status, attempt=attempt),
                )
                if attempt >= self._max_retries:
                    raise last_error
                await self._sleep(self._backoff_delay(attempt, None))
                continue
            if 400 <= status < 500:
                # Body may echo request data; log only the status code.
                raise OpenHandsRequestError(f"client error (HTTP {status})", status_code=status)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise OpenHandsTransientError("invalid JSON in response") from exc

        raise last_error or OpenHandsError("request failed")  # pragma: no cover

    # --- public API ---

    async def get_current_user(self) -> dict[str, Any]:
        """GET /api/v1/users/me.

        Returns a *sanitised* subset. The raw response can contain
        ``sandbox_api_key`` and similar secrets, so it is never returned whole.
        """
        data = await self._request("GET", "/api/v1/users/me")
        if not isinstance(data, Mapping):
            return {}
        allowed = {"id", "email", "email_verified", "role", "org_id", "org_name"}
        return {k: data.get(k) for k in allowed if k in data}

    async def start_conversation(
        self,
        *,
        title: str | None = None,
        initial_message: str | None = None,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        parent_conversation_id: str | None = None,
        tags: Mapping[str, str] | None = None,
        agent_type: str = "default",
        extra_body: Mapping[str, Any] | None = None,
    ) -> StartTask:
        """POST /api/v1/app-conversations. Returns the async start task."""
        body: dict[str, Any] = {"agent_type": agent_type}
        if title is not None:
            body["title"] = title
        if initial_message is not None:
            # The API requires ``content`` to be a list of content blocks, not a
            # bare string: a string is rejected with HTTP 422 (``list_type``).
            body["initial_message"] = {
                "role": "user",
                "content": [{"type": "text", "text": initial_message}],
            }
        if selected_repository is not None:
            body["selected_repository"] = selected_repository
        if selected_branch is not None:
            body["selected_branch"] = selected_branch
        if parent_conversation_id is not None:
            body["parent_conversation_id"] = parent_conversation_id
        if tags is not None:
            body["tags"] = dict(tags)
        if extra_body:
            body.update(extra_body)

        data = await self._request("POST", "/api/v1/app-conversations", json_body=body)
        if not isinstance(data, Mapping):
            raise OpenHandsTransientError("unexpected start-conversation response")
        task = _parse_start_task(data)
        logger.info(
            "started conversation request",
            **safe_extra(start_task_id=task.id, status=task.status),
        )
        return task

    async def get_start_tasks(self, ids: Iterable[str]) -> list[StartTask]:
        """GET /api/v1/app-conversations/start-tasks (batch, by ids)."""
        id_list = [i for i in ids if i]
        if not id_list:
            return []
        data = await self._request(
            "GET",
            "/api/v1/app-conversations/start-tasks",
            params={"ids": id_list},
        )
        if not isinstance(data, list):
            raise OpenHandsTransientError("unexpected start-tasks response")
        return [_parse_start_task(item) for item in data if isinstance(item, Mapping)]

    async def get_start_task(self, start_task_id: str) -> StartTask | None:
        tasks = await self.get_start_tasks([start_task_id])
        return tasks[0] if tasks else None

    async def search_start_tasks_by_conversation(
        self, conversation_id: str, limit: int = 20
    ) -> list[StartTask]:
        """GET /api/v1/app-conversations/start-tasks/search (by conversation id)."""
        data = await self._request(
            "GET",
            "/api/v1/app-conversations/start-tasks/search",
            params={"conversation_id__eq": conversation_id, "limit": limit},
        )
        items = _page_items(data)
        return [_parse_start_task(item) for item in items]

    async def get_conversations(self, ids: Iterable[str]) -> list[Conversation]:
        """GET /api/v1/app-conversations (batch, by ids)."""
        id_list = [i for i in ids if i]
        if not id_list:
            return []
        data = await self._request(
            "GET", "/api/v1/app-conversations", params={"ids": id_list}
        )
        if not isinstance(data, list):
            raise OpenHandsTransientError("unexpected conversations response")
        return [_parse_conversation(item) for item in data if isinstance(item, Mapping)]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        convs = await self.get_conversations([conversation_id])
        return convs[0] if convs else None

    async def search_conversations(
        self,
        *,
        parent_conversation_id: str | None = None,
        title_contains: str | None = None,
        limit: int = 20,
    ) -> list[Conversation]:
        """GET /api/v1/app-conversations/search.

        Note: the spec does not expose a ``parent_conversation_id`` filter, so
        filtering by parent is done client-side when requested.
        """
        params: dict[str, Any] = {"limit": limit, "include_sub_conversations": True}
        if title_contains is not None:
            params["title__contains"] = title_contains
        data = await self._request("GET", "/api/v1/app-conversations/search", params=params)
        items = [_parse_conversation(i) for i in _page_items(data)]
        if parent_conversation_id is not None:
            items = [c for c in items if c.parent_conversation_id == parent_conversation_id]
        return items

    async def get_events(self, conversation_id: str, ids: Iterable[str]) -> list[dict[str, Any]]:
        """GET /api/v1/conversation/{id}/events (batch, by event ids)."""
        id_list = [i for i in ids if i]
        if not id_list:
            return []
        data = await self._request(
            "GET",
            f"/api/v1/conversation/{conversation_id}/events",
            params={"id": id_list},
        )
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []

    async def get_conversation_result(self, conversation_id: str) -> str | None:
        """Return the last agent message text, or ``None`` if there is none.

        Used to distinguish a conversation that genuinely produced a result from
        one that ended without output. An empty return must never be treated as
        success by the caller.

        Uses ``GET /api/v1/conversation/{id}/events/search`` (newest first) and
        keeps only ``MessageEvent`` records emitted by the agent.
        """
        data = await self._request(
            "GET",
            f"/api/v1/conversation/{conversation_id}/events/search",
            params={
                "kind__eq": "MessageEvent",
                "sort_order": "TIMESTAMP_DESC",
                "limit": 20,
            },
        )
        for item in _page_items(data):
            if item.get("kind") != "MessageEvent":
                continue
            if item.get("source") != "agent":
                continue
            text = _message_text(item.get("llm_message"))
            if text:
                return text
        return None


def _message_text(message: Any) -> str | None:
    """Extract concatenated text from an ``llm_message`` payload.

    ``Message.content`` is a list of ``TextContent``/``ImageContent`` blocks, so
    only ``text`` fields are joined; everything else is ignored.
    """
    if not isinstance(message, Mapping):
        return None
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    joined = "\n".join(parts).strip()
    return joined or None


def _page_items(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, Mapping):
        items = data.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, Mapping)]
    if isinstance(data, list):
        return [i for i in data if isinstance(i, Mapping)]
    return []


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
