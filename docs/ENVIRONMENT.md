# Environment, dependencies and run commands

The single reference for what NEXUS reads from its environment, what it needs
installed, how each process is started, which permissions it requires, and how
to recover a deployment. It is kept in step with `.env.example` (the template
to copy) and with the code: the *read by* column names the module that reads the
variable, so a rename is caught by reading that file.

**No real secret belongs in this file, in `.env.example`, in `render.yaml`, in
`docker-compose.yml` or in any committed file.** Every value below is a name, a
default or a placeholder. Set real values in the deployment's secret store (the
Render dashboard, the systemd `EnvironmentFile`, or a local, gitignored `.env`).

---

## 1. Dependencies

| where | file | contents |
| --- | --- | --- |
| runtime | `requirements.txt` | `psycopg[binary]>=3.2,<4` and `cryptography>=42,<46` — the only third-party packages. Everything else is the standard library, on purpose. |
| tests / lint | `requirements-dev.txt` | `pytest==9.0.3`, `ruff==0.13.1`, and optional `playwright>=1.40,<2` for the browser E2E. |
| packaging | `pyproject.toml` | `requires-python >= 3.10`; the `nexus` console script (`orchestration.cli:main`); the studio assets are declared as `package-data`. |

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                 # run the service
pip install -r requirements.txt -r requirements-dev.txt   # develop / test
python -m playwright install --with-deps chromium         # optional, browser E2E
```

`psycopg[binary]` ships prebuilt wheels, so no libpq or compiler is needed. The
`cryptography` package provides Ed25519 for the Evidence Trust Engine and Fernet
for stored credentials; without it the evidence engine reports `BLOCKED` rather
than fabricating a verdict.

---

## 2. Run commands

```bash
# The single web instance (web UI + dispatcher + the inline lab loop).
python -m orchestration.web_server --host 0.0.0.0 --port "$PORT" --token "$WEB_AUTH_TOKEN"
# flags: --host (default 0.0.0.0) --port (8000) --poll-interval (5.0) --token (=$WEB_AUTH_TOKEN)

# The autonomous lab worker (a separate process; optional — the server runs the
# loop itself unless NEXUS_LAB_INLINE=0).
python -m orchestration.lab_worker --worker-id lab-1
# flags: --worker-id (lab-<pid>) --poll-interval (5.0) --lease-seconds (900) --once

# The MCP server over stdio (thin client over the same database).
python -m orchestration.mcp_server

