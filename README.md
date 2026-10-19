# OpenHands Cloud Dispatcher (Python)

Autonomous Sr/Jr task dispatcher for [OpenHands Cloud](https://app.all-hands.dev),
designed to run on **Render Free** with durable state in **PostgreSQL/Supabase**.

> Status: **pre-production**. Unit, store-contract, and MCP HTTP end-to-end tests
> pass locally. Supabase, Render, and Better Stack are **not yet verified against
> live services**. See "Not yet verified".

## Architecture

```
FastAPI (app/main.py, built by create_app())
  ├── GET  /health          liveness only (process is up)
  ├── GET  /ready           dependency readiness (DB + API key + last cycle)
  ├── POST /jr/tasks        protected HTTP bridge: Sr spawns a Jr child task
  └── MOUNT /mcp            MCP streamable-HTTP server exposing create_jr_task

Background loop (app/main.py: dispatcher_loop)
  └── Dispatcher.run_cycle()  (app/dispatcher/core.py)
        ├── dispatch()    claim due tasks -> start conversations
        ├── advance()     poll start tasks / conversations
        ├── reconcile()   resolve stale remote state after restart
        └── verify_workflows()  gate COMPLETED on real child completion

StateStore (app/store/)
  ├── base.py       explicit interface (the dispatcher only uses this)
  ├── memory.py     in-memory backend (tests / no DB)
  └── postgres.py   PostgreSQL/Supabase backend (psycopg + pool)

OpenHandsClient (app/openhands_client.py)
  └── V1 API: async start tasks, batch fetch, search, retries, rate limits
```

### Task lifecycle

`QUEUED → STARTING → RUNNING → VERIFYING → COMPLETED` (plus `FAILED`, `BLOCKED`,
`CANCELLED`). Workflow: `QUEUED → PLANNING → RUNNING → VERIFYING → COMPLETED`
(plus `FAILED`, `BLOCKED`).

A workflow becomes `COMPLETED` **only** when every Jr child task is
`COMPLETED` *and* the Sr task has reached `VERIFYING`. There is no path that
marks a workflow complete on the strength of a plan alone.

### Asynchronous conversation creation

`POST /api/v1/app-conversations` returns an **`AppConversationStartTask`** whose
`app_conversation_id` is `null` until the sandbox is ready. The dispatcher
stores `start_task_id` and `app_conversation_id` as **separate columns** and
polls start tasks until `status == "READY"`.

### Idempotency and recovery

* Tasks and workflows carry a unique `idempotency_key`; re-creating returns the
  existing row.
* Tasks are claimed with a time-bounded **lease** (`FOR UPDATE SKIP LOCKED` on
  Postgres), so two dispatchers can never start the same conversation twice.
* On startup the dispatcher `recover()`s non-terminal tasks: those with no
  remote ids are re-queued; those already linked are `reconcile()`d, never
  recreated.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in real values; never commit .env
```

Environment variables are documented in `.env.example`. Secrets are read from
the environment only and are never logged.

## Running

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Without `DATABASE_URL` the app uses the in-memory store (not durable) and logs a
warning.

## Testing

```bash
# Unit + store-contract tests (in-memory backend)
pytest -q

# Include real PostgreSQL integration tests
TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/dispatcher" pytest -q
```

The Postgres path applies versioned migrations from `app/migrations/` and
records them in `schema_migrations`.

## Database

Migrations live in `app/migrations/` and are applied automatically on startup
(`PostgresStateStore.apply_migrations`). They are idempotent and additive.

Tables: `workflows`, `tasks`, `task_events`, `notifications`, `schema_migrations`.

### Supabase (Transaction pooler)

Connection string is read from `DATABASE_URL`, falling back to `SUPABASE_DB_URL`.
Set it in the Render dashboard (or local `.env`) — never in code.

The **Transaction pooler** (PgBouncer, port `6543`) does not support server-side
prepared statements. psycopg 3 prepares statements automatically after a few
executions, which breaks under PgBouncer, so the store sets
`prepare_threshold=None` when `DATABASE_TRANSACTION_POOLER=true` (the default).
For a direct/session connection (port `5432`) set it to `false`.

Use the pooler connection string with a URL-encoded password. Because the
pooler multiplexes connections, keep `DATABASE_POOL_MAX_SIZE` small (the
default is 5).

## Render deployment

`render.yaml` is a Blueprint for a free web service. Set `OPENHANDS_CLOUD_API_KEY`,
`DATABASE_URL`, `JR_BRIDGE_TOKEN`, and `HEARTBEAT_URL` as **secret** env vars in
the Render dashboard.

**Render Free caveat:** free instances spin down when idle and are not a
guarantee of 24/7 background processing. The dispatcher is therefore designed to
rebuild all state from PostgreSQL on every start; it never relies on a single
long-lived process.

## Sr → Jr bridge (MCP)

A Sr conversation spawns Jr children through a real MCP server mounted on this
service at `MCP_MOUNT_PATH` (default `/mcp`), using the **streamable-HTTP**
transport. The server exposes one tool, `create_jr_task`.

```
Sr conversation (OpenHands sandbox)
  └── MCP client  ──HTTPS──▶  POST /mcp  (app/mcp_server.py)
                                    └── Orchestrator.create_jr_task()
                                          └── StateStore.create_task(role=JR)
```

**Wiring.** When the dispatcher starts a Sr conversation it attaches the MCP
config (`app/bridge_config.py`) built from `MCP_PUBLIC_BASE_URL`, plus a
`system_message_suffix` that carries a **scope token**. Jr conversations get
neither, so a Jr cannot spawn grandchildren.

**Scope tokens** (`app/scope.py`). A shared bearer token alone would let any Sr
act on any workflow. Instead the dispatcher mints an HMAC-SHA256 token binding
`workflow_id` + `task_id` (+ expiry) and embeds it in the Sr's system message.
Every `create_jr_task` call verifies that token *and* re-checks the parent task
in the database, so a token for one workflow cannot create children in another.

**Rules enforced in one place** (`app/orchestrator.py`, shared by both
transports):

* `JR_ALLOWED_REPOSITORIES` — comma-separated `owner/repo` allowlist. **Empty
  means deny all** (fails closed).
* `JR_MAX_CHILDREN_PER_WORKFLOW` — hard cap per Sr task.
* Idempotency — a repeated call with the same key returns the existing child.

**Auth.** `MCP_BEARER_TOKEN` (if set) protects `/mcp`; `JR_BRIDGE_TOKEN`
protects `POST /jr/tasks`. `JR_SCOPE_SECRET` must be set for the bridge to
authorise tool calls.

## Deployment notes

* **Render Free** spins down when idle and is not 24/7. All state lives in
  PostgreSQL and is rebuilt on every start.
* **Supabase Transaction pooler** (port `6543`) — see below.
* **Better Stack** — `HEARTBEAT_URL` is fired only after a fully successful
  dispatch cycle. No monitor is configured yet.

## Not yet verified

1. **Supabase credentials.** No real Supabase project has been connected; only
   local PostgreSQL has been exercised. The Transaction-pooler code path
   (`prepare_threshold=None`) is unit-tested but not run against a live pooler.
2. **Live OpenHands Cloud.** All OpenHands API interaction is exercised through
   a fake client; no end-to-end run against the real API has been performed.
3. **Better Stack.** No monitor configured.
