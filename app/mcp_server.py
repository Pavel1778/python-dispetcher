"""MCP server exposing the dispatcher's orchestration tools to a Sr conversation.

This is the real Sr -> Jr transport. OpenHands Cloud conversations accept MCP
servers (``MCPConfig`` -> ``mcpServers``, attached via conversation settings or
via a plugin's ``.mcp.json``), so a Sr conversation can call these tools
directly, just like a built-in tool.

The tools are deliberately narrow. There is no generic SQL tool, no generic
HTTP tool, and no filesystem access. Each tool maps to one validated
``Orchestrator`` method.

Auth: every tool call must carry a per-conversation *scope token* minted by the
dispatcher (see ``app/scope.py``). A shared bearer token alone would let any Sr
act on any workflow; the scope token binds the caller to one workflow + task.
The token is passed as an ordinary tool argument so it works over any MCP
transport without custom header plumbing.

Transport note: this module exposes a Streamable HTTP app. Over a network the
scope token is the security boundary, so the endpoint must be served over TLS.
When the dispatcher and the MCP server share a process, ``/mcp`` is mounted on
the same FastAPI app and inherits its exposure.
"""

from __future__ import annotations

import contextlib
from typing import Any

# Imported at module scope (not inside the builder) because the MCP tool
# decorator resolves type annotations from this module's globals. main.py
# imports this module lazily, so a missing `mcp` package is still non-fatal.
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .logging_config import get_logger, safe_extra
from .orchestrator import (
    CapExceeded,
    Orchestrator,
    OrchestratorError,
    RepositoryNotAllowed,
    ScopeViolation,
)
from .scope import ScopeError, verify_scope_token

logger = get_logger(__name__)

# Human-readable descriptions matter: the agent chooses tools by description.
_CREATE_JR_TASK_DOC = """Create a Jr (junior) task and start a separate OpenHands
conversation to carry it out. Use this to delegate a concrete, self-contained
unit of work (for example: "add retry logic to X and open a PR").

Args:
  scope_token: REQUIRED. Authorization token injected into your context by the
    dispatcher. Never invent or modify it.
  workflow_id: REQUIRED. Your workflow id.
  task_id: REQUIRED. Your own Sr task id.
  title: short imperative title for the child task.
  prompt: full instructions for the child conversation. Be specific; the child
    sees only this prompt.
  idempotency_key: REQUIRED. Stable key for this child, e.g. "wf-123/jr/tests".
    Calling again with the same key returns the existing child instead of
    creating a duplicate.
  selected_repository: optional "owner/repo". Must be allowlisted.
  selected_branch: optional branch name.

Returns the child task id and status. The child runs asynchronously; poll it
with get_jr_task_status.
"""


