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
| `GITHUB_TOKEN` | no | `github_client.py` | Read-only GitHub token used to discover repositories for the allowlist. Unset → repositories must be added by hand. Never returned by the API or the UI. |
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

Once, locally, in an interactive terminal (the `hash` command reads the password
from a prompt, so it cannot be piped or run from a script):

```
python -m orchestration.web_auth secret
# paste the output into AUTH_SESSION_SECRET

python -m orchestration.web_auth hash
# prompts twice (input hidden), prints the hash for AUTH_PASSWORD_HASH
```

The password is read from a prompt, never from the command line, so it does not
end up in your shell history. The same password verifies only against that hash.

The printed value must look like

```
pbkdf2_sha256$600000$<salt>$<digest>
```

four parts separated by three `$`, where `<salt>` and `<digest>` are standard
base64 (`+`, `/`, `=` allowed). `AUTH_PASSWORD_HASH` is the hash, never the
password.

### The `$` characters: the one thing that breaks this

`$` is special to a shell. Inside double quotes, a heredoc, or an unquoted
assignment, a shell expands `$600000` and friends and the value loses its
separators — that is why the log can say "does not look like a hash" for a hash
that was generated correctly. Rules:

* In the Render dashboard, paste the line verbatim. Render stores a literal
  string; there is nothing to escape and no quotes to add.
* In a shell, always single quotes, which do not expand `$`:

  ```
  export AUTH_PASSWORD_HASH='pbkdf2_sha256$600000$<salt>$<digest>'
  ```

* The `hash` command prints exactly that `export` line to stderr, so you can
  copy it instead of retyping the value.

### Verify the value that is actually set

Before deploying, confirm the value the server will read:

```
python -m orchestration.web_auth check
```

It reads `AUTH_PASSWORD_HASH` from the environment, prints `OK` with the
algorithm, iteration count and sizes, or `INVALID` with the specific problem and
the fix. It never prints the value. Add `--password` to also confirm that a
password matches the hash.

To check a value that is not in your environment, pass it on stdin with
`--stdin` (single-quoted, so the shell leaves the `$` alone):

```
printf '%s' 'pbkdf2_sha256$...' | python -m orchestration.web_auth check --stdin
```

`--stdin` is required whenever `AUTH_PASSWORD_HASH` is already set: without it
the environment wins and you would verify the old value, not the new one. Note
that `hash` reads the password from a prompt and therefore cannot be piped into
`check`; run `hash` first, copy the value it prints, then check that.

### Login fails with "Неверный логин или пароль"

That message means the submitted credentials did not match the configured ones.
Work through this in order:

1. Run `python -m orchestration.web_auth check`. It names the exact problem.
   `GET /api/system` returns the same reasons in `config.login_errors`, and a
   start-up refusal prints them too.
2. If the value lost its `$` separators, or is wrapped in quotes, or uses the
   URL-safe base64 alphabet (`-` and `_`), or is missing its `=` padding, the
   message says which. Re-export with single quotes and re-paste.
3. If `AUTH_PASSWORD_HASH` holds the password itself, or a hash from another tool
   (bcrypt, argon2, Django, `openssl passwd`), every login fails. Only the
   `pbkdf2_sha256$...` format from this repository is accepted.
4. Confirm `AUTH_USERNAME` matches exactly what you type. Leading and trailing
   spaces are trimmed on both sides, but the comparison is case-sensitive.
5. `AUTH_SESSION_SECRET` only signs the session. A wrong value breaks the
   *existing* session, not the login itself.

### Regenerating AUTH_PASSWORD_HASH on Render

Do this only when the existing value is unusable — `check` says `INVALID` and the
problem is not one of the quoting cases above. It replaces the password for the
owner login; nothing else changes.

1. Locally, in an interactive terminal:

   ```
   python -m orchestration.web_auth hash
   ```

   Type the new password twice. The command prints one line, the hash, and an
   `export ...='...'` line on stderr. It never prints the password back.

2. Verify before pasting. Copy the hash from step 1 and check it (the `--stdin`
   is needed when `AUTH_PASSWORD_HASH` is already set in your shell; otherwise
   plain `check` reads the environment):

   ```
   printf '%s' 'paste-the-hash-here' | python -m orchestration.web_auth check --stdin
   ```

   Expect `OK: stdin is a usable hash (...)`. Then paste the hash into Render.

3. In the Render dashboard, open the service → **Environment**, edit
   `AUTH_PASSWORD_HASH`, and paste the hash line verbatim as the value. Do not
   add quotes and do not escape the `$` characters. Save.

