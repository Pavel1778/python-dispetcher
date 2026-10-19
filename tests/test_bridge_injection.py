"""The dispatcher must hand Sr conversations the bridge, and Jr none.

This is the link that was missing: without it a Sr conversation has no way to
call ``create_jr_task``. These tests assert the exact request payload the
dispatcher sends to OpenHands Cloud.
"""

from __future__ import annotations

import re

import pytest

from app.config import Settings
from app.dispatcher.core import Dispatcher
from app.openhands_client import StartTask
from app.scope import verify_scope_token
from app.status import TaskRole
from app.store.memory import InMemoryStateStore

from .conftest import FakeOpenHandsClient

SCOPE_SECRET = "scope-secret"


def _settings(**over) -> Settings:
    base = dict(
        jr_scope_secret=SCOPE_SECRET,
        jr_allowed_repositories="acme/widgets",
        dispatcher_enable_background_loop=False,
    )
    base.update(over)
    return Settings(**base)


def _start(sid="start-1", status="WORKING", conv=None) -> StartTask:
    return StartTask(
        id=sid,
        status=status,
        app_conversation_id=conv,
        sandbox_id="sandbox-1" if conv else None,
        detail=None,
        created_at=None,
        updated_at=None,
    )


async def _sr_task(store, *, role=TaskRole.SR.value, key="k1"):
    wf = await store.create_workflow(name="wf", idempotency_key=f"wf-{key}")
    task, _ = await store.create_task(
        workflow_id=wf.id,
        role=role,
        title="t",
        prompt="p",
        idempotency_key=key,
        selected_repository="acme/widgets",
    )
    return wf, task


async def _dispatch_one(settings, store, client):
    dispatcher = Dispatcher(settings=settings, store=store, client=client)
    await dispatcher.dispatch()
    return dispatcher


async def test_sr_conversation_receives_scope_token_in_system_suffix():
    store = InMemoryStateStore()
    await store.connect()
    wf, task = await _sr_task(store)
    client = FakeOpenHandsClient()
    client.queue_start_task(_start())

    await _dispatch_one(_settings(), store, client)

    assert len(client.start_calls) == 1
    suffix = client.start_calls[0]["extra_body"]["system_message_suffix"]
    assert wf.id in suffix
    assert task.id in suffix

    # Extract the embedded token and prove it verifies for exactly this scope.
    match = re.search(r"scope token is `([^`]+)`", suffix)
    assert match, suffix
    scope = verify_scope_token(
        SCOPE_SECRET, match.group(1), workflow_id=wf.id, task_id=task.id
    )
    assert scope.workflow_id == wf.id
    assert scope.task_id == task.id


async def test_jr_conversation_receives_no_bridge():
    """A Jr must not be able to spawn grandchildren."""
    store = InMemoryStateStore()
    await store.connect()
    wf, task = await _sr_task(store, role=TaskRole.JR.value)
    client = FakeOpenHandsClient()
    client.queue_start_task(_start())

    await _dispatch_one(_settings(), store, client)

    extra = client.start_calls[0]["extra_body"]
    assert "system_message_suffix" not in extra
    assert "plugins" not in extra


async def test_no_scope_secret_means_no_bridge_injection():
    store = InMemoryStateStore()
    await store.connect()
    wf, task = await _sr_task(store)
    client = FakeOpenHandsClient()
    client.queue_start_task(_start())

    await _dispatch_one(_settings(jr_scope_secret=None), store, client)

    extra = client.start_calls[0]["extra_body"]
    assert "system_message_suffix" not in extra


async def test_plugin_attached_only_when_configured():
    store = InMemoryStateStore()
    await store.connect()
    wf, task = await _sr_task(store)
    client = FakeOpenHandsClient()
    client.queue_start_task(_start())

    await _dispatch_one(
        _settings(
            mcp_public_base_url="https://dispatcher.example.com",
            mcp_plugin_source="github:acme/plugin",
            mcp_plugin_ref="v1",
        ),
        store,
        client,
    )

    plugins = client.start_calls[0]["extra_body"]["plugins"]
    assert plugins == [{"source": "github:acme/plugin", "ref": "v1"}]


async def test_no_plugin_when_unset():
    store = InMemoryStateStore()
    await store.connect()
    wf, task = await _sr_task(store)
    client = FakeOpenHandsClient()
    client.queue_start_task(_start())

    await _dispatch_one(_settings(mcp_public_base_url=None), store, client)

    assert "plugins" not in client.start_calls[0]["extra_body"]
