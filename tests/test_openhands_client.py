"""OpenHands Cloud client: success, error, retry, and redaction behaviour."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.openhands_client import (
    OpenHandsAuthError,
    OpenHandsClient,
    OpenHandsRateLimitError,
    OpenHandsRequestError,
    OpenHandsTransientError,
)

BASE = "https://app.all-hands.dev"


def make_client(**kwargs) -> OpenHandsClient:
    kwargs.setdefault("sleep", _no_sleep)
    return OpenHandsClient("sk-test-not-real", base_url=BASE, **kwargs)


async def _no_sleep(_seconds: float) -> None:  # pragma: no cover - trivial
    return None


@respx.mock
async def test_get_current_user_redacts_secret_fields():
    respx.get(f"{BASE}/api/v1/users/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "user-1",
                "email": "a@b.c",
                "sandbox_api_key": "super-secret-value",
                "search_api_key": "another-secret",
            },
        )
    )
    client = make_client()
    try:
        user = await client.get_current_user()
    finally:
        await client.aclose()
    assert user["id"] == "user-1"
    # Secrets present in the raw payload must not be surfaced.
    assert "sandbox_api_key" not in user
    assert "search_api_key" not in user


@respx.mock
async def test_start_conversation_parses_async_start_task():
    respx.post(f"{BASE}/api/v1/app-conversations").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "start-123",
                "status": "WORKING",
                "app_conversation_id": None,
                "sandbox_id": None,
                "detail": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "request": {},
            },
        )
    )
    client = make_client()
    try:
        start = await client.start_conversation(title="t", initial_message="do work")
    finally:
        await client.aclose()
    assert start.id == "start-123"
    assert start.status == "WORKING"
    assert start.app_conversation_id is None
    assert not start.is_terminal


@respx.mock
async def test_start_conversation_sends_content_as_block_list():
    """``initial_message.content`` must be a list of blocks, not a bare string.

    The live API rejects a string with HTTP 422 (``list_type``), which would make
    every dispatch fail before a conversation is even created.
    """
    route = respx.post(f"{BASE}/api/v1/app-conversations").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "start-124",
                "status": "WORKING",
                "app_conversation_id": None,
                "sandbox_id": None,
                "detail": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "request": {},
            },
        )
    )
    client = make_client()
    try:
        await client.start_conversation(title="t", initial_message="do work")
    finally:
        await client.aclose()

    sent = json.loads(route.calls[0].request.content)
    assert sent["initial_message"] == {
        "role": "user",
        "content": [{"type": "text", "text": "do work"}],
    }


@respx.mock
async def test_auth_error_is_not_retried():
    route = respx.get(f"{BASE}/api/v1/users/me").mock(
        return_value=httpx.Response(401, json={"detail": "bad token"})
    )
    client = make_client(max_retries=3)
    try:
        with pytest.raises(OpenHandsAuthError):
            await client.get_current_user()
    finally:
        await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_rate_limit_retries_then_succeeds():
    route = respx.get(f"{BASE}/api/v1/app-conversations/start-tasks").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow down"}),
            httpx.Response(200, json=[{"id": "s1", "status": "READY", "app_conversation_id": "c1"}]),
        ]
    )
    client = make_client(max_retries=3)
    try:
        tasks = await client.get_start_tasks(["s1"])
    finally:
        await client.aclose()
    assert route.call_count == 2
    assert tasks[0].is_ready
    assert tasks[0].app_conversation_id == "c1"


@respx.mock
async def test_rate_limit_exhausts_retries():
    respx.get(f"{BASE}/api/v1/users/me").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={})
    )
    client = make_client(max_retries=2)
    try:
        with pytest.raises(OpenHandsRateLimitError):
            await client.get_current_user()
    finally:
        await client.aclose()


@respx.mock
async def test_transient_timeout_retries_then_raises():
    respx.get(f"{BASE}/api/v1/users/me").mock(side_effect=httpx.ConnectTimeout("boom"))
    client = make_client(max_retries=1)
    try:
        with pytest.raises(OpenHandsTransientError):
            await client.get_current_user()
    finally:
        await client.aclose()


@respx.mock
async def test_server_error_retries_then_raises():
    respx.get(f"{BASE}/api/v1/users/me").mock(return_value=httpx.Response(503, json={}))
    client = make_client(max_retries=1)
    try:
        with pytest.raises(OpenHandsTransientError):
            await client.get_current_user()
    finally:
        await client.aclose()


@respx.mock
async def test_client_error_is_not_retried():
    route = respx.post(f"{BASE}/api/v1/app-conversations").mock(
        return_value=httpx.Response(400, json={"detail": "bad request"})
    )
    client = make_client(max_retries=3)
    try:
        with pytest.raises(OpenHandsRequestError):
            await client.start_conversation(title="x")
    finally:
        await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_get_conversation_result_returns_last_agent_message():
    respx.get(f"{BASE}/api/v1/conversation/conv-1/events/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "kind": "MessageEvent",
                        "source": "user",
                        "llm_message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "user text"}],
                        },
                    },
                    {
                        "kind": "MessageEvent",
                        "source": "agent",
                        "llm_message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "agent final answer"}],
                        },
                    },
                ]
            },
        )
    )
    client = make_client()
    try:
        text = await client.get_conversation_result("conv-1")
    finally:
        await client.aclose()
    assert text == "agent final answer"


@respx.mock
async def test_get_conversation_result_none_when_no_agent_message():
    respx.get(f"{BASE}/api/v1/conversation/conv-2/events/search").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    client = make_client()
    try:
        text = await client.get_conversation_result("conv-2")
    finally:
        await client.aclose()
    assert text is None


async def test_missing_key_raises_auth_error():
    client = OpenHandsClient(None, base_url=BASE, sleep=_no_sleep)
    try:
        with pytest.raises(OpenHandsAuthError):
            await client.get_current_user()
    finally:
        await client.aclose()