4. Render redeploys automatically on an environment change. If it does not,
   trigger **Manual Deploy → Deploy latest commit**.

5. Verify: the service log must not contain the "refusing to bind a public
   interface without login configured" message, and `GET /api/system` must show
   an empty `config.login_errors`. Then log in.

Neither the password, the hash nor the session secret is ever written to a log,
a response body or an error message; the messages name the variable and the
problem, never the value.

## Build

```
pip install -r requirements.txt
```

`requirements.txt` lists the only third-party dependency: `psycopg[binary]`.

## Migrations

Migrations are plain SQL under `migrations/` and are **idempotent**. The process
applies them itself on first connect, in filename order, and records each file's
checksum in `schema_migrations` so a restart does not re-run one. This is what
makes the Render free plan deployable at all: free instances have no shell, so
there is nowhere to run `psql` before the service starts.

To apply them by hand instead, set `AUTO_MIGRATE=false` and run:

```
psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql
psql "$SUPABASE_DB_URL" -f migrations/0002_sr_runtime.sql
psql "$SUPABASE_DB_URL" -f migrations/0003_tool_call_recovery.sql
psql "$SUPABASE_DB_URL" -f migrations/0004_auth.sql
psql "$SUPABASE_DB_URL" -f migrations/0005_sr_start_retry.sql
```

Do not run migrations against production without authorization. Applying them on
boot is still a deliberate step: it happens once per file, the SQL is
idempotent, and `AUTO_MIGRATE=false` turns it off for environments that manage
the schema out of band.

## Connection pooling

Set `DATABASE_TRANSACTION_POOLER=true` when `SUPABASE_DB_URL` points at
Supabase's transaction pooler (PgBouncer, port 6543). The pooler may hand the
same server connection to a different client between statements, so psycopg's
automatic server-side preparation (default: after the fifth execution) can fail
with `prepared statement ... does not exist`. The flag disables preparation
(`prepare_threshold=None`), which is asserted by
`tests/test_schema_bootstrap.py`.

## Schema check at startup

With `AUTO_MIGRATE=false` the schema is managed out of band, so a forgotten
migration would otherwise surface as `UndefinedTable` on the first request. The
server instead checks the schema on startup and refuses to start if a required
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

The blueprint uses the **free** plan, which means:

* the instance spins down after roughly 15 minutes idle and cold-starts on the
  next request, so the background loop is not continuously running;
* there is no shell and no persistent disk — all state is in PostgreSQL;
* run exactly one instance: the loop is not partitioned between instances.


## Account capabilities (read-only)

`GET /api/capabilities` reports the account configuration the dispatcher will
inherit: whether an LLM key is set, the configured MCP server names, the active
agent profile, marketplace counts and provider-token presence. It changes
nothing and returns no secret values. It distinguishes **configured** (present
in settings) from **available** (loaded into a specific conversation, via
`GET /api/v1/app-conversations/{id}/skills`). Only an observed tool call proves
a capability was **exercised**.

## GitHub repository discovery

The dispatcher only runs a task in a repository that is in its own allowlist.
Instead of typing `owner/name` by hand, set `GITHUB_TOKEN` and press
**Загрузить из GitHub** on the System screen; the same thing is available as an
API call:

```bash
curl -X POST -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  https://<service>/api/repositories/sync
```

What the token needs and how it is handled:

* Scope: read access to repositories (`repo` for private repositories, or
  `public_repo` for public only). The dispatcher never writes to GitHub.
* The token is read from the environment on each call, sent as a bearer header,
  and is **never** logged, returned by the API, or written to the database.
  `GET /api/system` and `GET /api/capabilities` report only whether it is set.
* Archived repositories are skipped: they are read-only on GitHub, so a task
  dispatched into one could never land a change.
* A sync refreshes the default branch and provider of repositories already in
  the list. It never re-enables one an operator disabled, and never clears an
  `allowed_branches` restriction. A GitHub outage leaves the allowlist as it
  was, because the whole list is fetched before anything is stored.

Endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/github/repositories` | Preview what a sync would import. Writes nothing. |
| `POST` | `/api/repositories/sync` | Import discovered repositories into the allowlist. |
| `POST` | `/api/repositories/remove` | Drop one repository from the allowlist. |

A `403` from GitHub with the rate-limit budget at zero is reported as a
transient problem (503), not as an auth failure: the token is valid and only the
quota has to reset. Rotating a working token would not help, so the dispatcher
does not ask for it.
