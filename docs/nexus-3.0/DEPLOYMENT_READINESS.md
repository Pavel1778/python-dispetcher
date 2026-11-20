# DEPLOYMENT READINESS — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: ready for a controlled deploy; one gate (push-verified live execution)
is a credential, not a code, blocker.**

---

## 1. What changed that a deploy must know

- **No schema change** in this increment (the last migration is still `0043`).
  A deploy runs no new migration; nothing to roll back at the database level.
- **OpenHands configuration behaviour changed**: `add()` no longer clears an
  omitted field. A reconnect from an existing client that *relied* on clearing a
  model by omission must now use `edit` with an explicit `null`. This is the
  intended fix; note it for operators.
- **CI now runs `cargo fmt --check` and `clippy -D warnings`.** A branch that
  fails these will no longer merge green.

## 2. Environment variables (unchanged set; see `docs/ENVIRONMENT.md`)

The OpenHands configuration this increment makes reliable is stored in the
database, not the environment, so a redeploy does not reset it. The variables a
deploy still needs:

- `SUPABASE_DB_URL` — the PostgreSQL database.
- `NEXUS_SECRET_KEY` — Fernet key for stored credentials (required to store an
  account; changing it makes stored keys unreadable — rotate by re-adding).
- `OPENHANDS_API_KEY` — the process-wide fallback key.
- `OPENHANDS_DEFAULT_MODEL` — recommended (`openhands/deepseek-v4.1-flash`); a
  bare sandbox without it ends `error` (LLM auth).
- `WEB_AUTH_TOKEN`, `AUTH_USERNAME`, `AUTH_PASSWORD_HASH`, `AUTH_SESSION_SECRET`.
- `GITHUB_TOKEN` — for repository reads and the Self-QA clone.

## 3. Health checks

- `GET /healthz` — one-line liveness, no configuration.
- `GET /readyz` — checks the database.
- `GET /api/openhands/diagnostics` (protected) — the full report; `live=1` runs
  the real probe (creates and deletes one conversation).

## 4. Run commands

```bash
# web server (the default advancer of runs)
python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN

# the lab worker (a second advancer; every claim is a lease, so both may run)
python -m orchestration.lab_worker

# one pass (cron-style; registers and heartbeats like a long-lived worker)
python -m orchestration.lab_worker --once
```

## 5. Rollback

- **Code**: revert the merge commit. This increment adds no migration, so a
  revert is complete with no data step.
- **Rust**: production never calls Rust; reverting the Rust files changes
  nothing observable.
- **OpenHands configuration**: stored in `nexus_openhands_accounts`; a revert of
  the code does not touch the rows. To return to the old "clear on omission"
  behaviour is not desirable; use `edit` with explicit `null` instead.

## 6. Pre-deploy checklist

- [ ] `ruff check .` clean.
- [ ] `pytest tests/ -q` green (see `MASTER_ACCEPTANCE_REPORT.md` §5).
- [ ] `cargo fmt --all --check` and `cargo clippy -D warnings` clean.
- [ ] `NEXUS_SECRET_KEY` set and stable (a change orphans stored keys).
- [ ] `OPENHANDS_DEFAULT_MODEL` set.
- [ ] `GET /api/openhands/diagnostics?live=1` → no FAILED.

## 7. What is NOT ready for an unattended production run

- **Push-verified execution** needs a GitHub provider token on the OpenHands
  account. Without it every repository task ends `completed_unverified` and the
  run blocks — correct, but not "autonomous completion".
- **Render free plan** stops the process after ~15 minutes idle; a run that must
  work unattended needs a paid instance or an external `/healthz` ping.
