# CLI AND MCP ACCEPTANCE — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: VERIFIED.** The CLI is a thin HTTP client with full lifecycle
coverage; MCP exposes 19 scoped tools over stdio and Streamable HTTP. 21
CLI/MCP tests pass.

---

## 1. CLI surface

`python -m orchestration.cli <command>` (thin client over the HTTP API):

```
version, doctor, status, migrations, worker-status, providers,
project, run, task, workflow, evidence, agent, openhands, restore-point, qa
```

Full lifecycle covered:

| Need | Command |
| --- | --- |
| Authentication | `--url`/`NEXUS_URL` + bearer token / session |
| Project listing / creation / selection | `project list|create|get` |
| Task submission / inspection | `task create|get|list` |
| Run inspection / control | `run list|get|cancel|restart|duplicate|switch` |
| Agent / account status | `agent`, `openhands accounts` |
| OpenHands diagnostics | `openhands diagnostics|tools|inventory|connection|errors` |
| Evidence / SelfQA | `evidence`, `qa` |
| Recovery | `restore-point create|list|verify` |
| Configuration diagnostics | `doctor`, `providers`, `migrations`, `worker-status` |

The CLI never prints a secret: account commands show `api_key_hint` only.

## 2. MCP surface

19 tools, each carrying a scope from `MCP_TOOL_SCOPES`
(`tests/test_security_checks.py::test_every_tool_declares_a_scope`):

```
list_projects, get_project, list_repositories,
list_tasks, get_task, create_task, dispatch_task, get_execution_status,
list_evidence, verify_evidence,
list_runs, get_run, cancel_run, restart_run, duplicate_run,
list_run_task_outputs, get_run_task_output, list_project_executors,
assign_project_executor
```

- **Two transports**: stdio (`python -m orchestration.mcp_server`) and
  Streamable HTTP at `POST /mcp` (reuses the web server's session/CSRF or
  bearer auth).
- **Every tool re-checks project membership** on a write; a tool with no scope
  entry cannot run.
- **No shell/SQL/eval tool.** An agent cannot reach the filesystem or a
  database through MCP.
- **Long-running operations** (`create_task`, `dispatch_task`, `restart_run`)
  return a durable id; the agent polls `get_task`/`get_run`, not a blocking call.
- **Result serialization** is JSON with `str()` on non-serialisable fields; no
  secret is in any result.

## 3. Verified (this session)

```
pytest tests/test_cli_mcp.py -q
-> 21 passed
```

Includes: tool schemas, authentication (bearer required), project scoping (a
non-member is refused), input validation, error handling, and that no tool leaks
a secret.

## 4. An agent can run its own verification

The Sr/Jr client-tool bridge (`orchestration/sr_tools.py`) lets an agent call
`sr_create_task`, `sr_get_task_status`, `sr_get_task_report`,
`sr_request_task_revision`, `sr_accept_task_result`, `sr_cancel_task`. Combined
with the `qa`/`evidence` MCP tools, an agent can run project self-checks and read
evidence without a person executing each command — but only within its project's
scope, and `workflow_id` is always substituted by the bridge, never taken from
the model.

## 5. Remaining limit

Streamable-HTTP MCP is implemented and tested over real HTTP; a third-party MCP
client's exact handshake was not exercised here (the tests drive the route
directly). No unrestricted administrative or filesystem access is exposed.
