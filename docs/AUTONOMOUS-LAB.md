# NEXUS Autonomous Lab — integration status

This document states what is actually implemented, tested and verified in
`main`, and what is not. It is written after the integration of PR #17 and the
addition of the provider cascade. Status labels follow the request's vocabulary:

**IMPLEMENTED** · **TESTED** · **LIVE VERIFIED** · **PARTIALLY VERIFIED** ·
**BLOCKED** · **NOT IMPLEMENTED**

An agent's own text is never evidence here. "TESTED" means a test in this
repository exercises the real code path (a real HTTP server and, where relevant,
a real PostgreSQL). "LIVE VERIFIED" is reserved for a check against a real
external service and is only used where that was actually run.

---

## 1. Single-main delivery (Git)

| Item | Status | Evidence |
|---|---|---|
| One working branch `main` | **LIVE VERIFIED** | GitHub API `branches` returns `["main"]`; local `main == origin/main`. |
| PR #17 integrated | **LIVE VERIFIED** | `origin/nexus/laboratory-files-github` was an ancestor of `main`; its files are present; branch deleted after confirmation. |
| User doc changes preserved | **LIVE VERIFIED** | Stashed edits re-applied onto merged `main`; the "What is ready today" section is present. |
| Preservation tags kept | **LIVE VERIFIED** | 13 `preservation/*` tags still present. |
| `main` pushed | **LIVE VERIFIED** | `git push` fast-forwarded `c36c828..24bdb53`. |

## 2. File laboratory (PR #17)

| Feature | Status | Evidence |
|---|---|---|
| Opaque project uploads (raw body + `X-File-Name`) | **TESTED** | `tests/test_lab_files.py` over real HTTP. |
| File lab uploads (multipart), preview, download, delete | **TESTED** | `tests/test_file_lab.py` (25 cases). |
| Development workspaces + unified diff | **TESTED** | `tests/test_file_lab.py`. |
| Archive safety (Zip Slip, symlinks, bombs) | **TESTED** | `tests/test_archive_safety.py` (no DB; each rejection named). |
| Voice notes (honest: no server recognizer) | **TESTED** | `tests/test_file_lab.py`. |
| Guarded GitHub actions (propose → confirm) | **TESTED** | `tests/test_file_lab.py`, `tests/test_github_client.py`. |
| Unified `nexus_files` schema (two designs) | **TESTED** | `migrations/0011_nexus_files.sql`; both suites green. |

## 3. LLM orchestrator cascade (new)

| Feature | Status | Evidence |
|---|---|---|
| Ordered adapters Gemini → Groq → OpenRouter → OpenHands | **TESTED** | `tests/test_providers.py`. |
| Real auth, structured JSON, size/timeout limits | **TESTED** | same. |
| Bounded exponential backoff, `Retry-After`, 429/5xx/transport | **TESTED** | same. |
| Failover only on provider/transport failure | **TESTED** | same (401 is not retried; a bad body fails over). |
| Orchestrator ≠ executor (OpenHands last, never executes its own plan) | **IMPLEMENTED** | `OpenHandsOrchestratorProvider.executor=False`. |
| Plan-draft route + UI button (stores/dispatches nothing) | **TESTED** | `tests/test_plan_draft.py`. |
| Live provider keys exercised end to end | **BLOCKED** | No `GEMINI_API_KEY`/`GROQ_API_KEY`/`OPENROUTER_API_KEY` in this environment; only the local fake-provider server was used. |

## 4. Project execution, evidence, CLI, MCP, PWA (pre-existing MVP)

| Feature | Status | Evidence |
|---|---|---|
| D-2 project execution binding | **TESTED** | `tests/test_project_binding.py`, `tests/test_dispatch_authorization.py`. |
| Ed25519 Evidence Trust Engine | **TESTED** | `tests/test_evidence_trust.py`. |
| CLI (`nexus`) | **TESTED** | `tests/test_cli_mcp.py`. |
| MCP stdio + Streamable HTTP | **TESTED** | `tests/test_cli_mcp.py`, `tests/test_route_auth.py`. |
| Backup/restore drill | **TESTED** | `tests/test_backup_restore.py` (real `pg_dump`/`pg_restore`). |
| Responsive web + PWA | **TESTED** | `tests/test_browser_e2e.py` (Playwright, Chromium), `tests/test_pwa.py`. |
| Live OpenHands execution | **BLOCKED** | No working OpenHands key in this environment; the Cloud boundary is exercised against `tests/fake_cloud.py`. |

