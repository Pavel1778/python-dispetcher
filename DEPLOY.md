# Deployment and migrations

This document is the explicit, reproducible procedure for deploying the
dispatcher and for applying database migrations. By default the application
automatically applies migrations at startup; set `AUTO_MIGRATE=false` to manage
them out of band. Review and back up the database before deployment.

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
* Two in-memory ceilings back that up, because the client IP is not fully
  trustworthy: per IP (`LOGIN_MAX_ATTEMPTS_PER_IP`, default 30/min) and
  endpoint-wide (`LOGIN_MAX_ATTEMPTS_GLOBAL`, default 120/min). The global one
  cannot be moved by any request header.
* The client IP is taken from the rightmost trusted hop of `X-Forwarded-For`,
  not the leftmost value, which a caller controls. Set `TRUSTED_PROXY_HOPS` to
  the number of proxies you actually run behind (default 1). A client that
  rotates or repeats the header therefore cannot pick its own rate-limit key.
* Expired sessions and stale rate-limit rows are swept periodically by the
  background loop, so neither table grows without bound.
* A wrong username and a wrong password give the same status and the same
  message, and take the same time, so the login page cannot be used to discover
  whether an account exists.

## Client IP behind Render (unconfirmed hop count)

`TRUSTED_PROXY_HOPS` decides which `X-Forwarded-For` entry is trusted, so it is
a security setting, not a tuning knob. **The correct value for this Render
service is not confirmed.**

What is confirmed from official sources:

* Cloudflare (which fronts Render, confirmed by the live response headers
  `server: cloudflare` and `cf-ray`) *appends* to `X-Forwarded-For` rather than
  replacing it, and writes the client IP to `CF-Connecting-IP`, which it does
  overwrite (Cloudflare Fundamentals, "HTTP request headers").
* Render does **not** strip an incoming `X-Forwarded-For`; it only appends
  (Render feedback thread "Send the correct X_FORWARDED_FOR", and a Render
  community thread where a user reproduced the spoof). Render's own DDoS article
  says, verbatim: "Because traffic passes through Cloudflare and Render's load
  balancers, your app sees the proxy's IP by default. To get the real client IP,
  read the `x-forwarded-for` header." Two layers are named there, but the number
  of header entries they add is still not stated.

What is **not** confirmed:

* The exact number of `X-Forwarded-For` entries Render adds for this service.
  Render publishes no hop count, and Cloudflare + the Render load balancer means
  the real number is likely 2, not 1.

Consequences to weigh:

* `TRUSTED_PROXY_HOPS=1` (the default) reads the entry Render's load balancer
  appended. A caller can still prepend entries, but they are ignored, so a
  rotating header no longer changes the key. This is the safe default and is
  what the login throttle relies on.
* Setting it **too high** is the dangerous direction: with 2 configured against
  a 1-hop chain the resolver refuses the short chain and falls back to the socket
  peer, but if the chain ever matches the wrong count the resolver can return a
  caller-supplied entry. Never raise it above the number of proxies you actually
  operate.
* The endpoint-wide `LOGIN_MAX_ATTEMPTS_GLOBAL` ceiling does not depend on the IP
  at all, so it still caps login guessing even if this value is wrong.

How to confirm the value (read-only, no spoofed headers to production): log the
resolved IP once behind the real edge for a known client, or check Render's
request logs. Until that is done, treat `1` as a conservative default, not a
verified fact.

## Required environment variables

Read from the source, not assumed. Names must match exactly.

