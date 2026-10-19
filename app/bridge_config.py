"""How a Sr conversation is told about the bridge.

Verified against the live OpenHands Cloud V1 OpenAPI spec
(``https://app.all-hands.dev/openapi.json``) and the official MCP docs:

* ``AppConversationStartRequest`` accepts ``plugins`` (a ``PluginSpec`` list)
  and ``system_message_suffix``; it does **not** accept a raw ``mcp_config``.
* The docs state plugins are "loaded and their skills/MCP config are merged
  into the agent", and that MCP servers can be configured via the SDK's
  ``mcp_config``, via Agent Canvas settings, or via a plugin's ``.mcp.json``.

So there are exactly two supported ways to give a *cloud* Sr conversation the
dispatcher MCP tools:

1. **Plugin** - a git-hosted plugin directory containing ``.mcp.json`` that
   points at this dispatcher's ``/mcp`` endpoint. Passed as
   ``plugins=[{"source": "github:owner/repo", ...}]``. Requires the plugin to
   be reachable by the cloud sandbox (i.e. pushed to a git host) and the
   dispatcher to be publicly reachable over TLS.
2. **User settings** - register the MCP server once in the user's agent
   settings (``POST /api/v1/settings`` -> ``agent_settings.mcp_config``), after
   which *new* conversations see it.

Neither is performed automatically here: both change state outside this
repository (a git push, or a cloud account setting), which is out of scope for
this branch. This module builds the exact payloads for both, and the dispatcher
injects them only when the corresponding config is present, so the wiring is
ready but inert until an operator opts in.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings


def build_mcp_config(settings: Settings) -> dict[str, Any]:
    """Build the MCPConfig JSON for this dispatcher's MCP server.

    Shape (verified against ``MCPConfig`` / ``RemoteMCPServer``):
        {"mcpServers": {"<name>": {"url": ..., "transport": "streamable-http"}}}
    """
    if not settings.mcp_public_base_url:
        raise ValueError("MCP_PUBLIC_BASE_URL is required to build an MCP config")
    url = settings.mcp_public_base_url.rstrip("/") + settings.mcp_mount_path
    server: dict[str, Any] = {
        "url": url,
        "transport": "streamable-http",
    }
    if settings.mcp_bearer_token:
        server["headers"] = {"Authorization": f"Bearer {settings.mcp_bearer_token}"}
    if settings.mcp_request_timeout_seconds:
        server["timeout"] = settings.mcp_request_timeout_seconds
    return {"mcpServers": {settings.mcp_server_name: server}}


def build_plugin_spec(settings: Settings) -> dict[str, Any] | None:
    """Return the ``PluginSpec`` dict for the dispatcher plugin, if configured.

    ``source`` is a git source (``github:owner/repo`` or a git URL) that the
    cloud sandbox can clone. The repo must contain ``.mcp.json`` with the MCP
    config from :func:`build_mcp_config`.
    """
    if not settings.mcp_plugin_source:
        return None
    spec: dict[str, Any] = {"source": settings.mcp_plugin_source}
    if settings.mcp_plugin_ref:
        spec["ref"] = settings.mcp_plugin_ref
    if settings.mcp_plugin_repo_path:
        spec["repo_path"] = settings.mcp_plugin_repo_path
    return spec


def build_plugin_mcp_json(settings: Settings) -> str:
    """Render the ``.mcp.json`` a plugin repository must contain."""
    return json.dumps(build_mcp_config(settings), indent=2)


def build_agent_settings_payload(settings: Settings) -> dict[str, Any]:
    """Build the ``POST /api/v1/settings`` payload registering the MCP server.

    Applied by an operator, not automatically. Merges into
    ``agent_settings.mcp_config``.
    """
    return {"agent_settings": {"mcp_config": build_mcp_config(settings)}}


def build_scope_instructions(
    *,
    workflow_id: str,
    task_id: str,
    scope_token: str,
    tool_names: list[str] | None = None,
) -> str:
    """System-prompt suffix telling Sr how to call the bridge.

    The scope token is injected here (not via a header) because MCP tool
    arguments are the only channel that works uniformly across transports.
    """
    tools = tool_names or [
        "create_jr_task",
        "get_jr_task_status",
        "get_jr_task_result",
        "list_workflow_tasks",
        "request_workflow_verification",
    ]
    return (
        "\n\n## Dispatcher bridge\n"
        "You can delegate work to Jr conversations through the dispatcher MCP "
        f"tools: {', '.join(tools)}.\n"
        f"Your workflow_id is `{workflow_id}` and your task_id is `{task_id}`.\n"
        f"Your scope token is `{scope_token}`.\n"
        "Pass workflow_id, task_id, and scope_token to every bridge tool call. "
        "Never invent or modify the scope token, and never share it outside "
        "these tool calls.\n"
        "Use a stable `idempotency_key` per child so a repeated call returns "
        "the existing child instead of creating a duplicate.\n"
        "When your work is done, call request_workflow_verification. That does "
        "not complete the workflow: the dispatcher marks it COMPLETED only "
        "after independently confirming every Jr child conversation finished.\n"
    )