## 5. Autonomous goal loop (the headline request)

| Capability | Status | Why |
|---|---|---|
| One big goal → auto plan → auto tasks → auto conversations → verify → memory → next action | **NOT IMPLEMENTED** | Today a plan is authored (manually or via the AI draft) and **approved explicitly**; approval does not dispatch. There is no scheduler that turns an approved plan into tasks, creates conversations, verifies them and continues unattended. |
| Project context collection (languages, modules, schema, deps, CI) | **NOT IMPLEMENTED** | No context collector exists. |
| Project memory (goals, decisions, attempts, evidence, failover reasons) | **PARTIALLY VERIFIED** | Durable records exist (`nexus_*`, `task_events`, `nexus_evidence`), but there is no unified memory model with `VERIFIED/INFERRED/UNKNOWN/OUTDATED` provenance. |
| Independent verification of task results | **PARTIALLY VERIFIED** | The verifier decides workflow state from durable signals and the Evidence Trust Engine signs results, but there is no independent test/build runner producing evidence of its own. |
| Supervisor states (PENDING…CANCELLED) and recovery | **PARTIALLY VERIFIED** | The dispatcher has leases, idempotency, bounded retry and reconciliation; the full NEXUS lifecycle (`planned→ready→…→completed|rework|blocked|failed`) is not implemented. |
| Multi-account GitHub / OpenHands routing | **NOT IMPLEMENTED** | One `GITHUB_TOKEN`, one `OPENHANDS_API_KEY`. |

## 6. Mobile application (Android / iOS)

| Item | Status | Why |
|---|---|---|
| Native Android/iOS app | **NOT IMPLEMENTED** | The PWA is the mobile-capable client today. No native project, no signing credentials, no store submission. The backend already serves the same auth/permissions over HTTPS, so a native client is a client-side effort, not a backend blocker. |
| Push notifications | **NOT IMPLEMENTED** | In-app status is polled; no push transport. |

## 7. Safety

| Item | Status | Evidence |
|---|---|---|
| Critical actions require a one-use, session-bound confirmation | **TESTED** | GitHub writes via `nexus_actions.py` (SHA-256 token, TTL, single use) and `github_confirmations.py`. |
| Production deploy/migrations not automatic | **IMPLEMENTED** | `AUTO_MIGRATE=false` opt-out; no deploy hook in this environment. |

---

## How to run

```bash
# 1. Dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 2. A PostgreSQL database; migrations apply on start by default.
export SUPABASE_DB_URL="postgresql://user:pw@host:5432/db"

# 3. Owner login + machine token + evidence key (see DEPLOY.md)
export WEB_AUTH_TOKEN=... AUTH_USERNAME=owner AUTH_PASSWORD_HASH=... AUTH_SESSION_SECRET=...

# 4. Optional: orchestrator providers (only configured ones are used)
export GEMINI_API_KEY=...   # default orchestrator
export GROQ_API_KEY=...     # fallback 1
export OPENROUTER_API_KEY=... # fallback 2
export OPENHANDS_API_KEY=...  # executor, and last-resort orchestrator

# 5. Run one instance
python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN
```

## Credentials needed for the parts that are BLOCKED

- `OPENHANDS_API_KEY` — a live OpenHands Cloud key (executor + last-resort
  orchestrator). Without it, execution stops at the Cloud boundary.
- `GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` — at least one, to
  exercise the cascade against a real provider.
- `GITHUB_TOKEN` — repository discovery; `GITHUB_ENABLE_WRITES=true` plus
  admin rights for the optional confirmed writes.

## What this document does not claim

The NEXUS product described in the request — enter one goal and the system
plans, dispatches, verifies, remembers and continues by itself — is **not**
built. What is built is a durable, tested control plane with a working Sr/Jr
delegation, a project/plan/file foundation, signed evidence, a CLI/MCP surface,
a responsive PWA and a real provider cascade with honest failover. Closing the
gap to the autonomous loop is the next, clearly-scoped piece of work, not
something this document presents as done.
