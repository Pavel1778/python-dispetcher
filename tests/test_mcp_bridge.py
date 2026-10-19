"""MCP bridge tests.

These exercise the *real* MCP server: tools are registered on the MCPServer
and invoked through its ``call_tool`` dispatch path (the same path an MCP
client such as an OpenHands conversation uses). They are not direct calls to
the orchestrator, so they prove the tool wiring, argument schema, auth, and
error text actually work end to end over the MCP surface.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.mcp_server import build_mcp_server
from app.orchestrator import Orchestrator
from app.scope import mint_scope_token
from app.status import TaskRole, TaskStatus
from app.store.memory import InMemoryStateStore

SCOPE_SECRET = "scope-secret"


def _settings(**over) -> Settings:
    base = dict(
        jr_allowed_repositories="acme/widgets",
        jr_max_children_per_workflow=5,
        jr_bridge_token="bridge-token",
        jr_scope_secret=SCOPE_SECRET,
    )
    base.update(over)
    return Settings(**base)


async def _setup(settings: Settings | None = None):
    settings = settings or _settings()
    store = InMemoryStateStore()
    await store.connect()
    wf = await store.create_workflow(name="wf", idempotency_key="wf-1")
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role=TaskRole.SR.value,
        title="sr",
        prompt="plan",
        idempotency_key="sr-1",
        selected_repository="acme/widgets",
    )
    orch = Orchestrator(settings=settings, store=store)
    server = build_mcp_server(orch, scope_secret=SCOPE_SECRET)
    token = mint_scope_token(SCOPE_SECRET, workflow_id=wf.id, task_id=sr.id)
    return settings, store, wf, sr, server, token


def _text(result) -> str:
    return "\n".join(c.text for c in result.content)


async def test_tools_are_registered():
    *_rest, server, _token = await _setup()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "create_jr_task",
        "get_jr_task_status",
        "get_jr_task_result",
        "list_workflow_tasks",
        "request_workflow_verification",
    }


async def test_create_jr_task_via_mcp_creates_real_child():
    settings, store, wf, sr, server, token = await _setup()

    result = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token,
            "workflow_id": wf.id,
            "task_id": sr.id,
            "title": "Add tests",
            "prompt": "Write unit tests for module X.",
            "idempotency_key": "wf-1/jr/tests",
        },
    )
    text = _text(result)
    assert "status=QUEUED" in text
    assert "created=True" in text

    tasks = await store.list_tasks_for_workflow(wf.id)
    children = [t for t in tasks if t.role == TaskRole.JR.value]
    assert len(children) == 1
    assert children[0].title == "Add tests"
    assert children[0].selected_repository == "acme/widgets"


async def test_create_jr_task_rejects_missing_scope_token():
    settings, store, wf, sr, server, _token = await _setup()
    result = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": "not-a-valid-token",
            "workflow_id": wf.id,
            "task_id": sr.id,
            "title": "x",
            "prompt": "y",
            "idempotency_key": "k1",
        },
    )
    assert "ERROR (authorization)" in _text(result)
    tasks = await store.list_tasks_for_workflow(wf.id)
    assert [t for t in tasks if t.role == TaskRole.JR.value] == []


async def test_create_jr_task_rejects_token_for_other_workflow():
    """A valid token for another workflow must not authorise this one."""
    settings, store, wf, sr, server, _token = await _setup()
    other = await store.create_workflow(name="other", idempotency_key="wf-2")
    forged = mint_scope_token(SCOPE_SECRET, workflow_id=other.id, task_id=sr.id)
    result = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": forged,
            "workflow_id": wf.id,
            "task_id": sr.id,
            "title": "x",
            "prompt": "y",
            "idempotency_key": "k1",
        },
    )
    assert "ERROR (authorization)" in _text(result)


async def test_idempotent_tool_call_does_not_duplicate():
    settings, store, wf, sr, server, token = await _setup()
    args = {
        "scope_token": token,
        "workflow_id": wf.id,
        "task_id": sr.id,
        "title": "t",
        "prompt": "p",
        "idempotency_key": "same-key",
    }
    await server.call_tool("create_jr_task", args)
    second = await server.call_tool("create_jr_task", args)
    assert "created=False" in _text(second)
    tasks = await store.list_tasks_for_workflow(wf.id)
    assert len([t for t in tasks if t.role == TaskRole.JR.value]) == 1


async def test_status_and_result_tools():
    settings, store, wf, sr, server, token = await _setup()
    created = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "t", "prompt": "p", "idempotency_key": "k1",
        },
    )
    jr_id = _text(created).split("jr_task_id=")[1].split(" ")[0]

    status = await server.call_tool(
        "get_jr_task_status",
        {"scope_token": token, "workflow_id": wf.id, "task_id": sr.id, "jr_task_id": jr_id},
    )
    assert "status=QUEUED" in _text(status)

    # Simulate the dispatcher recording a result from the child conversation.
    await store.update_task(jr_id, result_summary="tests added", status=TaskStatus.COMPLETED.value)
    result = await server.call_tool(
        "get_jr_task_result",
        {"scope_token": token, "workflow_id": wf.id, "task_id": sr.id, "jr_task_id": jr_id},
    )
    assert "result_summary=tests added" in _text(result)


async def test_list_workflow_tasks():
    settings, store, wf, sr, server, token = await _setup()
    await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "child", "prompt": "p", "idempotency_key": "k1",
        },
    )
    result = await server.call_tool(
        "list_workflow_tasks",
        {"scope_token": token, "workflow_id": wf.id, "task_id": sr.id},
    )
    text = _text(result)
    assert sr.id in text
    assert "child" in text


async def test_repository_allowlist_enforced_via_mcp():
    settings, store, wf, sr, server, token = await _setup()
    result = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "t", "prompt": "p", "idempotency_key": "k1",
            "selected_repository": "evil/repo",
        },
    )
    assert "ERROR (repository)" in _text(result)


async def test_child_cap_enforced_via_mcp():
    settings, store, wf, sr, server, token = await _setup(
        _settings(jr_max_children_per_workflow=1)
    )
    await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "t", "prompt": "p", "idempotency_key": "k1",
        },
    )
    second = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "t2", "prompt": "p", "idempotency_key": "k2",
        },
    )
    assert "ERROR (limit)" in _text(second)


async def test_verification_request_does_not_complete_workflow():
    """The core safety property: Sr cannot declare COMPLETED."""
    settings, store, wf, sr, server, token = await _setup()
    created = await server.call_tool(
        "create_jr_task",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "title": "t", "prompt": "p", "idempotency_key": "k1",
        },
    )
    jr_id = _text(created).split("jr_task_id=")[1].split(" ")[0]

    result = await server.call_tool(
        "request_workflow_verification",
        {
            "scope_token": token, "workflow_id": wf.id, "task_id": sr.id,
            "summary": "everything is done, trust me",
        },
    )
    text = _text(result)
    assert "accepted=True" in text
    assert "unfinished=1" in text

    # Workflow is VERIFYING, not COMPLETED, despite the Sr's claim.
    wf_after = await store.get_workflow(wf.id)
    assert wf_after.status == "VERIFYING"

    # The child is still QUEUED, so completion is correctly withheld.
    child = await store.get_task(jr_id)
    assert child.status == TaskStatus.QUEUED.value
