# AGENTS.md

Repository-specific knowledge for the OpenHands Sr/Jr dispatcher.

## Commands

```bash
pip install -r requirements-dev.txt   # deps + pytest
pytest -q                             # unit + store-contract (in-memory)
make test-integration                 # adds real PostgreSQL (needs TEST_DATABASE_URL)
uvicorn app.main:app --port 8000      # run locally (in-memory store if no DATABASE_URL)
```

Tests import `app.*`, so run pytest from the repo root (or set `PYTHONPATH=.`).

## Architecture notes

* `app/main.py` exposes **both** a module-level `app` and a `create_app()`
  factory. The module-level `app` is itself `create_app()` and is what
  `uvicorn app.main:app` serves. The factory exists so tests can build a fresh
  app per test: the MCP sub-app's session manager can only be started once, and
  `_mount_mcp` accumulates mounts if called twice on the same app. Build a new
  app with `create_app()` rather than mounting onto an existing one.
* `app/orchestrator.py` is the **single** place that creates Jr child tasks.
  Both the MCP transport (`app/mcp_server.py`) and `POST /jr/tasks` route
  through it, so the repository allowlist and child cap cannot be bypassed by
  choosing a transport. Do not add a second creation path.
* `app/scope.py` mints HMAC-SHA256 scope tokens binding Sr conversation →
  workflow/task. `create_jr_task` verifies the token *and* re-reads the parent
  task from the store; never trust the token alone.
* `JR_ALLOWED_REPOSITORIES` is a **comma-separated string**, not `list[str]`.
  pydantic-settings JSON-decodes complex types, so a list field would make
  `JR_ALLOWED_REPOSITORIES=owner/repo` (and the empty value) crash startup. Read
  it via `Settings.allowed_repositories`. **Empty means deny all** (fail closed).
* `app/dashboard.py` is the operator UI (`/`, `/login`, `/workflows/{id}`,
  `/api/config`, `/api/health`). It is **stdlib-only** (no Jinja2/itsdangerous)
  so the Render Free footprint does not grow. It is wired in `create_app()` via
  `_RuntimeProxy`, which forwards attribute access to the live `runtime` object -
  without it the dashboard would capture the `None` store seen at import time.
* `app/auth.py` owns scrypt password hashing and stateless HMAC session tokens.
  The dashboard is **disabled unless both** `DASHBOARD_USERNAME` and
  `DASHBOARD_PASSWORD_HASH` are set; it fails closed rather than exposing an
  unauthenticated panel. CSRF is a double-submit token derived from the session
  cookie (`SessionSigner.csrf_token`), so no extra server state is needed.

## Gotchas learned the hard way

* FastAPI `app.mount()` strips the mount prefix before dispatching, so the inner
  MCP ASGI app must be served at path `"/"`, not `"/mcp"`.
* A mounted Starlette sub-app's own lifespan is **not** run by the parent. The
  MCP session manager is therefore entered explicitly via `McpBridge.lifespan()`
  inside the parent lifespan.
* `mcp` 2.x renamed `FastMCP` to `MCPServer`; the client's `streamable_http_client`
  yields 2 values, not 3.
* Tool type imports must live at module scope in `mcp_server.py` or signature
  introspection fails with `InvalidSignature`.
* **Attaching the bridge to a Sr conversation**: `AppConversationStartRequest`
  accepts `plugins` (a list of `PluginSpec`) and `system_message_suffix`, but
  **not** a raw `mcp_config` - the schema sets `additionalProperties: null`, so
  any extra field is rejected. There are only two supported attach paths:
  a git-hosted plugin (`MCP_PLUGIN_SOURCE`) whose repo contains a `.mcp.json`
  built by `build_mcp_config()`, or an MCP server registered once in account
  settings. If neither is configured the tools are simply unavailable, so
  `build_scope_instructions(tools_attached=False)` must be used: naming tools the
  conversation does not have makes the model call a tool that does not exist.
  `bridge_config.mcp_tools_attached()` is the single source of truth for this.
* `POST /api/v1/app-conversations` requires `initial_message.content` to be a
  **list of content blocks** (`[{"type": "text", "text": ...}]`). A bare string is
  rejected with HTTP 422 (`list_type`), which fails every dispatch before a
  conversation is created. `openhands_client.start_conversation` wraps the string.

## Resilience invariants

* A workflow only reaches `COMPLETED` when every Jr child is `COMPLETED` **with a
  recorded `result_summary`** and the Sr reached `VERIFYING`. A Jr whose
  conversation finished without a result is `BLOCKED`, never `COMPLETED`. If the
  child result cannot be read yet, defer and retry - never treat an API failure
  as "no result".
* `claim_due_tasks` refuses tasks with `attempts >= max_attempts`. Something must
  therefore move them to a terminal state, or they stall forever:
  `expire_exhausted_tasks()` sweeps **QUEUED** tasks only. Do not widen it to
  STARTING/RUNNING/VERIFYING - those are still polled by `advance()` and a high
  attempt count there does not mean the task is stuck (a Sr can legitimately sit
  in VERIFYING while its children run).
* `dispatcher_start_task_timeout_seconds` is enforced in `_apply_start_task`; a
  start task that never becomes `READY` fails rather than holding the task open.
* `_fail()` scrubs its message via `Settings.redaction_secrets()` because the text
  can embed upstream error bodies (e.g. a start-task `detail`) that are persisted
  and later served to the Sr through `get_jr_task_status`.
* The notification queue has no external sink yet (Better Stack is deferred);
  `drain_notifications()` logs each row and marks it sent so the queue stays
  bounded on the free tiers.

## Secrets

Never print or persist: the OpenHands API key, `sandbox_api_key`, auth headers,
or full `/users/me` responses. `Settings.redaction_secrets()` lists values that
must be redacted from logs and API responses; extend it when adding a secret.
`.env` is git-ignored — keep it that way.
