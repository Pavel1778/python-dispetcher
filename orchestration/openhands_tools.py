"""What tools an OpenHands agent actually has, and what NEXUS exposes itself.

The request behind this module asks for two separate answers and forbids
conflating them:

* which tools the **agent inside an OpenHands conversation** can call
  (terminal, the workspace file system, Git, testing, browser, the allowed MCP
  servers), and whether they are *available* — read from the agent-server's own
  tool catalog, not assumed from configuration;
* which tools **NEXUS** offers, which reach an OpenHands agent as *client
  tools* over the event stream (the ``sr_`` bridge) and as MCP tools of NEXUS's
  own MCP server — never as agent-server built-ins.

A tool being *configured* is not proof it is *available*, and availability is
not proof it *ran*: only an observed ``ActionEvent`` proves exercise. This
module only normalises what the server reports; the diagnostics decide the
status from that observation.

The normaliser is a pure function so it can be tested without a socket, and it
accepts every shape the two documented endpoints return:

* ``GET /api/tools/catalog`` (or ``/api/tools/``) — a list (or mapping) of tool
  descriptors carrying ``user_selectable`` / ``in_default_set`` / ``usable``;
* ``GET /server_info`` — ``{"usable_tools": ["terminal", ...]}``.

Nothing here contacts the network; it only interprets a payload.
"""

from __future__ import annotations

from typing import Any


class ToolCatalogUnsupported(RuntimeError):
    """This transport exposes no agent-server tool catalog.

    Raised by a transport that has no agent-server to ask (the Cloud V1
    *app* transport) or that has no sandbox to ask right now. Distinct from a
    failure: nothing was proved either way, so a caller reports ``BLOCKED``.
    """


#: The capability groups the request names, each proved by a set of tools the
#: agent-server reports. Git and testing have no dedicated built-in — the agent
#: runs ``git`` and ``pytest`` through the terminal — so both are proved by
#: ``terminal``; the evidence says so rather than inventing a tool name.
CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    "terminal": ("terminal",),
    "workspace_filesystem": (
        "file_editor", "read_file", "write_file", "edit",
        "list_directory", "glob", "grep", "planning_file_editor",
    ),
    "git": ("terminal",),
    "testing": ("terminal",),
    "browser": ("browser_tool_set",),
}

#: Capabilities that must be present for the check to pass. Browser is opt-in
#: ("if provided for"); MCP and the NEXUS tools are checked separately.
REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "terminal", "workspace_filesystem", "git", "testing",
)


def normalize_catalog(payload: Any) -> dict[str, dict[str, bool]]:
    """Normalise a tool-catalog or ``/server_info`` payload.

    Returns ``{tool_name: {"user_selectable": bool, "in_default_set": bool,
    "usable": bool}}``. An unknown shape yields ``{}`` rather than raising: the
    caller reports what was actually seen.
    """
    tools: dict[str, dict[str, bool]] = {}
    if not isinstance(payload, dict):
        return tools

    # ``/server_info`` -> ``usable_tools``: presence alone proves usability.
    for name in payload.get("usable_tools") or []:
        if isinstance(name, str) and name:
            tools.setdefault(name, {"user_selectable": False,
                                    "in_default_set": False, "usable": True})

    raw = payload.get("tools")
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for name, meta in raw.items():
            items.append({**meta, "name": name} if isinstance(meta, dict)
                         else {"name": name})

    for item in items:
        if isinstance(item, str):
            tools.setdefault(item, {"user_selectable": True,
                                    "in_default_set": False, "usable": True})
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        tools[name] = {
            "user_selectable": bool(item.get("user_selectable", True)),
            "in_default_set": bool(item.get("in_default_set", False)),
            "usable": bool(item.get("usable", True)),
        }
    return tools


def usable_tool_names(tools: dict[str, dict[str, bool]]) -> list[str]:
    """The names the server reports as usable, sorted."""
    return sorted(name for name, meta in tools.items() if meta.get("usable", True))


def capability_report(tools: dict[str, dict[str, bool]]) -> dict[str, dict[str, Any]]:
    """Per-capability availability from a normalised catalog.

    A capability is available when at least one of its tools is present *and*
    usable. The evidence names the tools that proved it, so a reader can tell a
    real tool from a capability folded onto the terminal.
    """
    usable = {name for name, meta in tools.items() if meta.get("usable", True)}
    out: dict[str, dict[str, Any]] = {}
    for capability, names in CAPABILITY_TOOLS.items():
        present = [n for n in names if n in usable]
        out[capability] = {
            "available": bool(present),
            "proved_by": present,
            "considered": list(names),
        }
    return out


def missing_capabilities(coverage: dict[str, dict[str, Any]]) -> list[str]:
    """The required capabilities that are not available."""
    return [c for c in REQUIRED_CAPABILITIES
            if not coverage.get(c, {}).get("available")]


def mcp_server_names(settings: dict[str, Any] | None) -> list[str]:
    """The MCP server names configured for an account — names only, never bodies.

    A server name is not a secret; the server's configuration may carry one, so
    only the keys are ever returned.
    """
    agent = (settings or {}).get("agent_settings") or {}
    mcp = agent.get("mcp_config") or {}
    return sorted(mcp.keys()) if isinstance(mcp, dict) else []


def nexus_tool_names() -> dict[str, list[str]]:
    """The tools NEXUS itself exposes, split by how they reach an agent.

    * ``mcp`` — NEXUS's own MCP server tools (``orchestration/mcp_server.py``),
      reachable by an MCP client and by the studio, not by an agent-server
      built-in.
    * ``client_tools`` — the ``sr_`` bridge (``orchestration/sr_tools.py``),
      delivered into a conversation as ``ClientToolSpec`` and answered over the
      event stream. These are the *project tools* an agent may call back into.

    Neither is part of the agent-server catalog: a tool being available to NEXUS
    does not make it an OpenHands built-in, and vice versa.
    """
    from .mcp_server import McpServer
    from .sr_tools import SrTools

    mcp = sorted(t["name"] for t in McpServer.tool_schemas())
    client = sorted(t["name"] for t in SrTools.tool_schemas())
    return {"mcp": mcp, "client_tools": client}
