# Deployment and migrations

This document is the explicit, reproducible procedure for deploying the
dispatcher and for applying database migrations. The application never applies
migrations itself; a human or a release step must.

## What the service is

A single stdlib HTTP server, started as a module:

```
python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN
```

It holds one background loop that runs, every `--poll-interval` seconds:

1. `SrRuntime.tick()` — services Sr client-tool calls,
2. `CloudDispatcher.tick()` — dispatches Jr tasks to Cloud conversations,
3. `WorkflowVerifier.tick()` — writes workflow terminal states.

Run **exactly one instance**. The loop is not partitioned between instances and
the in-process Cloud key is shared; horizontal scaling is not supported.

## Required environment variables

Read from the source, not assumed. Names must match exactly.

| Variable | Required | Read by | Purpose |
|---|---|---|---|
| `SUPABASE_DB_URL` (or `DATABASE_URL`) | yes | `cloud_store.py`, `web_server.py` | PostgreSQL/Supabase DSN. Missing → exit code 2. |
| `OPENHANDS_API_KEY` | yes (for Cloud) | `cloud_client.py` | Bearer key for Cloud V1. |
| `WEB_AUTH_TOKEN` | yes | `web_server.py` | Bearer token guarding writes; required to bind `0.0.0.0`. |
| `OPENHANDS_CLOUD_URL` | no | `cloud_client.py` | Override for a self-hosted Cloud. Default `https://app.all-hands.dev`. |
| `OPENHANDS_AGENT_PROFILE_ID` | no | `web_server.py` | Select an account agent profile by id. Unset = inherit the account's active profile. |
| `TEST_SUPABASE_DB_URL` | tests only | `tests/` | DSN for the test suite. |

Never commit these values. Set them in the Render dashboard (or your secret
store). The server logs neither the Cloud key nor the DSN.

## Build

```
pip install -r requirements.txt
```

`requirements.txt` lists the only third-party dependency: `psycopg[binary]`.

## Migrations

Migrations are plain SQL under `migrations/` and are **idempotent**. Apply them
in order, before the first start and after any pull that adds a file:

```
psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql
psql "$SUPABASE_DB_URL" -f migrations/0002_sr_runtime.sql
psql "$SUPABASE_DB_URL" -f migrations/0003_tool_call_recovery.sql
```

Do not run migrations against production without authorization. There is no
automatic migration on boot by design, so a schema change is always a
deliberate, reviewable step.

## Render

`render.yaml` describes the web service. It does not contain secret values; the
three required variables are marked `sync: false` and must be set in the
dashboard.

## Account capabilities (read-only)

`GET /api/capabilities` reports the account configuration the dispatcher will
inherit: whether an LLM key is set, the configured MCP server names, the active
agent profile, marketplace counts and provider-token presence. It changes
nothing and returns no secret values. It distinguishes **configured** (present
in settings) from **available** (loaded into a specific conversation, via
`GET /api/v1/app-conversations/{id}/skills`). Only an observed tool call proves
a capability was **exercised**.
