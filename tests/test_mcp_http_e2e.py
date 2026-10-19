"""End-to-end test over the real MCP Streamable HTTP transport.

Unlike ``test_mcp_bridge.py`` (which calls the server's in-process dispatch),
this test starts a real HTTP server, connects a real MCP *client* to the
mounted ``/mcp`` endpoint, performs the MCP initialize handshake, lists tools,
and calls ``create_jr_task`` over the wire. The tool call then reaches the
dispatcher's store and a real Jr task row is created.

What this proves: the MCP transport, tool schema, scope auth, and dispatcher
wiring all work over HTTP.

What this does NOT prove: that OpenHands Cloud can reach this endpoint, or that
a real Sr LLM chooses to call the tool. Those require a live cloud
conversation (see README "Live E2E").
"""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app, runtime
from app.orchestrator import Orchestrator
from app.scope import mint_scope_token
from app.status import TaskRole
from app.store.memory import InMemoryStateStore

SCOPE_SECRET = "scope-secret"

pytest.importorskip("mcp")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_server():
    """Run the FastAPI app (with /mcp mounted) on a real port."""
    store = InMemoryStateStore()
    await store.connect()

    settings = Settings(
        jr_allowed_repositories="acme/widgets",
        jr_bridge_token="bridge-token",
        jr_scope_secret=SCOPE_SECRET,
        mcp_enabled=True,
        dispatcher_enable_background_loop=False,
    )
    # Fresh app per test: the mounted MCP session manager can only be started
    # once, so reusing a module-level app across lifespans fails.
    app = create_app()
    # Wire runtime so the lifespan does not rebuild dependencies.
    runtime.settings = settings
    runtime.store = store
    runtime.orchestrator = Orchestrator(settings=settings, store=store)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("uvicorn did not start")

    yield store, settings, port

    server.should_exit = True
    await task


async def test_mcp_http_round_trip_creates_jr_task(live_server):
    store, settings, port = live_server
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    wf = await store.create_workflow(name="wf", idempotency_key="wf-1")
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role=TaskRole.SR.value,
        title="sr",
        prompt="plan",
        idempotency_key="sr-1",
        selected_repository="acme/widgets",
    )
    token = mint_scope_token(SCOPE_SECRET, workflow_id=wf.id, task_id=sr.id)

    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "create_jr_task" in names

            result = await session.call_tool(
                "create_jr_task",
                {
                    "scope_token": token,
                    "workflow_id": wf.id,
                    "task_id": sr.id,
                    "title": "Add tests",
                    "prompt": "Write tests for module X.",
                    "idempotency_key": "wf-1/jr/tests",
                },
            )
            text = "\n".join(c.text for c in result.content)
            assert "status=QUEUED" in text
            assert "created=True" in text

    # The call travelled over HTTP and mutated real dispatcher state.
    tasks = await store.list_tasks_for_workflow(wf.id)
    children = [t for t in tasks if t.role == TaskRole.JR.value]
    assert len(children) == 1
    assert children[0].title == "Add tests"
    assert children[0].parent_task_id == sr.id


async def test_mcp_http_rejects_bad_scope_token(live_server):
    store, settings, port = live_server
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    wf = await store.create_workflow(name="wf2", idempotency_key="wf-2")
    sr, _ = await store.create_task(
        workflow_id=wf.id,
        role=TaskRole.SR.value,
        title="sr",
        prompt="plan",
        idempotency_key="sr-2",
        selected_repository="acme/widgets",
    )

    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "create_jr_task",
                {
                    "scope_token": "forged.token",
                    "workflow_id": wf.id,
                    "task_id": sr.id,
                    "title": "x",
                    "prompt": "y",
                    "idempotency_key": "k1",
                },
            )
            text = "\n".join(c.text for c in result.content)
            assert "ERROR (authorization)" in text

    tasks = await store.list_tasks_for_workflow(wf.id)
    assert [t for t in tasks if t.role == TaskRole.JR.value] == []