# The CLI (thin client over the HTTP API; needs NEXUS_URL and NEXUS_TOKEN).
python -m orchestration.cli doctor
```

Migrations are applied by the process on first connect (`AUTO_MIGRATE=true`),
idempotently, in filename order. Set `AUTO_MIGRATE=false` to manage the schema
out of band; see `DEPLOY.md`.

### Health probes

- `GET /healthz` — liveness, a one-line JSON, no configuration.
- `GET /readyz` — liveness plus a real database round-trip.
- `GET /api/openhands/diagnostics` — the full diagnostic report (see
  `TROUBLESHOOTING.md`).

### Secret generation

```bash
python -m orchestration.web_auth secret          # -> AUTH_SESSION_SECRET
python -m orchestration.web_auth hash            # -> AUTH_PASSWORD_HASH (prompts, hidden)
python -m orchestration.web_auth check           # verify AUTH_PASSWORD_HASH without printing it
python -m orchestration.evidence_keygen --key-id nexus-lab   # -> NEXUS_EVIDENCE_* values
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # -> NEXUS_SECRET_KEY
```

`AUTH_PASSWORD_HASH` contains `$` separators: quote it with single quotes in a
shell, and double every `$` (`$$`) inside a Docker Compose `env_file`.

---

## 3. Environment variables

### 3.1 Core (server, database, auth)

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `SUPABASE_DB_URL` (or `DATABASE_URL`) | — | `cloud_store.py`, `web_server.py` | PostgreSQL DSN. Required. Missing → exit code 2. |
| `AUTO_MIGRATE` | `true` | `cloud_store.py` | Apply the idempotent migrations on first connect; `false` manages the schema out of band. |
| `PSYCOPG_PREPARE` | off | `cloud_store.py` | Server-side prepared statements. Set to `session` only on a direct session-mode connection; never behind a pooler. |
| `WEB_AUTH_TOKEN` | — | `web_server.py` | Bearer token for the machine API. Required to bind a public interface. |
| `AUTH_USERNAME` | — | `web_auth.py` | Owner login name. Required for a public bind. |
| `AUTH_PASSWORD_HASH` | — | `web_auth.py` | PBKDF2-SHA256 hash of the owner password (`pbkdf2_sha256$<iters>$<salt>$<digest>`). Never the password. |
| `AUTH_SESSION_SECRET` | — | `web_auth.py` | Separate secret that signs sessions. Must not be the Cloud key. |
| `WEB_COOKIE_SECURE` | auto | `web_server.py` | Force the `Secure` cookie flag. Default: on when `X-Forwarded-Proto: https`. |
| `TRUSTED_PROXY_HOPS` | `1` | `web_server.py` | Number of trusted proxies; the client IP is the rightmost trusted hop of `X-Forwarded-For`. `0` ignores the header. A junk value falls back to `1`; a negative value clamps to `0`. |
| `LOGIN_MAX_ATTEMPTS_PER_IP` | `30` | `web_server.py` | In-memory per-IP login ceiling per minute. |
| `LOGIN_MAX_ATTEMPTS_GLOBAL` | `120` | `web_server.py` | In-memory endpoint-wide login ceiling per minute; independent of the client IP. |
| `APP_VERSION` / `RENDER_GIT_COMMIT` / `GIT_COMMIT` | — | `web_server.py` | Shown on the System screen (`build.commit`). |

### 3.2 OpenHands

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `OPENHANDS_API_KEY` | — | `cloud_client.py` | Bearer key for Cloud V1. Required for real execution. Never logged or returned. |
| `OPENHANDS_CLOUD_URL` | `https://app.all-hands.dev` | `cloud_client.py` | Cloud base URL. Must be `http(s)`; any other scheme is refused at construction. |
| `OPENHANDS_TRANSPORT` | `direct` | `cloud_direct.py` | `direct` provisions a sandbox and drives its agent-server; `app` uses the legacy app-conversation endpoint. |
| `OPENHANDS_DEFAULT_MODEL` | — | `cloud_direct.py` | Model forced on every sandbox conversation. **Recommended**: a bare sandbox's default model may have no key (see `OPENHANDS_SETUP.md`). |
| `OPENHANDS_DIRECT_REPO_MODE` | `prompt` | `cloud_direct.py` | How the direct transport handles a repository-scoped task: `prompt` prepends a clone step, `refuse` dead-letters, `allow` ignores the field. |
| `OPENHANDS_AGENT_PROFILE_ID` | — | `web_server.py` | Select an account agent profile by id. Unset = inherit the account's active profile and its MCP servers/skills. |
| `OPENHANDS_ORCHESTRATOR_MODEL` | `gpt-5` | `providers.py` | Model for OpenHands used as the orchestrator fallback (see §3.4). |
| `LOCAL_AGENT_SERVER_URL` | `http://127.0.0.1:60000` | `local_runtime.py` | Local agent-server base URL for the local end-to-end scripts. |
| `OHP_LOCAL_LLM_MODEL` | — | `local_runtime.py` | Model the local runtime asks the local agent-server to use. |
| `SESSION_API_KEY` | — | `openhands_client.py` | Session key for the local agent-server adapter. Not used by the Cloud transports. |

