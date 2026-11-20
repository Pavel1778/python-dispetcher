# MCP and Self-QA — actual implementation (2026-11-19)

## MCP: what exists, what does not

**Server only.** There is no MCP *client* that consumes external MCP servers to
take actions. The diagnostics surface *reports which MCP servers an OpenHands
account has configured* (names only, never configs or keys — see
`openhands_diagnostics`); it never connects to them.

Two transports, same tools:

1. **stdio** — `python -m orchestration.mcp_server` (JSON-RPC over stdin/stdout).
2. **Streamable HTTP (single request/response)** — `POST /mcp` on the web
   server. Auth is the server's own: a browser session with `X-CSRF-Token`, or
   the machine bearer. The MCP caller therefore holds exactly the caller's
   authority; a notification returns `202` with no body; parse errors `400`.

Tool surface (19 tools, all declared and scope-checked —
`test_every_tool_declares_a_scope`):

`list_projects, get_project, list_repositories, list_tasks, get_task,
create_task, dispatch_task, get_execution_status, list_evidence,
verify_evidence, list_runs, get_run, cancel_run, restart_run, duplicate_run,
list_run_task_outputs, get_run_task_output, list_project_executors,
assign_project_executor`

Authorization model: every project-scoped tool re-checks project membership
(`McpServer._member`); the server resolves the principal from the transport
credential (`user:<login>`); cross-project access returns `NotFound`-class
errors. Writes (`create_task`, `dispatch_task`, executor assignment, run
control) additionally pass through the same write authority as the HTTP API
(session+CSRF or bearer) — there is no side door.

Not implemented (honest):

- Streamable-HTTP session negotiation, server→client notifications, and
  resumability — each `POST /mcp` is an independent request/response.
- MCP resources and prompts primitives — tools only.
- Sampling (`sampling/createMessage`) — the server never calls a model.
- Per-user MCP credentials — the web tier is single-owner.

Verified by: `tests/test_cli_mcp.py` (328 LOC incl. scope and refusal cases),
`tests/test_security_checks.py` (scope declaration), `test_route_auth.py`
(`/mcp` requires auth), `test_write_gate.py` (gate ordering).

## Self-QA (`orchestration/nexus_selfqa.py`, `nexus qa run|verify|report`)

What it is: a **bounded, deterministic** checker. For a repository at a commit
it creates a **real scratch clone** in a temp directory, runs the checks the
repository declares for itself (`.nexus-qa.json`, or defaults) with a minimal
environment, a per-check timeout (default 900 s) and a total timeout (2400 s),
capped output (16 KB per stream), through the same log-redaction the app uses.
Verdicts: `passed` (all required checks exit 0) · `failed` (a required check
ran and exited non-zero) · `skipped` (nothing runnable — *not* a pass) ·
`blocked` (checkout/runner could not be made — *not* a pass).

What it is **not**, by construction (no code path exists for these):

- cannot merge a PR, push to a remote, or create commits in the caller's repo;
- cannot bypass authorization (it holds no credentials; checks run in a
  scratch dir with a minimal env);
- cannot delete branches/tags/worktrees outside its own temp dir;
- cannot touch production state or the running database;
- cannot mark a failing check as skipped to look green — `skipped` is only
  reachable when no check *ran*, and is reported as "not verified";
- does not use a model in the verdict path (LLM review, if wanted, is a
  separate, optional layer and is not part of the verdict).

Tests: `tests/test_selfqa.py` (skipped≠pass, timeout kills the check, required
vs optional semantics, redaction, no NEXUS checkout mutation).

**As a gate on the autonomous loop** (`ProjectQualityGate`): a task the Git
verifier accepted is checked at *that* commit, and only a `failed` verdict
weakens it (to `completed_unverified`, so the task is reworked); `passed`,
`skipped` and `blocked` leave the verdict as it was. It is **per project**
(`nexus_projects.quality_gate`): `auto` (the default) runs only when the
repository declares `.nexus-qa.json`, `on` always, `off` never;
`NEXUS_SELFQA_GATE` overrides that deployment-wide. A private repository is
cloned with the project's own GitHub account, and the token reaches git through
the environment only (never argv, never the URL, never a report). Tests:
`tests/test_quality_gate.py`, `tests/test_quality_gate_policy.py`.

The CLI surface: `nexus qa report` (real system report via `/api/dashboard` + `/api/system`),
`nexus qa run` (isolated checkout run), `nexus qa verify` (evidence
verification). Destructive operations refuse without `--yes` (exit 8).

## Practical diagnostics recipe (single VM)

1. `nexus doctor` — DB round-trip, auth configured, github/openhands presence,
   build identity, login misconfiguration reasons.
2. `nexus migrations` — 42/42 applied? which file is pending?
3. `nexus worker-status` — queue depth, stuck tasks, live workers (heartbeats).
4. `curl -s $NEXUS_URL/readyz` — is the process serving and DB-reachable?
5. `docker compose logs app worker db` — structured; spans for the execution
   path via `NEXUS_TRACE_LOG=1` (`nexus.trace` JSON lines).
6. `nexus qa verify --json` — evidence integrity of a run.