def build_mcp_server(orchestrator: Orchestrator, *, scope_secret: str | None):
    """Build the MCP server bound to ``orchestrator``."""
    server = MCPServer(
        name="openhands-dispatcher",
        title="OpenHands Dispatcher",
        instructions=(
            "Tools for delegating work to Jr conversations and reporting "
            "completion. A workflow is only marked COMPLETED after the "
            "dispatcher independently confirms that every child conversation "
            "finished, so do not treat request_workflow_verification as "
            "declaring success."
        ),
    )

    def _authorize(scope_token: str, workflow_id: str, task_id: str) -> None:
        """Verify the per-conversation scope token. Raises on any mismatch."""
        try:
            verify_scope_token(
                scope_secret or "",
                scope_token,
                workflow_id=workflow_id,
                task_id=task_id,
            )
        except ScopeError as exc:
            # Log the reason, never the token value.
            logger.warning("mcp scope rejected", **safe_extra(reason=str(exc)))
            raise

    def _error_text(exc: Exception) -> str:
        if isinstance(exc, ScopeViolation):
            return f"ERROR (scope): {exc}"
        if isinstance(exc, RepositoryNotAllowed):
            return f"ERROR (repository): {exc}"
        if isinstance(exc, CapExceeded):
            return f"ERROR (limit): {exc}"
        if isinstance(exc, OrchestratorError):
            return f"ERROR (invalid request): {exc}"
        if isinstance(exc, ScopeError):
            return f"ERROR (authorization): {exc}"
        return f"ERROR: {type(exc).__name__}"

    @server.tool(
        name="create_jr_task",
        description=_CREATE_JR_TASK_DOC,
        structured_output=False,
    )
    async def create_jr_task(
        scope_token: str,
        workflow_id: str,
        task_id: str,
        title: str,
        prompt: str,
        idempotency_key: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
    ) -> list[TextContent]:
        try:
            _authorize(scope_token, workflow_id, task_id)
            view = await orchestrator.create_jr_task(
                workflow_id=workflow_id,
                task_id=task_id,
                title=title,
                prompt=prompt,
                idempotency_key=idempotency_key,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
            return [TextContent(type="text", text=_error_text(exc))]
        return [
            TextContent(
                type="text",
                text=(
                    f"jr_task_id={view.task_id} status={view.status} "
                    f"created={view.created}"
                ),
            )
        ]

    @server.tool(
        name="get_jr_task_status",
        description=(
            "Get the current status of a Jr child task you created. "
            "Args: scope_token, workflow_id, task_id, jr_task_id (all required)."
        ),
        structured_output=False,
    )
    async def get_jr_task_status(
        scope_token: str, workflow_id: str, task_id: str, jr_task_id: str
    ) -> list[TextContent]:
        try:
            _authorize(scope_token, workflow_id, task_id)
            view = await orchestrator.get_jr_task_status(
                workflow_id=workflow_id, task_id=task_id, jr_task_id=jr_task_id
            )
        except Exception as exc:  # noqa: BLE001
            return [TextContent(type="text", text=_error_text(exc))]
        return [
            TextContent(
                type="text",
                text=(
                    f"status={view.status} attempts={view.attempts} "
                    f"last_error={view.last_error or 'none'}"
                ),
            )
        ]

    @server.tool(
        name="get_jr_task_result",
        description=(
            "Get the recorded result of a Jr child task. The result is written "
            "by the dispatcher from the child conversation, not supplied by "
            "you. Args: scope_token, workflow_id, task_id, jr_task_id."
        ),
        structured_output=False,
    )
    async def get_jr_task_result(
        scope_token: str, workflow_id: str, task_id: str, jr_task_id: str
    ) -> list[TextContent]:
        try:
            _authorize(scope_token, workflow_id, task_id)
            view = await orchestrator.get_jr_task_result(
                workflow_id=workflow_id, task_id=task_id, jr_task_id=jr_task_id
            )
        except Exception as exc:  # noqa: BLE001
            return [TextContent(type="text", text=_error_text(exc))]
        return [
            TextContent(
                type="text",
                text=(
                    f"status={view.status}\n"
                    f"result_summary={view.result_summary or '(not available yet)'}"
                ),
            )
        ]

    @server.tool(
        name="list_workflow_tasks",
        description=(
            "List all tasks in your workflow with their statuses. Useful to "
            "check which children are still running. "
            "Args: scope_token, workflow_id, task_id."
        ),
        structured_output=False,
    )
    async def list_workflow_tasks(
        scope_token: str, workflow_id: str, task_id: str
    ) -> list[TextContent]:
        try:
            _authorize(scope_token, workflow_id, task_id)
            views = await orchestrator.list_workflow_tasks(
                workflow_id=workflow_id, task_id=task_id
            )
        except Exception as exc:  # noqa: BLE001
            return [TextContent(type="text", text=_error_text(exc))]
        lines = [
            f"{v.task_id} {v.status} attempts={v.attempts} {v.title or ''}".rstrip()
            for v in views
        ]
        return [TextContent(type="text", text="\n".join(lines) or "(no tasks)")]

    @server.tool(
        name="request_workflow_verification",
        description=(
            "Declare that you have finished your work and ask the dispatcher to "
            "verify the workflow. This does NOT complete the workflow: the "
            "dispatcher marks it COMPLETED only after independently confirming "
            "every Jr child conversation finished. "
            "Args: scope_token, workflow_id, task_id, summary (optional)."
        ),
        structured_output=False,
    )
    async def request_workflow_verification(
        scope_token: str,
        workflow_id: str,
        task_id: str,
        summary: str | None = None,
    ) -> list[TextContent]:
        try:
            _authorize(scope_token, workflow_id, task_id)
            result: dict[str, Any] = await orchestrator.request_workflow_verification(
                workflow_id=workflow_id, task_id=task_id, summary=summary
            )
        except Exception as exc:  # noqa: BLE001
            return [TextContent(type="text", text=_error_text(exc))]
        return [
            TextContent(
                type="text",
                text=(
                    f"accepted={result['accepted']} "
                    f"children={result['child_count']} "
                    f"unfinished={result['unfinished_children']}\n"
                    f"{result['note']}"
                ),
            )
        ]

    return server


class McpBridge:
    """Owns the MCP server, its ASGI app, and its session-manager lifecycle.

    Starlette does not run the lifespan of a *mounted* sub-app, so the MCP
    session manager's task group is never initialised if the app is mounted
    naively - every request then fails with "Task group is not initialized".
    The parent app must therefore enter ``session_manager.run()`` in its own
    lifespan. This class keeps that wiring in one place.
    """

    def __init__(
        self, orchestrator: Orchestrator, *, scope_secret: str | None, path: str = "/"
    ) -> None:
        self._server = build_mcp_server(orchestrator, scope_secret=scope_secret)
        # ``path`` is the path *inside* this app. When mounted at "/mcp",
        # Starlette strips the prefix, so the inner path must be "/" and the
        # public URL is "/mcp/". Using "/mcp" here would give "/mcp/mcp".
        self._asgi = self._server.streamable_http_app(streamable_http_path=path)
        self._manager = self._server.session_manager

    @property
    def asgi_app(self):
        return self._asgi

    @contextlib.asynccontextmanager
    async def lifespan(self):
        """Run the MCP session manager for the lifetime of the parent app."""
        async with self._manager.run():
            yield
