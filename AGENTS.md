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
  factory. Anything that mounts the MCP transport **must use `create_app()`**:
  the MCP session manager can only be started once, and mounts accumulate on a
  reused app. This is why tests build a fresh app per test.
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

## Secrets

Never print or persist: the OpenHands API key, `sandbox_api_key`, auth headers,
or full `/users/me` responses. `Settings.redaction_secrets()` lists values that
must be redacted from logs and API responses; extend it when adding a secret.
`.env` is git-ignored — keep it that way.