| Variable | Required | Read by | Purpose |
|---|---|---|---|
| `SUPABASE_DB_URL` (or `DATABASE_URL`) | yes | `cloud_store.py`, `web_server.py` | PostgreSQL/Supabase DSN. Missing → exit code 2. |
| `AUTO_MIGRATE` | no | `cloud_store.py` | Default true. Apply the idempotent migrations on first connect; `false` manages the schema out of band. |
| `PSYCOPG_PREPARE` | no | `cloud_store.py` | Server-side prepared statements are off by default. Set to `session` only on a direct (session-mode) connection; never behind a pooler. |
| `OPENHANDS_API_KEY` | yes (for Cloud) | `cloud_client.py` | Bearer key for Cloud V1. |
| `NEXUS_SECRET_KEY` | to store accounts | `secrets.py` | Fernet key that encrypts stored GitHub PATs and OpenHands API keys. Without it, adding an account is refused rather than writing plaintext. |
| `AUTH_USERNAME` | yes | `web_auth.py` | Owner login name. |
| `AUTH_PASSWORD_HASH` | yes | `web_auth.py` | PBKDF2 hash of the owner password. |
| `AUTH_SESSION_SECRET` | yes | `web_auth.py` | Separate secret that signs sessions. |
| `WEB_AUTH_TOKEN` | yes | `web_server.py` | Bearer token for the machine API. |
| `GITHUB_TOKEN` | no | `github_client.py` | Server-side GitHub token for repository discovery; if write access is separately enabled, its GitHub permissions also bound confirmed writes. Never returned by the API or UI. |
| `GITHUB_ENABLE_WRITES` | no | `web_server.py` | Default false. Enables the session-only, CSRF-protected, two-step GitHub repository actions; see [docs/DEVELOPMENT-LAB.md](docs/DEVELOPMENT-LAB.md). |
| `OPENHANDS_CLOUD_URL` | no | `cloud_client.py` | Override for a self-hosted Cloud. Default `https://app.all-hands.dev`. Must be `http(s)`: any other scheme is refused at startup. |
| `OPENHANDS_TRANSPORT` | no | `cloud_direct.py` | `direct` (default) provisions a sandbox and drives its agent-server; `app` uses the legacy app-conversation endpoint. See "Cloud transport" below. |
| `OPENHANDS_DEFAULT_MODEL` | no | `cloud_direct.py` | Model forced on every sandbox conversation. Set it when a bare sandbox's default model has no key. |
| `OPENHANDS_AGENT_PROFILE_ID` | no | `web_server.py` | Select an account agent profile by id. Unset = inherit the account's active profile. |
| `WEB_COOKIE_SECURE` | no | `web_server.py` | Force the `Secure` cookie flag on/off. Default: on when `X-Forwarded-Proto: https`. |
| `TRUSTED_PROXY_HOPS` | no | `web_server.py` | Number of trusted proxies in front of the app, used to pick the client IP from `X-Forwarded-For`. Default `1`. Set `0` to ignore the header entirely (direct connection, no proxy). A missing, empty or unparsable value falls back to `1`; a negative value is clamped to `0`. **The correct value for this Render service is not confirmed** — see "Client IP behind Render" below. |
| `LOGIN_MAX_ATTEMPTS_PER_IP` | no | `web_server.py` | In-memory per-IP login ceiling per minute. Default `30`. |
| `LOGIN_MAX_ATTEMPTS_GLOBAL` | no | `web_server.py` | In-memory endpoint-wide login ceiling per minute, independent of the client IP. Default `120`. |
| `APP_VERSION` / `RENDER_GIT_COMMIT` | no | `web_server.py` | Shown on the System screen. |
| `TEST_SUPABASE_DB_URL` | tests only | `tests/` | DSN for the test suite. |
| `GITHUB_API_URL` | no | `github_client.py` | Override the GitHub REST base URL (default `https://api.github.com`). Must be `http(s)` with a host and no embedded credentials, or construction raises. |
| `LOCAL_AGENT_SERVER_URL` | dev only | `local_runtime.py` | Base URL of a local agent-server for the local end-to-end scripts. Default `http://127.0.0.1:60000`. |
| `OHP_LOCAL_LLM_MODEL` | dev only | `local_runtime.py` | Model the local runtime asks the local agent-server to use. |
| `SESSION_API_KEY` | dev only | `openhands_client.py` | Session key for the local agent-server adapter. Not used by the Cloud transports. |

