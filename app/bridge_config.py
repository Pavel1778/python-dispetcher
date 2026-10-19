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


def mcp_tools_attached(settings: Settings) -> bool:
    """Whether a new conversation can actually be given the dispatcher MCP tools.

    There are only two supported attach paths (see the module docstring): a
    git-hosted plugin, or a server registered in account settings. If neither
    is configured the tools are unreachable, and a Sr conversation must not be
    told to call them.
    """
    if not settings.mcp_enabled:
        return False
    if build_plugin_spec(settings) is not None:
        return True
    # No plugin: the only remaining path is an account-level registration, which
    # requires a public base URL to have been configured.
    return bool(settings.mcp_public_base_url)


def build_plugin_spec_if_buildable(settings: Settings) -> dict[str, Any] | None:
    """Return the plugin spec only when its MCP config can be built.

    A plugin whose ``.mcp.json`` cannot be generated (missing
    ``MCP_PUBLIC_BASE_URL``) would point the sandbox at an unusable endpoint,
    so it is not attached at all.
    """
    spec = build_plugin_spec(settings)
    if spec is None:
        return None
    build_mcp_config(settings)  # raises ValueError when not buildable
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
    tools_attached: bool = True,
) -> str:
    """System-prompt suffix telling Sr how to call the bridge.

    The scope token is injected here (not via a header) because MCP tool
    arguments are the only channel that works uniformly across transports.

    ``tools_attached`` must reflect whether the MCP server was actually attached
    to this conversation. When it was not, the instructions must not name tools
    the Sr does not have - that would make it call a tool that does not exist.
    """
    tools = tool_names or [
        "create_jr_task",
        "get_jr_task_status",
        "get_jr_task_result",
        "list_workflow_tasks",
        "request_workflow_verification",
    ]
    if not tools_attached:
        return (
            "\n\n## Dispatcher bridge\n"
            f"Your workflow_id is `{workflow_id}` and your task_id is `{task_id}`.\n"
            "The dispatcher bridge tools are NOT available in this conversation, "
            "so you cannot delegate to Jr conversations or report verification. "
            "Do the work you can yourself and summarise clearly what remains for "
            "an operator to dispatch.\n"
        )
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
