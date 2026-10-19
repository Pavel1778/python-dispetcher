# OpenHands Cloud Dispatcher (Python)

Autonomous Sr/Jr task dispatcher for [OpenHands Cloud](https://app.all-hands.dev),
designed to run on **Render Free** with durable state in **PostgreSQL/Supabase**.

> Status: **scaffold / pre-production**. Unit and store-contract tests pass.
> Supabase, Render, and Better Stack are **not yet wired**. See "Blockers".

## Architecture

```
FastAPI (app/main.py)
  ├── GET  /health          liveness only (process is up)
  ├── GET  /ready           dependency readiness (DB + API key + last cycle)
  └── POST /jr/tasks        protected bridge: Sr spawns a Jr child task

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

## Render deployment

`render.yaml` is a Blueprint for a free web service. Set `OPENHANDS_CLOUD_API_KEY`,
`DATABASE_URL`, `JR_BRIDGE_TOKEN`, and `HEARTBEAT_URL` as **secret** env vars in
the Render dashboard.

**Render Free caveat:** free instances spin down when idle and are not a
guarantee of 24/7 background processing. The dispatcher is therefore designed to
rebuild all state from PostgreSQL on every start; it never relies on a single
long-lived process.

## Blockers (not yet done)

1. **Jr bridge transport.** The bridge endpoint exists and is token-protected,
   but a Sr conversation has no wired way to *call* it yet (no MCP server or
   tool registration). Until that exists, Sr plans are not executed as Jr work.
   Workflows needing Jr children should end up `BLOCKED`, not `COMPLETED`.
2. **Supabase credentials.** No real Supabase project is connected; only local
   PostgreSQL has been exercised.
3. **Better Stack.** `HEARTBEAT_URL` is supported and fired only after a fully
   successful dispatch cycle, but no monitor is configured.