This table is the deploy-time subset. The complete list — the OpenHands account
pool, the autonomous-lab and durable-run settings, the provider cascade, the
evidence keyring and the SSRF allow-list — is in
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md), which names the default and the
module that reads each one and is kept in step with `.env.example`.

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
# ... every file in migrations/, in lexical order, e.g. with a shell loop:
for f in $(ls migrations/*.sql | sort); do psql "$SUPABASE_DB_URL" -f "$f"; done
```

The migrations are `0001`–`0011`, `0014`–`0032`. `0012` and `0013` are
intentionally unused (reserved for the deferred File Lab, D-1). The runner
applies every `*.sql` in lexical order and does not require a contiguous
sequence, so the gap is safe. The set includes the auth tables (`0004`), the
cloud-direct route table (`0007`), the NEXUS projects/plans/files increments
(`0009`–`0011`), the D-2 project binding (`0014`), the workspace lock (`0015`),
the RLS on the sandbox session-key tables (`0016`), the context/memory tables
(`0017`), the lab runs (`0018`), the GitHub/OpenHands accounts (`0019`, `0020`,
`0022`, `0026`, `0027`), the specifications (`0021`), the worker heartbeat
(`0023`), the task hand-off/preferred-account/time-limit columns (`0024`,
`0025`), the lease fencing and abandoned-conversation columns (`0028`, `0029`),
and the durable-run/connection/project-repository tables (`0030`–`0032`).

`0014_nexus_project_binding.sql` is additive: it adds the project↔repository
link table, nullable `project_id` on `tasks`/`workflows`, the canonical
repository-identity trigger and the `nexus_evidence` table. It does **not**
backfill legacy rows; a legacy task/workflow keeps `project_id IS NULL`.

`0015_task_workspace_lock.sql` adds the partial unique index that serializes
repository work across processes. `0016_secret_tables_rls.sql` locks the sandbox
session-key tables. Both are additive and idempotent.

### Backup and restore

Before applying a new migration to a database with real data, back it up:

```
pg_dump "$SUPABASE_DB_URL" --format=custom --file=nexus-$(date +%F).dump
```

Restore into a **disposable** database and verify the schema guard, then point
the service at it:

```
createdb nexus_restore
pg_restore --dbname=nexus_restore --no-owner nexus-2026-01-01.dump
psql "postgresql://.../nexus_restore" -c "SELECT filename FROM schema_migrations ORDER BY filename;"
```

`tests/test_migration_d2.py` exercises the apply/idempotency/legacy-preservation
properties against a disposable database; it is the automated proxy for a
restore drill, not a substitute for one against a real backup.

### Repository restore points

Before changing the code, take a restore point — a self-contained git bundle of
every ref plus a JSON record of the exact HEAD and working-tree state:

```
python -m orchestration.cli restore-point create --label "before <change>"
python -m orchestration.cli restore-point verify .restore-points/<name>.json
```

They are written under `.restore-points/` (gitignored, never pushed). A restore
point is a git bundle, so it restores the whole history offline:

```
git clone .restore-points/nexus-restore-<stamp>-<head12>.bundle /tmp/restored
```

A bundle cannot carry uncommitted changes; when the tree is dirty the record
also holds the patch of tracked changes and the names of untracked paths.

Do not run migrations against production without authorization. Applying them on
boot is still a deliberate step: it happens once per file, the SQL is
idempotent, and `AUTO_MIGRATE=false` turns it off for environments that manage
the schema out of band.

## Connection pooling

Server-side prepared statements are **off by default** (`prepare_threshold=None`).
That is the safe choice behind any transaction-mode pooler: Supabase's PgBouncer
on port 6543, and any other pooler that reuses a server session. The failure it
prevents is not the missing-statement one, it is the opposite, and it only shows
up on a restart:

1. The process starts, the poll loop runs one statement more than psycopg's
   default threshold (5), and psycopg prepares it server-side as `_pg3_0`.
2. The process is recycled -- a Render free-plan cold start, a deploy, a crash.
   The pooler does not reset the server session, so `_pg3_0` is still present on
   the backend.
3. The next start runs the same statement, psycopg decides to prepare it and
   generates `_pg3_0` again, and PostgreSQL rejects it with
   `DuplicatePreparedStatement: prepared statement "_pg3_0" already exists`.

Because the first start is fine, this looks like a random startup failure rather
than a pooling bug. Reproduced against a real PgBouncer in `pool_mode=transaction`
with `max_prepared_statements=0` (newer PgBouncer manages prepared statements
itself and hides the bug; Supabase's pooler does not): two consecutive clients
running the same query produced exactly `prepared statement "_pg3_0" already
exists`, and `prepare_threshold=None` made it disappear.

Preparation is a pure win on a direct session-mode connection, so it can be
re-enabled there with `PSYCOPG_PREPARE=session`. Do not set it behind a pooler.
The default is asserted by `tests/test_schema_bootstrap.py`.

## Schema check at startup

With `AUTO_MIGRATE=false` the schema is managed out of band, so a forgotten
migration would otherwise surface as `UndefinedTable` on the first request. The
server instead checks the schema on startup and refuses to start if a required
table is missing, printing the names:

```
database is missing tables (apply migrations/): sessions, login_attempts
```

This checks the legacy core tables and columns only, not every NEXUS table or
column. The full required set (`REQUIRED_COLUMNS` in `cloud_store.py`) is
checked by `GET /readyz` and by the diagnostics `schema` check; run those after
applying migrations to confirm the new NEXUS tables are present too.

## Render

### Autonomous runs: who advances them

The web server advances autonomous runs itself, on a thread of its own. Nothing
has to be configured: a run started from the studio begins its first task
within seconds, and the «Агент» tab and `GET /api/studio` (`lab.inline`) say
that the server does this.

All state is in PostgreSQL and every claim is a lease, so a restart continues
where the last pass stopped, and closing the browser stops nothing.

**The free plan stops the process after about 15 minutes without a request.**
The loop stops with it. Conversations already started go on in OpenHands Cloud
and their results are collected when the service wakes; no new task starts
until then. While the studio is open in a visible tab on the conversation of a
working run, the page asks for the run's state every five seconds, and that
keeps the service awake; a tab in the background does not ask. For a run that
has to work unattended, use a paid instance, or have something outside request
`/healthz` every few minutes.

A separate worker is optional - for a deployment that wants the web process to
serve requests only:

```
python -m orchestration.lab_worker --worker-id lab-1 --poll-interval 5
```

It shares `SUPABASE_DB_URL` with the web service, holds no per-run state and
can be restarted or scaled to N at any time. Set `NEXUS_LAB_INLINE=0` on the
web service if only the worker should advance runs; left as it is, both do, and
the leases keep them apart. `--once` runs a single pass (a cron-style
deployment).

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_LAB_INLINE` | on | `0`, `false`, `no` or `off`: the web server does not advance runs |
| `OPENHANDS_TRANSPORT` | `direct` | `direct` gives every agent the whole task text as a file; `app` cannot place a file (see below) |
| `NEXUS_TASK_TIMEOUT_SECONDS` | 7200 | how long one task's conversation may run before the task is given up (it was 1800) |
| `NEXUS_SPEC_MAX_CHARS` | 1 000 000 | longest task text accepted |
| `NEXUS_SPEC_INLINE_CHARS` | 20 000 | up to this, the text travels inside the agent's first message |
| `NEXUS_SPEC_MESSAGE_CHARS` | 60 000 | `app` transport only: up to this, the whole text is still in the message; longer, each task gets the part it was planned from |
| `NEXUS_SPEC_STAGE_TOKENS` | 24 000 | size of one planning part |
| `NEXUS_SPEC_SANDBOX_DIR` | - | `direct` transport: the one directory to place the text in |
| `<PROVIDER>_CONTEXT_TOKENS` | per provider | the model's window, used to size planning parts |

**OpenHands limits.** The account has a daily limit on new conversations. A
run that reaches it is not failed: the task waits and is tried again (every 15
minutes for a daily limit), and its line in the «Агент» tab says so. A run of
more tasks than the limit allows takes more than a day; its deadline is 24
hours unless the request sets another.

**What a run does to the repository.** Each task's agent is told to push to
the run's branch, and a task counts only when the head of that branch on GitHub
is a commit the agent's report names. Choose the branch with that in mind. One
task at a time works on a branch: a second run on the same branch waits its
turn task by task, and a push by anyone else while a task is at work can leave
that task unverified.

`render.yaml` also defines a worker service (`python-dispetcher-lab-worker`)
alongside the web service. Whoever advances runs registers a heartbeat
(`nexus_lab_workers`: kind `worker` for the separate process, `inline` for the
web server's own loop), so `GET /api/openhands/diagnostics` can tell a live
loop from a dead one — a lease alone cannot. See
[docs/WORKER_SETUP.md](docs/WORKER_SETUP.md).

Client packages (`packaging/`): a `.deb` (Debian/Linux), a signed `.apk`
(Android) and a `.exe` (Windows). All three are thin clients that open the same
backend URL and store no credential; build them with the scripts in
`packaging/*/build-*.sh`.


`render.yaml` describes the web service. It does not contain secret values; the
required variables are marked `sync: false` and must be set in the dashboard.
The health check path is `/healthz`, which returns `{"status": "ok"}` and
nothing else.

The blueprint uses the **free** plan, which means:

* the instance spins down after roughly 15 minutes idle and cold-starts on the
  next request, so the background loop is not continuously running;
* there is no shell and no persistent disk — all state is in PostgreSQL;
* run exactly one instance: the loop is not partitioned between instances.


## Cloud transport: `direct` and `app`

The dispatcher can start Cloud conversations two ways. `OPENHANDS_TRANSPORT`
selects which; the default is `direct`.

**`direct` (default).** Provision a sandbox yourself
(`POST /api/v1/sandboxes`), wait for it to be `RUNNING`, then create the
conversation on that sandbox's agent-server with the sandbox's own
`session_api_key`. Each conversation is bound to its sandbox in the
`conversation_routes` table, so a read from a later process still finds it.

**`app` (legacy).** `POST /api/v1/app-conversations`, which provisions a sandbox
*and* creates the conversation for you, as a two-phase start task.

**Caution:** the `direct` transport has no native `selected_repository` field
on the agent-server create call, so a repository-scoped task is handled by
instruction, not by a verified workspace selection. `OPENHANDS_DIRECT_REPO_MODE`
decides how: `prompt` (default) prepends a deterministic clone step, `refuse`
dead-letters the task, `allow` ignores the field. Sandbox reuse is now scoped to
the repository and branch a conversation was started for (`workspace_key`), so a
task on another repository or branch gets a sandbox of its own and never finds
someone else's working tree; tasks on one branch share a sandbox but never run
at the same time (see the workspace lock below). See
[docs/OPENHANDS_SETUP.md](docs/OPENHANDS_SETUP.md) and [docs/NEXUS-SPEC.md](docs/NEXUS-SPEC.md).
The `app` endpoint can fail with
`HTTP 500` on the live service when it asks a just-provisioned sandbox to create
a conversation: the start task reaches `STARTING_CONVERSATION` and then errors,
and a start without an initial message is `READY` but `send-message` returns
`502`. When that happens the whole dispatcher is inert, because nothing can
reach a running conversation. The `direct` transport avoids that inner call
entirely and was verified end to end against the live service.

Two consequences of `direct`:

* A bare sandbox may default to a model whose key is not present, and a
  conversation on it then fails with an LLM auth error. Set
  `OPENHANDS_DEFAULT_MODEL` to a model the sandbox can reach.
* Get-or-create reuses one `RUNNING` sandbox across conversations to avoid
  provisioning a sandbox per task. A conversation that needs its own client
  tools (that is, the Sr conversation) gets its own sandbox, so a long-running
  Sr cannot share a sandbox with a Jr that is being reset between tasks.

Set `OPENHANDS_TRANSPORT=app` to fall back without a code change.

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

* For discovery, grant only repository read access. Optional confirmed create/
  visibility operations require additional GitHub permissions and explicit
  `GITHUB_ENABLE_WRITES=true`; see [docs/DEVELOPMENT-LAB.md](docs/DEVELOPMENT-LAB.md).
* The token is read from the environment when the client is constructed, sent as a bearer header,
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

## Operator controls on a workflow

Every workflow is inspectable and steerable from its detail panel on the
**Очередь и задачи** screen. Each action is also a JSON API call, and the UI
offers only the actions the workflow's current state can actually accept, so
there are no buttons that fail by design.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/workflows/{id}/message` | Send the Sr agent a message. |
| `POST` | `/api/workflows/{id}/cancel` | Stop the workflow for good. |
| `POST` | `/api/workflows/{id}/resume` | Reopen a blocked or failed workflow. |
| `POST` | `/api/workflows/{id}/rename` | Rename the Sr conversation in Cloud. |
| `POST` | `/api/tasks/{id}/retry` | Requeue one Jr task for another attempt. |

How each behaves, and why:

* **cancel** is terminal and idempotent. It moves the workflow to `cancelled`,
  which the Sr loop and the verifier both skip, and cancels its in-flight child
  tasks so the Jr dispatcher stops polling their conversations. The Sr
  conversation is **not** deleted: the transcript is the record of why the work
  stopped. A second cancel is a no-op, so a double click is harmless.
* **resume** is for a workflow that stopped and needs a human nudge: it clears
  the Sr start backoff and returns the workflow to `queued`, then requeues the
  child tasks that still have attempts left. A task that exhausted its
  `max_attempts` is left as it is, because resuming the workflow is not consent
  to spend an attempt the task does not have. Resuming a `completed` or
  `cancelled` workflow is a `409`, not a silent no-op.
* **rename** applies the title to the real Cloud conversation (`PATCH
  /api/v1/app-conversations/{id}`) and only then reports success; it never
  changes the workflow's goal. A rename before the Sr conversation exists is a
  `400`.
* **retry** requeues one task through the state machine's legal path, so a task
  in a state with no route back to `queued` is refused with a `409` rather than
  forced.

### Repository and branch contention check

Two conversations editing one checkout can overwrite each other's commits. This
is prevented by a **real cross-process lock**, not only a read-then-check:

* Migration `0015_task_workspace_lock.sql` adds the partial unique index
  `uq_tasks_workspace_active` on `(selected_repository, COALESCE(selected_branch,''))`
  over the states `assigned|running|verifying|blocked|recovering`. `queued` is
  deliberately excluded, so two queued siblings do not block each other before
  either starts. Repository-less tasks (`selected_repository IS NULL`) never
  contend, because a NULL key is distinct in a unique index.
* `claim_task` claims inside a `SAVEPOINT` and treats a `UniqueViolation` as
  "workspace taken", so two dispatchers can never claim two different queued
  rows for one workspace in the same instant. The `_workspace_holder` read is
  only the fast path.
* On the `direct` transport, sandbox reuse is additionally scoped to the
  repository and branch a conversation was started for (`workspace_key`).

Two things remain true and are worth knowing: a task can still be started
against a branch something *outside* NEXUS is pushing to (a person, a bot), and
the direct transport selects the repository by instruction, not by a native
field — see [docs/OPENHANDS_SETUP.md](docs/OPENHANDS_SETUP.md) and
[docs/NEXUS-SPEC.md](docs/NEXUS-SPEC.md).
