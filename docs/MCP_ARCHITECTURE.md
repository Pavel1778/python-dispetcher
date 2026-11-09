# NEXUS MCP — Architecture (design)

Status: **partly implemented.** The **server** is implemented
(`orchestration/mcp_server.py`, stdio transport) with the limited tool set in
§4; see [MVP_RELEASE_REPORT.md](MVP_RELEASE_REPORT.md). The **client** and the
Streamable-HTTP transport remain designs below and are **not** implemented.

Implemented tools: `list_projects`, `get_project`, `list_repositories`,
`list_tasks`, `get_task`, `create_task`, `dispatch_task`,
`get_execution_status`, `list_evidence`, `verify_evidence`. Every write
re-checks project membership through the same services the HTTP API uses; there
is no shell/SQL/eval tool.

---

## 1. Principle

MCP must never give an agent more authority than NEXUS itself has. Every MCP
tool maps to an existing, already-authorized operation. There is no generic
"run shell" or "run SQL" tool.

## 2. MCP client (`orchestration/mcp/client.py`)

NEXUS connects to external MCP servers.

```
McpServerConfig {
  name, transport: "stdio" | "streamable_http",
  command?/args?/env?  (stdio),
  url?/headers?        (http),
  enabled, scopes, allowed_tools[], timeout_s, rate_limit
}
```

Operations: connect, disconnect, health check, list tools, list resources, list
prompts, invoke an allowed tool, handle errors, timeout, reconnect, log.

Requirements:

- stdio transport spawns a child process with an explicit, minimal environment;
  credentials are injected per server, never inherited wholesale.
- Streamable HTTP uses TLS and refuses redirects (same rule as the existing
  HTTP clients).
- Every invocation is validated against `allowed_tools` and `scopes` first.
- Every invocation is rate-limited and audited.

## 3. MCP server (`orchestration/mcp/server.py`)

Exposes NEXUS to OpenHands and other MCP clients. Tool groups (brief §6.2),
each mapped to a real NEXUS call:

| Group | Tool | Maps to |
|---|---|---|
| Projects | `list_projects`, `create_project`, `inspect_project`, `get_project_status` | `/api/v1/projects*` |
| Tasks | `create_task`, `list_tasks`, `get_task`, `cancel_task`, `retry_task` | `/api/workflows*`, `/api/tasks*` |
| Agents | `list_agents`, `inspect_agent`, `get_agent_status` | account/profile layer (Phase 6) |
| QA | `run_qa`, `get_qa_report`, `get_qa_history`, `diagnose_project` | `QaRunner`, `EvidenceStore` (Phase 4) |
| GitHub | `list_repositories`, `inspect_repository`, `get_repository_status`, `request_github_action` | `github_client`, confirmation flow |
| Files | `list_files`, `inspect_file`, `upload_file`, `analyze_archive` | `nexus_files` / unified file lab |
| Sandbox | `get_sandbox_status`, `inspect_execution` | Cloud sandbox (read-only) |
| Integrations | `list_integrations`, `inspect_integration` | `/api/capabilities` |

`request_github_action` is the only write, and it goes through the **same
confirmation handshake** as the web path (DECISION-3). It cannot bypass
`GITHUB_ENABLE_WRITES`.

## 4. Security (brief §6.3) — mandatory

| Control | Implementation |
|---|---|
| Authentication | bearer/session token required to reach the MCP server |
| Authorization per project | every project-scoped tool re-checks membership (404 on foreign id) |
| Permission scopes | each tool declares a scope; the client config grants a subset |
| Input validation | JSON-schema per tool; types, bounds, enums |
| Size limits | uploads and payloads bounded; same limits as the file lab |
| Timeout | per-tool timeout, enforced |
| Rate limiting | per-client and per-tool |
| Audit | every call recorded (tool, client, project, outcome) — no secret values |
| No arbitrary shell/SQL | no such tool exists; asserted in tests |
| Path traversal | file tools use the same `safe_workspace_path` as the file lab |
| SSRF | outbound MCP/HTTP calls refuse redirects; internal targets blocked |
| Credential storage | per-server, isolated, never returned by any API |
| Destructive actions | require explicit confirmation; refused without it |

## 5. UI (brief §6.4)

Integrations tab gains: server list, connection status, add server, transport
config, credential management, tool list, connection test, call history, errors,
permission management. Every control calls a real endpoint; none is decorative.

## 6. Testing

- Client: real stdio server (a small local process) and a real Streamable HTTP
  server (a local HTTP server), not mocks.
- Server: an MCP client connects to NEXUS and drives each tool group.
- Security: negative tests for every row in §4 — unauthorized project access,
  scope violation, oversized payload, timeout, rate limit, traversal, SSRF,
  destructive action without confirmation.

## 7. Non-goals

- No MCP tool that performs an action the web UI cannot already perform under
  the same authorization.
- No universal execution tool.
