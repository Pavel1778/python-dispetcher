# NEXUS — Deployment Checklist

Repeatable install, configuration, migration, start/stop, health, backup and
rollback for the running product. `docs/ENVIRONMENT.md` is the full variable
reference; this is the operational subset a deploy sets by hand.

## 1. Prerequisites

- Python 3.10+ (CI and this sprint ran 3.12/3.13).
- PostgreSQL 17 (the schema targets 17; the tests run against a local cluster).
- Chromium only for the browser E2E (`python -m playwright install chromium`).
- Optional live services: an OpenHands Cloud key (execution), a GitHub token
  (repository read/write), an LLM provider key (Gemini/Groq/OpenRouter).

## 2. Install

```bash
python -m pip install -r requirements.txt          # psycopg[binary,pool], cryptography
python -m pip install -r requirements-dev.txt      # pytest, ruff, playwright (dev only)
```

## 3. Configure (minimum for a working single-instance service)

```bash
export SUPABASE_DB_URL="postgresql://user:pw@host:5432/nexus"   # the store
export WEB_AUTH_TOKEN="$(python -m orchestration.cli hash-password ...)"  # machine bearer
export AUTH_USERNAME="owner"
export AUTH_PASSWORD_HASH="<pbkdf2 hash>"          # see web_auth / cli
export AUTH_SESSION_SECRET="<32+ random bytes>"
export OPENHANDS_API_KEY="<key>"                   # execution
export OPENHANDS_DEFAULT_MODEL="openhands/deepseek-v4.1-flash"   # required in practice
```

The server **refuses to bind `0.0.0.0`** unless `WEB_AUTH_TOKEN` and all three
`AUTH_*` variables are set (`web_server.main`). `.env.example` lists the rest
(`OPENHANDS_TRANSPORT`, `NEXUS_SPEC_*`, `NEXUS_LAB_INLINE`, …).

## 4. Migrate

Migrations are **idempotent** and applied by the process on first connect
(recorded in `schema_migrations`), so a Render free plan with no shell works.
To apply out of band:

```bash
for f in $(ls migrations/*.sql | sort); do psql "$SUPABASE_DB_URL" -f "$f"; done
```

- 41 files, `0001`–`0040` (the `0012`/`0013` gap is reserved and permanent).
- A migration whose body changed **after** it was applied is refused
  (`MigrationError`), never re-run — ship a new file instead.
- `AUTO_MIGRATE=false` turns the automatic apply off for environments that
  manage the schema out of band.

## 5. Start / stop

```bash
# Web + inline lab loop (default: the server advances runs itself)
python -m orchestration.web_server --host 0.0.0.0 --port "$PORT" --token "$WEB_AUTH_TOKEN"

# Or run the lab loop as its own process (recommended on a paid instance)
NEXUS_LAB_INLINE=0 python -m orchestration.web_server --host 0.0.0.0 --port "$PORT" --token "$WEB_AUTH_TOKEN" &
python -m orchestration.lab_worker            # the worker; safe to run alongside

# MCP (stdio, or Streamable HTTP at POST /mcp on the web server)
python -m orchestration.mcp_server
```

Run **exactly one** web instance unless the database handles the pool. The
worker may run beside it (every claim is a lease). Stop with `SIGTERM`: the
server waits `LAB_STOP_GRACE` (20 s) for the loop and leaves no half-written
start (a recorded start is followed by the next process).

## 6. Health

```bash
curl -s http://host:port/healthz          # {"status":"ok"} — liveness, no config
curl -s http://host:port/readyz           # {"status":"ready","database":true}
```

`/readyz` checks the database; `/healthz` stays a one-line liveness probe. Both
are public and expose no configuration.

## 7. Verify the install (smoke)

```bash
python -m orchestration.cli doctor --json                     # config, no secret
python -m orchestration.cli openhands diagnostics             # accounts, pool, cloud
python -m orchestration.cli openhands diagnostics --live      # one real conversation
python -m orchestration.cli openhands tools --live            # agent + NEXUS tool inventory
```

## 8. Backup / restore

```bash
pg_dump "$SUPABASE_DB_URL" -Fc -f nexus-$(date +%F).dump      # backup
createdb nexus_restore && pg_restore -d nexus_restore nexus-*.dump
```

`tests/test_backup_restore.py` is the automated proof that a dump restores.

## 9. Upgrade / rollback

- **Upgrade:** deploy the new build; it applies any new migration on start. A
  migration is additive by policy; never edit an applied file.
- **Rollback:** redeploy the previous build. The schema is forward-compatible
  (new columns are nullable/defaulted), so the previous build runs against it.
  Restore from the dump only if a rollback must also undo data.
- The application keeps a **safe restore point** (a git bundle of every ref)
  under `.restore-points/` (gitignored): `nexus restore-point create|list|verify`.

## 10. Do not

- Do not run `git reset --hard` / `clean -fd` / force-push against `main`.
- Do not deploy to production without the owner's explicit authorization.
- Do not commit a secret; the token is read from the environment and never
  logged or returned.
