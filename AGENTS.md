# AGENTS.md

## What this repository is

`/workspace/platform` is the durable control plane for orchestrating OpenHands
agent conversations. It owns task state, leases, assignment and recovery.
It does not own the runtime: conversations execute in the OpenHands
agent-server.

## Layout

- `orchestration/control_plane.py` — SQLite task store: `submit_task`,
  `claim_task`, `transition`, `reclaim_expired`, `bind_conversation`,
  `set_result`. Enforces the state machine in `_ALLOWED`.
- `orchestration/openhands_client.py` — stdlib-only REST adapter for the
  agent-server. No external dependencies, on purpose.
- `orchestration/dispatcher.py` — one tick = reconcile → recover → advance →
  dispatch. This is the only place that decides task state from runtime
  signals.
- `INTEGRATION-MAP.md` — every confirmed API fact. Read it before changing the
  adapter.
- `CLOUD-INTEGRATION-MAP.md` — the same for the **Cloud V1** adapter. Read it
  before changing anything under `cloud_*`.
- `orchestration/cloud_client.py` — stdlib-only REST adapter for **OpenHands
  Cloud V1** (`/api/v1/app-conversations`, Bearer auth).
- `orchestration/cloud_store.py` — PostgreSQL/Supabase task store. Atomic claim
  via `SELECT ... FOR UPDATE SKIP LOCKED`; legal-path recovery via
  `force_transition`.
- `orchestration/cloud_dispatcher.py` — Cloud dispatcher. Two-phase create
  (start task → poll → bind), allowlist, bounded retry, recovery.
- `orchestration/sr_tools.py` — Sr operations (create/list/status/report/
  revise/accept/cancel) with real, durable effects.
- `migrations/0001_init.sql` — Supabase schema. Idempotent; apply with `psql`.
- `tests/fake_cloud.py` — a real local HTTP server speaking the Cloud V1
  surface. Cloud tests use it, not mocks of the client.
- `tests/fake_runtime.py` — a real local HTTP server speaking the confirmed
  v1.50.1 surface. Tests use it, not mocks of the client.
- `tests/e2e_*.py` — manual scripts against the real runtime. Not collected by
  pytest (`python_files = test_*.py`).

## Commands

```bash
# Tests (needs the venv that has pytest; the system python does not).
# Cloud tests need a PostgreSQL DSN; without it they skip cleanly.
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/db" \
/workspace/project/4119041ca5254315a7b6e95fdc85ae8a/scratch/sdk-main/.venv/bin/python \
  -m pytest tests/ -q -p no:cacheprovider

# Apply the Supabase schema (idempotent).
psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql

# Manual end-to-end against the live runtime (creates and deletes its own
# conversation; the three long-lived conversations are never touched)
/workspace/project/4119041ca5254315a7b6e95fdc85ae8a/scratch/sdk-main/.venv/bin/python \
  tests/e2e_dispatcher_loop.py
```

## Runtime facts that cost real debugging to learn

These were found by running against the live server, not from docs:

1. **`agent_settings` is required on create.** Without it: `422 ... One of
   agent, agent_settings, or agent_profile_id must be provided`. The dispatcher
   fetches it from `GET /api/settings` once and forwards it, so it never needs
   to hold an LLM key.
2. **Creation is idempotent only if you supply `conversation_id`.** The
   dispatcher derives it with `task_conversation_id()` (uuid5 of the task id).
   A restart then gets `200` and the existing conversation, not a duplicate.
   Do not switch to a random uuid without replacing the recovery logic.
3. **`?kind=ConversationErrorEvent` on `events/search` returns nothing** even
   when the event exists. Fetch unfiltered and match `classification` on the
   client.
4. **`GET /api/conversations/{id}/events` needs a body** and returns 422
   otherwise; use `/events/search` or `/events/count`.
5. **The runtime classifies its own failures.** `classification.retryable` is
   authoritative: `false` means retrying cannot succeed, so the task is
   dead-lettered at once. Do not retry auth/config errors.
6. **`POST /run` returning 409 means already running**, which is success.

## Invariants to keep

- A missing answer is never completion. `finished` with an empty final response
  is `blocked`, not `completed`.
- A transport error keeps the lease and does not increment `attempt`.
- One action per task per tick (`_touched`). Without it a persistently failing
  task spins inside a single tick.
- The API key is read from the environment and never logged.

## Environment note

The runtime at `127.0.0.1:60000` in this sandbox has **no working LLM key**
(auth fails). That is why the e2e scripts assert the dead-letter path. A
successful-completion e2e needs a valid key and is not yet exercised.

## Sr tool bridge and web UI

- Sr acts only through **client tools**: `start_conversation(..., client_tools=...)`,
  then poll `events/search` for `ActionEvent` and deliver results with
  `send-message`. See `orchestration/sr_runtime.py`.
- Client-tool names **must not collide with built-in tool names** (422). Sr tool
  names carry an `sr_` prefix for this reason. Do not rename them back.
- `workflow_id` is **never** taken from the model; the bridge substitutes the real
  one. Keep `workflow_id` out of `SrTools.tool_schemas()`.
- `orchestration/web_server.py` is the UI (`python -m orchestration.web_server`).
  Run tests with `TEST_SUPABASE_DB_URL` set; without a DB the DB-backed modules
  skip cleanly.
- The account has a **daily conversation limit**; probing schemas can exhaust it.
  Prefer the fake Cloud server in `tests/fake_cloud.py` for iteration.

## Account capability inheritance (Cloud path)

- The web path starts Cloud conversations with only the fields it needs and
  **omits** `agent_profile_id`, `agent_type`, `plugins`, `agent_settings` and
  `conversation_settings`, so each conversation inherits the account's own
  active agent profile and its configured MCP servers/skills. Never write these
  fields to reconfigure the account; they are opt-in via
  `OPENHANDS_AGENT_PROFILE_ID` / `SrRuntime(plugins=...)`.
- `GET /api/capabilities` (read-only) reports what the account has
  **configured** (settings: MCP names, marketplaces, LLM key presence, provider
  tokens). `GET /api/v1/app-conversations/{id}/skills` reports what a specific
  conversation has **available**. Neither proves a tool ran; only an observed
  `ActionEvent` proves it was **exercised**. Do not conflate the three.
- Client tools need no browser: `SrRuntime.tick()` polls `events/search`,
  claims by the stable event `id`, executes, and delivers with `send-message`.
  `tests/test_cloud_integration.py` proves this against the fake server.
- The workflow verifier now reads the Sr view *first*: an Sr in `error`/`stuck`
  fails the workflow, `waiting_for_confirmation` blocks it, and an Sr that
  stopped (or vanished) with no child work blocks it — none of these may hang
  forever or be reported as success.

## Deployment

- `requirements.txt` (only `psycopg[binary]`), `render.yaml`, `DEPLOY.md`.
  Start: `python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN`.
  Run exactly one instance. Migrations are manual and idempotent; the app never
  applies them.
