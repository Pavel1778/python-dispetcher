# NEXUS Operations

How to run NEXUS, watch it, diagnose it, recover it and back it up. For the
full environment reference see `docs/ENVIRONMENT.md`; for deployment see
`DEPLOY.md`; for troubleshooting see `docs/TROUBLESHOOTING.md`.

## 1. What runs

Two processes, one database:

| Process | Command | Role |
| --- | --- | --- |
| Web + dispatcher | `python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN` | HTTP API, studio, and (by default) the lab loop on its own thread |
| Lab worker | `python -m orchestration.lab_worker --worker-id lab-1` | Advances autonomous runs; may run beside the web server (every claim is a lease) |

Optional: `python -m orchestration.mcp_server` (MCP over stdio),
`python -m orchestration.cli …` (operator CLI).

The web server advances runs unless `NEXUS_LAB_INLINE` is `0`/`false`/`no`/`off`
(`web_server.lab_inline_default`). A deployment that wants a dedicated worker
sets `NEXUS_LAB_INLINE=0` and runs `lab_worker` (see `docs/WORKER_SETUP.md`).

## 2. Health and readiness

- `GET /healthz` — one line, `{"status": "ok"}`, no configuration. Public.
- `GET /readyz` — liveness plus a real database round-trip; `503` when the
  database is not reachable. Public, no configuration.
- `GET /api/system` — build, versions, provider *presence* (booleans only).
- `GET /api/openhands/diagnostics[?live=1]` — the diagnostic report; each check
  is VERIFIED / FAILED / BLOCKED / UNVERIFIED. `live=1` runs the real probe
  (creates and deletes exactly one OpenHands conversation).
- `GET /api/openhands/tools[?live=1]` — the tool inventory (agent catalog, MCP
  names, NEXUS tool names).

## 3. Monitoring

- **A run that is not advancing**: check `GET /api/openhands/diagnostics` for
  `lab_worker`. `FAILED` means active runs exist with no fresh heartbeat. Confirm
  the worker process is running and can reach PostgreSQL.
- **The database connection dropped**: the store reconnects on evidence; while
  it cannot, HTTP answers `503 {"error": "database unavailable"}` and further
  attempts are refused for 2 s. `/readyz` returns `503` meanwhile.
- **A provider outage**: `GET /api/providers` (presence) and
  `GET /api/providers?live=1` (a real read-only probe). A 429 is BLOCKED, not
  FAILED.
- **Logs**: the server scrubs credential-looking values from every record
  (`install_log_redaction`). The API key is never logged.

## 4. Diagnostics

```bash
# One command, everything the operator needs
TEST_SUPABASE_DB_URL=... python -m orchestration.cli doctor
python -m orchestration.cli migrations          # 42/42 applied? which file pending?
python -m orchestration.cli worker-status       # queue, stuck tasks, live workers

# The OpenHands diagnostics, live
python -m orchestration.cli openhands diagnostics --live

# The provider cascade
python -m orchestration.cli providers --live
```

## 5. Recovery

- **A worker died mid-task**: the claim's lease (default 900 s) expires and the
  task is reclaimed by the next pass; a recorded start is adopted (the
  conversation is bound without starting a second one). `nexus_run_tasks.
  pending_start` is what makes a start survive the process that asked for it.
- **The process was restarted**: nothing is carried in memory that matters — the
  loop re-reads active runs. A generation in flight is lost (its answer was not
  stored); the conversation is left `thinking` and can be asked again.
- **A migration was edited after being applied**: `apply_migrations` refuses
  with `MigrationError`, naming the file. Ship a new migration; do not edit an
  applied one.
- **A restore point**: `python -m orchestration.cli restore-point create|list|
  verify` writes a git bundle of every ref plus a JSON record under
  `.restore-points/` (gitignored). It is local, non-destructive and needs no API.

## 6. Backup

- **Database**: `pg_dump` the database; `tests/test_backup_restore.py` is the
  automated drill that a dump restores into a fresh database.
- **Repository**: the restore point above (`git bundle create --all`).
- **Secrets**: `NEXUS_SECRET_KEY` encrypts every stored secret; without it, a
  restored database cannot decrypt them. Keep it with the backup, separately,
  and never in the repository.

## 7. Deployment

`render.yaml` defines the web service and the lab worker; `docker-compose.yml`
defines both for a local stack. Migrations are idempotent and applied by the
process on first connect (recorded in `schema_migrations`); `AUTO_MIGRATE=false`
turns that off for environments that manage the schema out of band. The server
refuses to bind `0.0.0.0` without `WEB_AUTH_TOKEN` and all three `AUTH_*`
variables.

## 8. What the free tier does not do

Render's free plan stops the process after ~15 minutes without a request, and
the loop with it. Conversations already started go on in the Cloud and are
collected when the service wakes. A run that must work unattended needs a paid
instance or an outside request to `/healthz` every few minutes.