### 3.3 Account pool, credentials and the address guard

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `NEXUS_SECRET_KEY` | — | `secrets.py` | Fernet key that encrypts stored GitHub PATs and OpenHands API keys. **Required** to add an account; without it the account endpoint refuses rather than write plaintext. |
| `NEXUS_OPENHANDS_ALLOWED_HOSTS` | — | `net_guard.py` | Comma-separated hosts (or `host:port`) permitted to be a private address, narrower than `NEXUS_ALLOW_PRIVATE_TARGETS`. A cloud-metadata address is refused even for a named host. |
| `NEXUS_ALLOW_PRIVATE_TARGETS` | `0` | `net_guard.py`, `nexus_policy.py` | Set to `1` to permit every private address as an outbound target (a deployment on a trusted network). Leave unset on a public deployment. |
| `GITHUB_TOKEN` | — | `github_client.py` | Server-side GitHub token for repository discovery; confirmed writes need the extra permissions below. Never returned by the API or UI. |
| `GITHUB_API_URL` | `https://api.github.com` | `github_client.py` | Override the GitHub REST base URL. Must be `http(s)` with a host and no embedded credentials. |
| `GITHUB_ENABLE_WRITES` | `false` | `web_server.py` | Enables the session-only, CSRF-protected, two-step GitHub create/visibility actions. |

### 3.4 LLM orchestrator cascade

Only configured providers are used; a missing key skips that provider. These
keys are server-side only, never sent to a model and never logged. The cascade
only *proposes* plans; OpenHands remains the executor.

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | — | `providers.py` | Gemini (first in the cascade). |
| `GROQ_API_KEY` | — | `providers.py` | Groq (second). |
| `OPENROUTER_API_KEY` | — | `providers.py` | OpenRouter (third). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | `providers.py` | Model override. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | `providers.py` | Model override. |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | `providers.py` | Model override. |
| `GEMINI_BASE_URL` / `GROQ_BASE_URL` / `OPENROUTER_BASE_URL` | provider defaults | `providers.py` | Endpoint overrides. |
| `<PROVIDER>_CONTEXT_TOKENS` | per provider | `providers.py` | The model's window, used to size planning parts. |

### 3.5 Evidence Trust Engine

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `NEXUS_EVIDENCE_KEY_ID` | — | `evidence_trust.py` | Key id the public key is published under. |
| `NEXUS_EVIDENCE_PRIVATE_KEY` | — | `evidence_trust.py` | Ed25519 private key (base64) that signs evidence. Server-side only. |
| `NEXUS_EVIDENCE_KEYRING` | — | `evidence_trust.py` | Comma-separated `key_id:base64public` entries; append `:revoked` to make a key's records `UNVERIFIED`. |

Without a key pair the lab records no evidence (never a fabricated one).

### 3.6 Autonomous lab and durable runs

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `NEXUS_LAB_INLINE` | on | `web_server.py` | `0`/`false`/`no`/`off` stops the web server advancing runs itself; a separate worker must then run. |
| `NEXUS_LAB_MAX_ACTIVE_TASKS` | `8` | `nexus_lab_store.py` | Tasks at work at once over all runs (`0` = no limit). |
| `NEXUS_TASK_TIMEOUT_SECONDS` | `7200` | `nexus_lab_store.py` | How long one task's conversation may run before the task is given up (bounded to 60 s … 24 h). |
| `NEXUS_TASK_STALL_SECONDS` | `1800` | `nexus_lab.py` | A conversation with no sign of life this long is told to stop and the task restarted. `0` switches the check off. |
| `NEXUS_RETRY_BASE_SECONDS` | `60` | `nexus_retry.py` | Wait after the first exhausted work cycle. |
| `NEXUS_RETRY_MAX_SECONDS` | `3600` | `nexus_retry.py` | Longest wait between cycles. |
| `NEXUS_RETRY_MAX_CYCLES` | `0` | `nexus_retry.py` | Operator ceiling on cycles per task (`0` = none; then the run waits for a person). |
| `NEXUS_SPEC_MAX_CHARS` | `1000000` | `nexus_specs.py` | Longest task text accepted. |
| `NEXUS_SPEC_INLINE_CHARS` | `20000` | `nexus_specs.py` | Up to this, the text travels inside the agent's first message. |
| `NEXUS_SPEC_MESSAGE_CHARS` | `60000` | `nexus_specs.py` | `app` transport: up to this the whole text is in the message; longer, each task gets the part it was planned from. |
| `NEXUS_SPEC_STAGE_TOKENS` | `24000` | `nexus_spec_planner.py` | Size of one planning part. |
| `NEXUS_SPEC_SANDBOX_DIR` | — | `cloud_direct.py` | `direct` transport: one directory to place the task text in (absolute). |
| `NEXUS_MAX_UPLOAD_BYTES` | `33554432` | `file_lab.py` | Largest file-lab / voice-note upload (32 MiB). |

