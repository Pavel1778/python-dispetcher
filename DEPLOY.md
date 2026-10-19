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

## Authentication model

There are two independent authentication paths, and neither weakens the other:

| Path | Who | Secret | Protects |
|---|---|---|---|
| Browser login | the human owner | `AUTH_USERNAME` + `AUTH_PASSWORD_HASH`, session cookie signed with `AUTH_SESSION_SECRET` | every page and every API read/write |
| Machine API | a script or integration | `WEB_AUTH_TOKEN` (Bearer) | API reads and writes, without a browser |

* The owner's password is **never** stored; only a PBKDF2-SHA256 hash with a
  random salt (`pbkdf2_sha256$<iters>$<salt>$<hash>`). 600 000 iterations by
  default.
* Sessions live server-side in the `sessions` table. Only
  `HMAC-SHA256(AUTH_SESSION_SECRET, token)` is stored, so a database dump does
  not yield usable cookies. Logout deletes the row, which is immediate.
* State-changing requests from a browser need the `X-CSRF-Token` header (the
  token is returned by `GET /api/session`). A Bearer client is exempt, because
  it does not rely on a cookie and so cannot be tricked by a cross-site form.
* Failed logins are rate limited per (username, IP); after 5 failures the pair
  is locked for 15 minutes. The stored key is a hash, so the table holds no
  readable usernames or IP addresses.
* A wrong username and a wrong password give the same status and the same
  message, and take the same time, so the login page cannot be used to discover
  whether an account exists.

## Required environment variables

Read from the source, not assumed. Names must match exactly.

| Variable | Required | Read by | Purpose |
|---|---|---|---|
| `SUPABASE_DB_URL` (or `DATABASE_URL`) | yes | `cloud_store.py`, `web_server.py` | PostgreSQL/Supabase DSN. Missing → exit code 2. |
| `OPENHANDS_API_KEY` | yes (for Cloud) | `cloud_client.py` | Bearer key for Cloud V1. |
| `AUTH_USERNAME` | yes | `web_auth.py` | Owner login name. |
| `AUTH_PASSWORD_HASH` | yes | `web_auth.py` | PBKDF2 hash of the owner password. |
| `AUTH_SESSION_SECRET` | yes | `web_auth.py` | Separate secret that signs sessions. |
| `WEB_AUTH_TOKEN` | yes | `web_server.py` | Bearer token for the machine API. |
| `OPENHANDS_CLOUD_URL` | no | `cloud_client.py` | Override for a self-hosted Cloud. Default `https://app.all-hands.dev`. |
| `OPENHANDS_AGENT_PROFILE_ID` | no | `web_server.py` | Select an account agent profile by id. Unset = inherit the account's active profile. |
| `WEB_COOKIE_SECURE` | no | `web_server.py` | Force the `Secure` cookie flag on/off. Default: on when `X-Forwarded-Proto: https`. |
| `APP_VERSION` / `RENDER_GIT_COMMIT` | no | `web_server.py` | Shown on the System screen. |
| `TEST_SUPABASE_DB_URL` | tests only | `tests/` | DSN for the test suite. |

The server **refuses to bind `0.0.0.0`** unless `WEB_AUTH_TOKEN` is set **and**
the three `AUTH_*` variables are present, so a public deploy can never come up
without a way to authenticate. On localhost it starts read-only-ish for
development.

Never commit these values. Set them in the Render dashboard (or your secret
store). The server logs neither the Cloud key nor the DSN, and no API response
contains a secret.

### Generating the two auth secrets

Once, locally:

```
python -m orchestration.web_auth secret
# paste the output into AUTH_SESSION_SECRET

python -m orchestration.web_auth hash
# prompts twice (input hidden), prints the hash for AUTH_PASSWORD_HASH
```

The password is read from a prompt, never from the command line, so it does not
end up in your shell history. The same password verifies only against that hash.

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
psql "$SUPABASE_DB_URL" -f migrations/0004_auth.sql
```

Do not run migrations against production without authorization. There is no
automatic migration on boot by design, so a schema change is always a
deliberate, reviewable step.

The server checks the schema on startup and refuses to start if a required
table is missing, printing the names:

```
database is missing tables (apply migrations/): sessions, login_attempts
```

This turns a forgotten migration into one clear message at boot instead of a
failed request later.

## Render

`render.yaml` describes the web service. It does not contain secret values; the
required variables are marked `sync: false` and must be set in the dashboard.
The health check path is `/healthz`, which returns `{"status": "ok"}` and
nothing else.


## Account capabilities (read-only)

`GET /api/capabilities` reports the account configuration the dispatcher will
inherit: whether an LLM key is set, the configured MCP server names, the active
agent profile, marketplace counts and provider-token presence. It changes
nothing and returns no secret values. It distinguishes **configured** (present
in settings) from **available** (loaded into a specific conversation, via
`GET /api/v1/app-conversations/{id}/skills`). Only an observed tool call proves
a capability was **exercised**.