### 3.7 MCP, CLI and tests

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `NEXUS_DB_URL` | falls back to `SUPABASE_DB_URL` | `mcp_server.py` | Database for the stdio MCP server. |
| `NEXUS_MCP_PRINCIPAL` | `machine:default` | `mcp_server.py` | Principal the MCP tools act as. |
| `NEXUS_URL` (or `NEXUS_BASE_URL`) | — | `cli.py` | Base URL of the NEXUS server for the CLI. |
| `NEXUS_TOKEN` | falls back to `WEB_AUTH_TOKEN` | `cli.py` | Bearer token for the CLI. Never printed. |
| `TEST_SUPABASE_DB_URL` | — | `tests/` | DSN for the pytest suite; without it the DB-backed modules skip cleanly. |

---

## 4. Required permissions

### GitHub token

| use | minimum scope |
| --- | --- |
| repository discovery (read) | read access to repositories (for private repositories: `repo`) |
| confirmed create / visibility writes | the write permissions GitHub requires for `POST /user/repos` and `PATCH /repos/{owner}/{name}`, **and** `GITHUB_ENABLE_WRITES=true`, an owner session, CSRF, and the two-step confirmation |

A `403` with `X-RateLimit-Remaining: 0` means the token is valid and the quota
is spent: NEXUS reports it as a transient `503`, not an auth failure. Rotating a
working token would not help.

### OpenHands key

The Cloud API key is a Bearer token. It needs access to conversations and
sandboxes. It is read from the environment (or an encrypted stored account) and
never logged, returned or written to the database. The account has a daily
conversation/sandbox limit; a run that reaches it waits and is retried.

### Database

The DSN must reach a PostgreSQL 14+ server. Behind a transaction-mode pooler
(Supabase PgBouncer on 6543) leave server-side prepared statements off, which is
the default. The app connects as the owner; the sandbox session-key tables have
RLS and are not readable by anon/authenticated clients.

### Filesystem / OS

The service stores all state in PostgreSQL and writes nothing to disk except,
optionally, local restore-point bundles under `.restore-points/` (gitignored).
A `direct`-transport deployment needs outbound HTTPS to the Cloud and the
sandbox agent-server URLs the Cloud hands out; the address guard (§3.3) refuses
private targets unless the operator allows them.

---

## 5. Recovery

### Database unreachable

`PostgresStore` holds one connection and replaces it on evidence; when the
database cannot be reached it raises `StoreUnavailable`, the HTTP layer answers
`503 {"error": "database unavailable"}`, and further attempts are refused for a
short backoff. The lab worker logs the error and retries on the next pass — it
does not exit. Check the DSN and the database; `GET /readyz` does a real
round-trip.

### A run stays `queued`

Nothing is advancing it. Remove `NEXUS_LAB_INLINE=0`, or start
`python -m orchestration.lab_worker`. `GET /api/openhands/diagnostics` reports
`lab_worker: FAILED` when active runs exist but no heartbeat is fresh.

### Lost credentials

Rotate the key and re-verify: `nexus openhands account verify --account <id>`
(re-check authorization) or `nexus openhands connection --account <id>` (probe
reachability). For GitHub, re-add the account with the same label to replace the
token.

### Restore the code

```bash
python -m orchestration.cli restore-point create --label "before <change>"
python -m orchestration.cli restore-point verify .restore-points/<name>.json
git clone .restore-points/nexus-restore-<stamp>-<head12>.bundle /tmp/restored
```

### Restore the database

```bash
pg_dump "$SUPABASE_DB_URL" --format=custom --file=nexus-$(date +%F).dump
createdb nexus_restore
pg_restore --dbname=nexus_restore --no-owner nexus-2026-01-01.dump
```

`tests/test_backup_restore.py` is the automated proxy for this drill.

See `TROUBLESHOOTING.md` for symptom-first fixes and `DEPLOY.md` for the
migration procedure.
