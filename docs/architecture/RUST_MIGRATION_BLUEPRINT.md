# NEXUS — Rust Migration Blueprint

> **STATUS: ARCHITECTURAL SPECIFICATION — NOT AN AUTHORIZATION TO REWRITE.**
>
> This document is a specification and a decision record. It does **not**
> authorize starting the Rust implementation, does not change production, does
> not change `main`, does not change `render.yaml`, and does not move any of the
> 18 commits from the local `nexus/perf-reliability-lifecycle` branch. It is
> written to be reviewed and approved before any implementation step begins.
>
> Every statement carries an evidence class:
> **VERIFIED** (checked against code/infra), **MEASURED** (a number was taken),
> **INFERRED** (a reasoned conclusion), **UNKNOWN** (insufficient data),
> **BLOCKED** (cannot be checked with current access).

- **Author:** OpenHands (AI agent) on behalf of the repository owner
- **Date:** 2026-11-14 (this forensic source; see the superseding decision below)
- **Base commit:** `b2f302880d715b8dc7768c14ae48af9d1e206595` (`origin/main`)
- **Branch:** `docs/rust-migration-blueprint`
- **Scope:** discovery, audit, target architecture, migration plan
- **Non-goals:** implementation, deployment, database change, traffic change

## Superseding decision and document set (added 2026-11-15)

The architecture decision of 2026-11-15 adopts the strangler strategy and the
Rust modular monolith recorded here, and expands it into a full document set.
**This blueprint remains the forensic source** (its discovery, audit and
measured facts are unchanged); the decision and the detailed specifications
live in the sibling documents:

- [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md) — the decision.
- [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md) — 20 domains.
- [RUST_WORKSPACE_DESIGN](./RUST_WORKSPACE_DESIGN.md) — crates.
- [EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md) — the hardest domain.
- [PYTHON_RUST_MIGRATION_MATRIX](./PYTHON_RUST_MIGRATION_MATRIX.md) — module + CRUD parity.
- [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) — dual-run rules.
- [CLIENTS_ARCHITECTURE](./CLIENTS_ARCHITECTURE.md) — Web/Desktop/Android/CLI.
- [SELF_QA_ARCHITECTURE](./SELF_QA_ARCHITECTURE.md) — autonomous verification.
- [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md) — phases + DoD.
- [RUST_MIGRATION_RISK_REGISTER](./RUST_MIGRATION_RISK_REGISTER.md) — risks.

**Update (2026-11-15):** the production blocker described in §6.3 was fixed in
`main` by commit `383a234` (the `DECLARE r record` → `text` change plus a
PostgreSQL-role regression test), and production now serves the fixed build
(`a16da03`). See [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md) phase P0.

---

## 0. How to read this document

Sections 1–3 are the current-state facts. Sections 4–6 are the audit of
performance, Render and PostgreSQL. Sections 7–9 are OpenHands, frontend and
security. Sections 10–14 are the target architecture and the two parity
matrices. Sections 15–20 are the migration, infrastructure, testing, risk,
phase and rollback plans. Section 21 is the acceptance criteria. Appendix A is
the full CRUD lifecycle matrix.

A claim of "faster", "safer" or "complete" is only made where an artefact backs
it. Where there is no artefact, the claim is not made.

---

## 1. Executive Summary

NEXUS is a durable control plane that orchestrates OpenHands agent
conversations. It is a mature, deliberately **standard-library-only Python
service** (~77,495 LOC across 181 modules, 1,488 test functions) whose measured
latency on its own paths is **single-digit milliseconds** — the slow paths are
network-bound (OpenHands Cloud, LLM providers, GitHub).

**The present, concrete problem is not performance; it is release engineering.**
Production is frozen on commit `42609a4`. Every deploy after it
(`f3767c9`, `c2b5fe8`, `a6bb451`, `8fba2b6`) failed with `update_failed`
(nonZeroExit=1) because of a single word in one migration: `DECLARE r record;`
instead of `DECLARE r text;` in `migrations/0037_conversation_management.sql`
(line 54). This is the exact source of the production error
`role "(authenticated)" does not exist` (VERIFIED, Render runtime logs).

**On the Rust migration itself:** Rust buys type safety, async I/O without a
global connection lock, a single binary, and one stack with Tauri for
desktop/mobile. It does **not** reduce the latency of LLM- or OpenHands-bound
operations (see §7). Any speed claim must be proven with identical benchmark
scenarios (§5.7, §14). The recommended target is a **modular monolith**, not
microservices.

---

## 2. Current architecture (VERIFIED)

Source: `docs/ARCHITECTURE.md`, `orchestration/web_server.py`,
`orchestration/cloud_store.py`.

```
browser (studio)            machine client (CLI / MCP)
      │                              │
      ▼                              ▼
┌───────────────────────────────────────────────────────────┐
│ orchestration/web_server.py — one ThreadingHTTPServer       │
│  auth (session + bearer + CSRF) · routes · lab loop thread  │
│  + plan loop thread + dispatch loop thread                  │
└───────┬───────────┬───────────┬───────────┬────────────────┘
        ▼           ▼           ▼           ▼
  nexus_intent  nexus_conversations  nexus_lab   providers
  nexus_commands  nexus_generation   (scheduler,  (Gemini→Groq→
  (router)        (stop token)        executor,    OpenRouter→
        │                            verifier)     OpenHands)
        ▼                              ▼
  nexus_specs / nexus_spec_planner   nexus_lab_store ── cloud_store (PostgreSQL)
        │                              ▼
  providers.py                 openhands_router / pool
                               CloudClient / CloudDirectClient
```

- **One process, stateful.** Background threads at `web_server.py:631-637`:
  `_loop`, `_plan_loop`, `_lab_loop` (the last is off when `NEXUS_LAB_INLINE=0`,
  then `lab_worker.py` advances runs as a separate process).
- **One database connection per process**, serialized by a
  `threading.RLock` (`cloud_store.py:276-281`, `_tx` at line 536).
- **Storage:** PostgreSQL via `SUPABASE_DB_URL` (or `DATABASE_URL`)
  (`cloud_store.py:234`).
- **OpenHands transport:** `direct` (default) or `app`, chosen by
  `OPENHANDS_TRANSPORT` (`cloud_direct.py:886`).

### 2.1 Layers

| Layer | Module(s) | Responsibility |
|---|---|---|
| HTTP surface | `web_server.py` | auth, routing, the chat turn, the lab loop, `POST /mcp` |
| Chat decision path | `nexus_commands`, `nexus_intent`, `nexus_chat`, `nexus_conversations`, `nexus_generation` | parse → classify → respond → store → cancel |
| Autonomous pipeline | `nexus_specs`, `nexus_spec_planner`, `nexus_lab`, `nexus_lab_store` | spec → plan → run DAG → schedule → execute → verify → record → decide |
| OpenHands | `openhands_router`, `openhands_pool`, `cloud_client`, `cloud_direct`, `openhands_client` | account selection, failover, dispatch, poll |
| Storage | `cloud_store.py`, `nexus_lab_store.py` | the store, the run/task state machine, leases, fencing |
| Frontend | `orchestration/studio/` | static, no build step |
| Security | `nexus_policy`, `net_guard`, `secrets`, `archive_safety`, `web_auth` | SSRF, redaction, Fernet, safe extraction, sessions |

---

## 3. Component map (VERIFIED)

| Domain | Main files | LOC | External | Tests | Migration criticality |
|---|---|---|---|---|---|
| HTTP/API | `web_server.py` | 4753 | — | `test_web_server`, `test_route_auth`, `test_studio_api` | **High** |
| Auth/RBAC | `web_auth.py` | 645 | — | `test_auth` | **High** |
| Chat/Intent | `nexus_intent`, `nexus_commands`, `nexus_chat` | 673 / 604 / 916 | LLM | `test_intent_router`, `test_commands` | High |
| Conversations | `nexus_conversations.py` | 718 | — | `test_conversations`, `test_conversation_management` | High |
| Orchestration | `nexus_lab.py`, `nexus_lab_store.py` | 2276 / 2398 | OpenHands | `test_lab_*`, `test_durable_runs` | **Critical** |
| Planning | `nexus_specs`, `nexus_spec_planner`, `nexus_planner` | 1431 / 1145 | LLM | `test_nexus_planning`, `test_plan_draft` | **Critical** |
| OpenHands | `cloud_client`, `cloud_direct`, `openhands_client`, `openhands_router`, `openhands_pool` | 1024 / 893 / 530 | OpenHands Cloud | `test_openhands_*` | **Critical** |
| Projects | `nexus_projects.py` | 290 | — | `test_nexus_projects`, `test_project_lifecycle` | High |
| Files/Lab | `file_lab.py`, `nexus_lab*.py`, `archive_safety.py` | 412 / 335 | — | `test_file_lab`, `test_archive_safety` | Medium |
| GitHub | `github_client`, `nexus_github_accounts` | 631 / 204 | GitHub API | `test_github_*` | Medium |
| MCP | `mcp_server.py` | 423 | — | `test_cli_mcp` | Medium |
| CLI | `cli.py` | 965 | — | `test_cli_mcp` | Medium |
| Evidence | `nexus_evidence`, `evidence_trust` | 391 | Ed25519 | `test_evidence_trust` | Medium |
| Frontend | `studio/app.js` + assets | 3438 (JS) | — | `test_browser_e2e` | Medium |
| Security | `nexus_policy`, `net_guard`, `secrets` | 387 / 302 / 51 | Fernet | `test_security_checks` | **High** |

**Implemented in code but without a UI screen** (VERIFIED,
`docs/KNOWN_LIMITATIONS.md`): lab file uploads, workspaces, voice notes,
evidence, plan approval, GitHub create-repository and visibility
confirmations, the legacy task form and queue, system diagnostics, the
allowlist editor, OpenHands account management. Reachable only via API/CLI/MCP.

**Design only (not implemented):** the MCP *client*, the Self-QA engine,
PDF/DOCX/XLSX/PPTX preview, the iOS client.

### 3.1 Dependency direction (VERIFIED, INFERRED)

`web_server` → (`nexus_intent`, `nexus_commands`, `nexus_chat`,
`nexus_conversations`, `nexus_generation`, `nexus_lab`, `nexus_lab_store`,
`nexus_projects`, `nexus_files`, `nexus_github_accounts`,
`nexus_openhands_accounts`, `mcp_server`) → (`cloud_store`, `cloud_client`,
`cloud_direct`, `github_client`, `providers`) → PostgreSQL / OpenHands / GitHub
/ LLM. The store is the only writer of durable state; `LabStore` is the only
writer of run/task terminal state. This single-writer discipline is the
invariant the Rust design must preserve.

---

## 4. Performance analysis (MEASURED / INFERRED)

### 4.1 Backend

- Synchronous standard-library HTTP (`ThreadingHTTPServer`), not asyncio. There
  is no "blocked event loop" because there is no event loop; concurrency is
  bounded by threads. **VERIFIED.**
- **One DB connection per process, behind an `RLock`** (`cloud_store.py`).
  This serializes all database operations. It is the real ceiling for
  concurrency, but on the current load it is not a measured hotspot.
  **VERIFIED (code) / INFERRED (impact).**
- Reconnect happens on evidence of a dropped connection, with no round-trip on
  a healthy connection (`docs/PERFORMANCE.md`). **VERIFIED.**

### 4.2 Orchestration engine

- `OpenHandsExecutor.poll_interval = 2.0s`, `start_poll_interval=2.0s`,
  `start_poll_attempts=40` (~5 min) — `web_server.py:598-602`. **VERIFIED.**
- `LabStore` uses `FOR UPDATE SKIP LOCKED`, a lease and a fencing token,
  dependency gating on `completed_verified`, branch exclusivity and recovery;
  `pg_advisory_xact_lock` guards the active-task limit
  (`nexus_lab_store.py:923-927`); `DEFAULT_MAX_ACTIVE_TASKS = 8`. **VERIFIED.**
- **"Several independent tasks in one conversation run concurrently" is
  intentional product behaviour, not a bug** (VERIFIED):
  `tests/test_concurrent_runs_policy.py` pins the contract, and a prior attempt
  to forbid it was reverted because it broke the browser e2e. Idempotency is
  per-text / per-goal (`nexus_specs.py`).

### 4.3 OpenHands

- HTTP 429 is handled with `Retry-After` (`cloud_client.py:312-322`,
  `cloud_direct.py:423, 605`); `failure_classification` honours
  `retryable: false`. **VERIFIED.**
- Known limitation: cancelling a run, a deadline, or a task time limit does
  **not** stop a conversation already running in the Cloud; "resume" after a
  timeout starts a second conversation. **VERIFIED.**

### 4.4 Frontend

- Static, no build step; `app.js` is 3438 lines; the whole `studio/` tree is
  ~428 KB. **MEASURED (on disk).**
- Polling: `setInterval(tickPending, 1000)`, `setTimeout(refreshWork, 5000)`
  (`app.js:946, 1154`). **VERIFIED.**

### 4.5 Render

- Free plan: spin-down after ~15 minutes idle, cold start on the next request,
  no shell, no persistent disk. **VERIFIED (`render.yaml`).**
- **MEASURED** (last 24h, Render MCP): CPU ≈ **0.048–0.054 core**, RAM ≈
  **197–217 MB**, instances: 1. `http_request_count` and `instance_count`
  returned empty — **BLOCKED** for latency/throughput.

### 4.6 Where the delay actually is (INFERRED)

Internal (NEXUS): DB reads (single-digit ms), serialization on the store lock.
External: OpenHands Cloud (conversation creation, poll, execution), LLM
(planning, answering), GitHub. The external part dominates. `chat_turn`
p50 23–27 ms and `start_run` p50 47–65 ms are measured against a local
stand-in provider, so they are NEXUS overhead plus a local LLM, not provider
latency. **MEASURED (stand-in).**

---

## 5. Real bottlenecks (INFERRED) and the performance baseline

### 5.1 Bottlenecks

1. **One DB connection + a global `RLock`** — serializes all requests. The
   ceiling under growth.
2. **Render Free spin-down** — the autonomous loop stops; a run does not
   advance while the instance sleeps.
3. **2s polling** × tasks × accounts — external round-trips to OpenHands.
4. **No async for external I/O** in the hot path (planning, poll).
5. **Nothing to optimise on the NEXUS side** — the measured profile says so.

### 5.2 Performance baseline (MEASURED)

Source: `docs/PERFORMANCE.md` (two runs, the second larger).

| Path | p50 (ms) | p90 (ms) | Note |
|---|---:|---:|---|
| studio_status | 0.82–1.16 | 1.43–4.76 | read |
| list_projects | 1.11–1.34 | 1.27–2.04 | read |
| open_project | 1.72–3.32 | 1.83–4.66 | read |
| list_conversations | 1.42–2.61 | 1.49–3.50 | read |
| open_conversation | 1.44–1.71 | 2.29–2.42 | read |
| fetch_history | 1.74–2.31 | 2.11–2.77 | 20/40 msgs |
| commands_registry | 0.77–0.83 | 0.93–0.95 | static |
| search_by_content | 1.71–1.85 | 2.02–2.05 | bounded EXISTS |
| export_conversation | 2.38–3.58 | 2.72–5.37 | read |
| chat_turn | 23.30–27.35 | 23.92–36.99 | provider-bound |
| start_run | 47.35–65.07 | 61.50–79.66 | provider-bound |
| store_dashboard_counts | 0.55–0.61 | 0.64–2.09 | aggregate |
| file_upload (4 KiB) | 3.14–3.78 | 4.04–8.20 | write |

Infrastructure (MEASURED, Render MCP, 24h): CPU 0.048–0.054 core;
RAM 197–217 MB.

### 5.3 Metrics not yet measured (UNKNOWN / BLOCKED)

| Metric | Status | What is needed |
|---|---|---|
| API latency p95/p99 in production | BLOCKED | Render paid metrics or OTel tracing |
| Task creation / planning / dispatch time (prod) | UNKNOWN | OTel spans on a staging deploy |
| OpenHands startup / task completion time | UNKNOWN | provider-side timestamps + tracing |
| Queue delay | UNKNOWN | tracing + a load profile |
| Concurrency under load | UNKNOWN | a load test on staging (not production) |
| Recovery time after a crash | UNKNOWN | failure injection on staging |

### 5.4 The instrument (VERIFIED)

`tools/benchmark.py` runs the real server against a real PostgreSQL and the
same stand-in OpenHands/GitHub/LLM servers the test suite uses, and reports
min/p50/p90/p99/max in ms. `tools/benchmark_compare.py before.json after.json`
prints the change per path. This is the only accepted instrument for a speed
claim.

### 5.5 What must never be claimed

- A speed-up of an LLM- or OpenHands-bound path from a language change.
- A production p95/p99 without production metrics.
- A "faster" result measured with a different scenario than the baseline.

### 5.6 Baseline reproducibility requirement

Any future Rust benchmark must use the **same** scenario definition
(`tools/benchmark.py`'s `results["measurements"]` entries), the same dataset
shape (`--projects`, `--conversations`, `--messages`), and the same stand-in
servers, or the comparison is invalid.

---

## 6. Render and PostgreSQL audit

### 6.1 Render (VERIFIED / MEASURED)

| Parameter | Live (Render MCP) | `render.yaml` in repo | Divergence |
|---|---|---|---|
| Region | **frankfurt** | **oregon** | **YES** |
| Services | web only | web **+ worker** (`python-dispetcher-lab-worker`) | **YES** |
| Plan | free | free | — |
| Auto-deploy | enabled | `autoDeploy: true` | — |
| Health check | `/healthz` | `/healthz` | — |
| Start command | `python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token "$WEB_AUTH_TOKEN"` | same | — |

**Deploy history (VERIFIED):**

| Commit | Status | Note |
|---|---|---|
| `42609a4` (PR #37) | **live** | the running build |
| `f3767c9` | update_failed | nonZeroExit=1 |
| `c2b5fe8` | update_failed | nonZeroExit=1 |
| `a6bb451` | update_failed | nonZeroExit=1 |
| `8fba2b6` | update_failed | nonZeroExit=1 |
| `b2f3028` | build_in_progress | at time of audit |

**Production state (VERIFIED):** `/healthz` → `200 {"status":"ok"}`;
`/readyz` → `200 {"status":"ready","database":true}`; `/api/system` → `401`
in ~0.18 s; `/` → `303`. Header `x-render-origin-server: SrJrDispatcher/2.0
Python/3.12.6`.

**Production vs `main` (VERIFIED):** production is **not** on the latest
`main`. Live = `42609a4`; `main` tip = `b2f3028`. Intermediate deploys fail.

### 6.2 PostgreSQL (VERIFIED)

- **Provider: Supabase** (external). No Render-managed Postgres in the
  workspace. DSN from `SUPABASE_DB_URL`; the **transaction pooler, port 6543**
  (PgBouncer) is recommended, with `prepare_threshold=None`
  (`cloud_store.py:242-266`; `DEPLOY.md:432-454`).
- **37 migration files** (numbering has gaps: 0012/0013 absent), **50 tables**,
  **94 indexes**.
- Migrations are applied by the process itself (`AUTO_MIGRATE=true`),
  idempotently, with a checksum and a **hard refusal to re-run an edited,
  already-applied migration** (`cloud_store.py:290-341`).
- **RLS** is enabled on many tables; `TO authenticated` policies are created
  **only if the role exists** (`0001_init.sql:115`, `0002_sr_runtime.sql:44`),
  so on a plain PostgreSQL they are a no-op. Revokes are done with a
  `DO ... FOR r IN SELECT rolname FROM pg_roles` loop (safe).

### 6.3 The `role "(authenticated)" does not exist` error — root cause (VERIFIED)

**File:** `migrations/0037_conversation_management.sql`, **line 54**.

```sql
DECLARE r record;      -- the defect; every other migration: DECLARE r text;
...
FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')
LOOP
    EXECUTE format('REVOKE ALL ON nexus_generations FROM %I', r);
END LOOP;
```

`format('%I', <record>)` serializes the **whole row** to `(authenticated)` and
quotes it, producing `REVOKE ... FROM "(authenticated)"`. No role has that
name, so the statement raises `UndefinedObject` and **the entire file's
transaction rolls back**, aborting process start.

**Production log (VERIFIED, Render runtime logs):**

```
psycopg.errors.UndefinedObject: role "(authenticated)" does not exist
CONTEXT:  SQL statement "REVOKE ALL ON nexus_generations FROM "(authenticated)""
```

**Genealogy (VERIFIED):**

- The file was added by commit `4db5b83` (2026-11-14 16:17 UTC) **already
  defective**.
- `4db5b83` is **not an ancestor** of the live commit `42609a4`, so production
  does not have migration 0037; its database is at 0036.
- Every deploy that carries 0037 fails, so production is frozen.

**Where the fix exists (VERIFIED):**

- `origin/main` (`8fba2b6` / `b2f3028`) and PR #39 both **still carry the
  defect**.
- A ready fix exists **only in the local, unpushed branch**
  `nexus/perf-reliability-lifecycle` (tip `82f5ae7`, **18 commits ahead of its
  remote**): `DECLARE r text;` plus a `MIGRATION_CORRECTIONS` map in
  `cloud_store.py` that re-applies the corrected body for a database that
  recorded the broken checksum.

**Database migration strategy:** the Python version must remain available for
rollback until the migration is confirmed complete. No DROP / DELETE / TRUNCATE
is performed. (Task constraint.)

---

## 7. OpenHands integration audit (VERIFIED)

- Clients: `CloudClient` (the `app` transport) and `CloudDirectClient` (the
  `direct` transport; provisions a sandbox and drives its agent-server).
- Account pool: `openhands_pool` (selection_mode, priority,
  `max_concurrent_tasks`), router with failover and `tried_accounts`.
- Rate limit / timeouts: 429 with `Retry-After`; `CloudClient.timeout=30s`,
  `CloudDirectClient.timeout=60s`.
- **External latency** (conversation creation, execution) must not be
  attributed to Python.
- BLOCKED: real OpenHands Cloud latencies are not available from
  production-side metrics.

**Explicit statement (required by the task):** Rust does **not** guarantee
faster LLM- or OpenHands-bound operations. Those operations are dominated by
provider round-trips and model inference, which are unchanged by the
implementation language. Only the NEXUS-side overhead (JSON shaping, DB
access, scheduling) can be affected, and that overhead is already measured at
single-digit milliseconds on the read paths.

---

## 8. Frontend audit (VERIFIED)

- Static files, no build step, ~428 KB on disk, no framework; events are
  delegated from `document` through `data-action`.
- Polling at 1 s and 5 s; PWA (service worker, manifest).
- Mobile adaptation through `app.css` (792 lines) plus e2e width tests.
- UNKNOWN: on-the-wire (gzip) bundle size; real low-end-mobile metrics.

---

## 9. Security audit (VERIFIED)

- Auth: PBKDF2-HMAC-SHA256, **600,000 iterations** (`web_auth.py:74`),
  server-side sessions (only the HMAC of the token is stored), double-submit
  CSRF, lockout, rate limiting.
- Secrets: Fernet from `NEXUS_SECRET_KEY`; a missing key is a **hard refusal**,
  never a plaintext write (`secrets.py`).
- SSRF: `net_guard.py`; archives: `archive_safety.py` (a member name never
  becomes a path).
- RLS + revokes on sensitive tables (including `conversation_routes`,
  `cloud_sandboxes` — `0016`).
- **Risk:** the auth model is **single-owner** (`AUTH_USERNAME` /
  `AUTH_PASSWORD_HASH`); there is no per-user account system. Project isolation
  is enforced by membership rows (a non-member gets 404).
- The repository is **public** (VERIFIED) — relevant to secret hygiene.

---

## 10. Rust target architecture (INFERRED — a recommendation)

**Modular monolith, not microservices.** There is no proven need to split, and
splitting would fracture the single-writer invariants that `AGENTS.md` records
in one place.

### 10.1 Backend

- Rust stable, `tokio`, `axum`, `sqlx` (PostgreSQL), `serde`, `tracing` +
  `tracing-subscriber`, `opentelemetry`.
- `axum` replaces the stdlib `ThreadingHTTPServer`; `sqlx`'s pool replaces the
  single connection + `RLock`, removing the serialization ceiling.
- **Trade-off:** `sqlx` is compile-time-checked SQL but requires either a live
  database at build time or an offline query cache; this is a real build-pipeline
  cost to plan for.

### 10.2 Frontend

- React + TypeScript + the existing design system (after a design audit),
  mobile-first.

### 10.3 Desktop

- Tauri 2 (Rust) → Linux `.deb`, AppImage, Windows `.exe`, `.msi`.

### 10.4 Android

- Tauri 2 (APK/AAB) **to be evaluated** for the required capabilities; if it
  cannot meet them, an alternative (Kotlin/JNI or a WebView shell) is the
  fallback. Not assumed.

### 10.5 Integrations

- OpenHands Cloud API, GitHub, MCP, PostgreSQL, external LLM providers.

### 10.6 Justification per decision

| Decision | Pros | Cons | Risk | Maintenance cost |
|---|---|---|---|---|
| Rust + tokio + axum | types, async I/O, no lock ceiling, one binary | 77.5k LOC + 1488 tests to port; learning curve | divergence from Python invariants | high during migration, lower after |
| `sqlx` | compile-checked SQL, pooling | build-time DB or offline cache | migration tooling friction | medium |
| Modular monolith | one deployment, shared invariants | less independent scaling | low | low |
| Tauri 2 | one Rust stack for desktop/mobile | WebView per-OS differences | Android capability gap | medium |

---

## 11. Proposed module boundaries (INFERRED)

Preliminary domains (not a mandated number of services):

1. `nexus-core` — shared types and business contracts.
2. `nexus-api` — HTTP API.
3. `nexus-auth` — authentication and RBAC.
4. `nexus-projects` — project management.
5. `nexus-conversations` — chats and history.
6. `nexus-orchestrator` — planning and run management.
7. `nexus-scheduler` — queues and dispatch.
8. `nexus-openhands` — OpenHands integration.
9. `nexus-llm` — LLM providers.
10. `nexus-memory` — memory.
11. `nexus-github` — GitHub integration.
12. `nexus-mcp` — MCP server/client.
13. `nexus-files` — File Laboratory.
14. `nexus-notifications` — notifications.
15. `nexus-cli` — CLI.
16. `nexus-observability` — metrics, tracing, health.

**What stays a library inside one backend:** `nexus-core`, `nexus-projects`,
`nexus-conversations`, `nexus-memory`, `nexus-files`, `nexus-notifications`,
`nexus-observability` (in-process crates).
**What may become a separate process later, only with evidence:**
`nexus-scheduler` (the run loop) and `nexus-cli` (already a separate binary).

---

## 12. Python → Rust parity matrix (functional)

**Definition (required):** an API route is **not** counted as a delivered user
function unless it is reachable through the UI (or explicitly declared a
machine-only surface: CLI/MCP/API). "Exists in code" ≠ "available to a user".

Legend: **UI** = reachable in the studio; **API/CLI/MCP** = machine surface;
**Parity** = the Rust port must reproduce it; **Verify** = how parity is proven.

| Feature | Implemented | Where | Surface | Data | Tests | Rust module | Parity check |
|---|---|---|---|---|---|---|---|
| Projects: create/rename/read | ✅ | `nexus_projects.py` | UI + API | `nexus_projects` | `test_nexus_projects` | `nexus-projects` | contract + e2e |
| Projects: archive/restore | ✅ | `nexus_projects.py` (`set_archived`) | UI + API | `archived_at` | `test_project_lifecycle` | `nexus-projects` | lifecycle e2e |
| Projects: trash/restore | ✅ | `nexus_projects.py` (`delete`, restore) | UI + API | `deleted_at` | `test_project_lifecycle` | `nexus-projects` | lifecycle e2e |
| Projects: permanent delete | ❌ by design | — | — | — | — | — | no hard delete (see §A) |
| Conversations: create/list/rename | ✅ | `nexus_conversations.py` | UI + API | `nexus_conversations` | `test_conversations` | `nexus-conversations` | contract + e2e |
| Conversations: archive/pin/trash/restore | ✅ | `nexus_conversations.py` | UI + API | `archived`,`pinned`,`deleted_at` | `test_conversation_management` | `nexus-conversations` | lifecycle e2e |
| Conversations: retention purge | ✅ (worker) | `nexus_lab_store.queue_cleanups` | background | `nexus_conversation_cleanups` | `test_conversation_cleanup` | `nexus-scheduler` | worker e2e |
| Messages: read/history | ✅ | `nexus_conversations.py` | UI + API | `nexus_conversation_messages` | `test_conversations` | `nexus-conversations` | contract |
| Messages: edit/delete | ❌ | — | — | — | — | — | see §A |
| Runs: start/advance/complete | ✅ | `nexus_lab.py`, `nexus_lab_store.py` | UI + API | `nexus_runs`,`nexus_run_tasks` | `test_durable_runs` | `nexus-orchestrator` | scenario suite |
| Runs: cancel / retry / hide / remove | ✅ | `nexus_lab_store.py` | UI + API | `hidden_at`, states | `test_run_history` | `nexus-orchestrator` | lifecycle e2e |
| Tasks: dispatch/poll/verify | ✅ | `nexus_lab.py` | background | `nexus_run_tasks` | `test_lab_verifier` | `nexus-orchestrator` | scenario suite |
| OpenHands accounts: CRUD/rotate/revoke | ✅ | `nexus_openhands_accounts.py` | API (no screen) | `nexus_openhands_accounts` | `test_openhands_account_admin` | `nexus-openhands` | API contract |
| OpenHands pool: mode/priority/limits | ✅ | `openhands_pool.py` | API | `nexus_openhands_pool` | `test_openhands_pool` | `nexus-openhands` | API contract |
| OpenHands failover/handoff | ✅ | `openhands_router.py` | background | `nexus_task_handoffs` | `test_openhands_failover` | `nexus-openhands` | failure injection |
| LLM providers: cascade/probe | ✅ | `providers.py` | API | — | `test_providers` | `nexus-llm` | parity + live probe |
| GitHub: read/discovery | ✅ | `github_client.py` | UI + API | `nexus_github_accounts` | `test_github_client` | `nexus-github` | live read |
| GitHub: write (create repo/visibility) | ✅ guarded | `github_client.py` | API (no screen) | confirmations | `test_github_write_http` | `nexus-github` | guarded e2e |
| Files: upload/preview/download/delete | ✅ | `file_lab.py`, `nexus_files.py` | API (no screen) | `nexus_files` | `test_file_lab` | `nexus-files` | API contract |
| Workspaces: create/list/delete/diff | ✅ | `file_lab.py` | API (no screen) | `nexus_workspaces` | `test_lab_store` | `nexus-files` | API contract |
| Archives: safe extraction | ✅ | `archive_safety.py` | API | — | `test_archive_safety` | `nexus-files` | security tests |
| MCP server (stdio + HTTP) | ✅ | `mcp_server.py` | MCP | — | `test_cli_mcp` | `nexus-mcp` | protocol tests |
| MCP client | ❌ design | — | — | — | — | `nexus-mcp` | not in scope |
| Notifications | ✅ (API) | `0018`, `nexus_lab_store` | API | `nexus_notifications` | — | `nexus-notifications` | parity |
| Memory: remember/retrieve/supersede | ✅ | `nexus_context.py` | background/API | `nexus_memory` | `test_context_memory` | `nexus-memory` | parity |
| Settings/config | ✅ | `web_server.py` (`/api/config`) | API | env-derived | `test_studio_api` | `nexus-api` | contract |
| Users/Access | ⚠️ single-owner | `web_auth.py` | UI (login) | `sessions` | `test_auth` | `nexus-auth` | security tests |
| RBAC / project isolation | ✅ | membership rows | — | `nexus_project_members` | `test_route_auth` | `nexus-auth` | 404 tests |
| Voice input | ⚠️ storage only | `voice.py` | API (no screen) | `nexus_voice_notes` | — | `nexus-files` | parity |
| Device sync | ⚠️ via polling | — | — | — | — | — | UNKNOWN |
| Adaptive UI | ✅ | `studio/` | UI | — | `test_browser_e2e` | React | visual + e2e |
| CLI | ✅ | `cli.py` | CLI | — | `test_cli_mcp` | `nexus-cli` | golden CLI |

**Rule:** rows marked "API (no screen)" are **not** counted as user-facing
functions until a screen exists; the parity plan must state, per row, whether
the Rust port also adds a screen or remains machine-only.

---

## 13. Full CRUD lifecycle matrix

See **Appendix A** for the full per-entity matrix (Projects, Conversations,
Messages, Tasks, Runs, OpenHands Accounts, LLM Providers, GitHub, Files, MCP,
Notifications, Memory, Settings, Users and Access), each with CRUD, archive,
trash, restore, permanent delete, dependencies, RBAC and API contracts, and an
explanation where an operation is not applicable.

---

## 14. Speed-claim protocol (required)

1. No claim of speed-up is made without a benchmark run by
   `tools/benchmark.py` under the **same** scenario as the baseline (§5.6).
2. LLM- and OpenHands-bound paths are excluded from any speed claim by
   construction; they are provider-bound (§7).
3. A Rust vs Python comparison uses `tools/benchmark_compare.py` and reports
   p50/p90 per path, with the raw JSON artefacts committed alongside.
4. Infrastructure-bound differences (Render Free vs paid, cold start) are
   reported separately from code differences.

---

## 15. Migration strategy (INFERRED)

### 15.1 Options compared

| Option | Complexity | Risk | Time | Data | Rollback | API compatibility | Ops cost |
|---|---|---|---|---|---|---|---|
| A. Replace Python modules incrementally | medium | medium | long | preserved | partial | hard | dual stack |
| **B. Parallel Rust backend, compatible API** | high | **low** | long | preserved | **full** | good | dual stack |
| **C. Domain-by-domain cutover via routing** | medium | **low** | medium | preserved | **full** | **excellent** | proxy |

### 15.2 Recommendation: B + C (a strangler)

The preferred option keeps the current system working until verification is
complete: a Rust backend behind a reverse proxy, cut over one domain at a time,
starting with read-only paths.

### 15.3 Plan

1. **First, unblock production** — fix migration 0037 and ship the increment.
   Do not start Rust while live lags `main`.
2. Rust backend behind a reverse proxy (Render/VPS); cut over domain by domain:
   read-only first (`/api/commands`, list endpoints), then chats, then
   orchestration.
3. **Database:** the same PostgreSQL; Rust reads/writes through `sqlx`; the
   migration directory `migrations/` stays the single source of truth.
4. **Traffic:** canary per domain; Python stays the fallback.

### 15.4 Traffic cutover criteria, canary, rollback, schema compatibility

- **Cutover criteria (per domain):** parity tests green; canary at 5% with
  error rate ≤ the Python baseline and p50 within ±10%; no data divergence for
  the canary window.
- **Canary:** a header- or percentage-based route at the proxy; a per-domain
  switch, never a global flip.
- **Rollback:** the proxy switches the domain back to Python in one step; both
  processes read the same database.
- **PostgreSQL schema compatibility:** migrations are **additive and
  idempotent**; a new column/table is added by one side and readable by the
  other; no destructive change during the migration window.

### 15.5 Prohibition on incompatible simultaneous writes

**No entity may be written by both Python and Rust at the same time.** During
migration each domain has exactly **one writer**:
- the writer is the process that owns that domain's route at the proxy;
- the other process may **read** but must not write that domain;
- ownership is recorded in a table (a cutover ledger) and enforced by the
  proxy routing (only the owner receives write methods for the domain);
- the store's single-writer discipline (§3.1) is preserved per domain, not
  broken by having two writers.

### 15.6 Python as the working fallback (required)

The Python implementation remains the **working backup implementation for the
entire migration**. It is not decommissioned until every domain passes the
acceptance criteria (§21) and has run stable for an agreed window. The switch
back to Python is always available and is the default on any anomaly.

### 15.7 Criteria to retire Python (per domain)

- Parity matrix rows for the domain are green.
- The §12 scenarios pass on Rust.
- Zero divergence incidents for the agreed stability window.
- Rollback has been exercised at least once in a drill.

### 15.8 Backup strategy

`pg_dump`/`pg_restore` (the existing drill in `tests/test_backup_restore.py`),
plus a point-in-time snapshot before each cutover; the snapshot is the
rollback floor for data.

---

## 16. Infrastructure and hosting (INFERRED)

**The current Render Free is not sufficient for a full NEXUS** (VERIFIED
limits): spin-down after ~15 min idle, no shell, no disk, no throughput
metrics, CPU ≈ 0.05 core.

| Option | Cost | RAM/CPU | Persistent | Workers | WebSocket | Cold start | Scale | Monitoring |
|---|---|---|---|---|---|---|---|---|
| Render Free (today) | $0 | 512 MB / 0.1 | no | no (spins down) | limited | yes | no | limited |
| Render Starter/Standard | $$ | 512 MB–2 GB | limited | yes | yes | no | medium | yes |
| **VPS + Docker (recommended)** | $–$$ | flexible | yes | yes | yes | no | high | full (OTel) |
| Kubernetes | $$$ | flexible | yes | yes | yes | no | high | full |

**Recommendation:** VPS + Docker Compose (app + lab worker + managed
PostgreSQL/Supabase), with a budget tier and a mid tier. Render is fine as a
quick start, not for unattended runs. No migration or plan change is performed
here.

---

## 17. Testing strategy (VERIFIED — the infrastructure already exists)

Existing: 1,488 test functions, PostgreSQL 17 in CI (`ubuntu-26.04`,
`timeout-minutes: 50`), Chromium for the browser e2e, a real `pg_dump`/
`pg_restore` drill, stand-in servers (`fake_cloud`, `fake_github`,
`fake_runtime`).

To add for Rust: unit, integration, API contract, PostgreSQL integration,
concurrency, race-condition, failure injection, recovery, OpenHands
integration, end-to-end, frontend, mobile, desktop build, security and
performance benchmark suites.

### 17.1 Mandatory scenarios and pass criteria

| # | Scenario | Pass criterion |
|---|---|---|
| 1 | Two independent tasks in one conversation | both visible; idempotency per text |
| 2 | Refusal of conflicting concurrent launches | no duplicate run for the same text |
| 3 | Different projects in parallel | isolation; no cross-talk |
| 4 | Cancel a task | only the target run changes |
| 5 | Server restart during a task | recovery without loss |
| 6 | Recovery of unfinished tasks | lease reclaim |
| 7 | Loss of connection to OpenHands | retry/backoff; no hang |
| 8 | LLM error | clean cascade failover |
| 9 | Loss of PostgreSQL connection | reconnect on evidence |
| 10 | Repeated request after a timeout | idempotent |
| 11 | One OpenHands account unavailable | failover to another |
| 12 | Re-run of a finished task | refused; no second run |
| 13 | Project isolation | 404 for a non-member |
| 14 | Project deletion and its data | archive/trash per rules |
| 15 | State sync between clients | consistent state |

---

## 18. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Production frozen on `42609a4`; deploys fail | **Realised** | **High** | Fix `0037` (`record`→`text`) + `MIGRATION_CORRECTIONS`; ship |
| R2 | The fix lives only in an unpushed local branch (18 commits) | **Realised** | **High** | Publish the branch / PR; merge |
| R3 | `render.yaml` (oregon + worker) diverges from live (frankfurt, no worker) | High | Medium | Reconcile the blueprint (separate analysis) |
| R4 | Rust rewrite without a latency benefit | Medium | Medium | Prioritise by the measured profile |
| R5 | Duplicated Python↔Rust invariants | Medium | High | Modular monolith, shared contracts |
| R6 | Loss of a function during migration | Medium | High | Parity matrix + contract tests |
| R7 | Single-owner auth | Medium | Medium | Account for it in `nexus-auth` design |
| R8 | Cancel does not stop a Cloud conversation | High | Medium | Documented; design for it in Rust |
| R9 | Render Free spin-down breaks autonomy | High | High | Paid plan / VPS |
| R10 | Two writers on one domain during migration | Medium | High | One-writer-per-domain rule (§15.5) |

---

## 19. Estimated implementation phases (no calendar promises)

| Phase | Goal | Components | Depends on | Risks | Tests | Acceptance | Rollback |
|---|---|---|---|---|---|---|---|
| 0 | Unblock production | `migrations/0037`, `cloud_store` | — | R1/R2 | `test_durable_migrations` | live = main; green deploy | previous deploy |
| 1 | Rust core skeleton | `nexus-core` (types/contracts) | — | — | unit | types compile; type parity | delete the crate |
| 2 | Read-only API | `nexus-api` + `sqlx` | P1 | R5 | contract | read parity on canary | proxy→Python |
| 3 | Auth/RBAC | `nexus-auth` | P2 | R7 | security | login/404 parity | proxy→Python |
| 4 | Conversations | `nexus-conversations` | P3 | — | e2e | chat parity | proxy→Python |
| 5 | Orchestrator + scheduler | `nexus-orchestrator`, `nexus-scheduler` | P4 | R6 | recovery/race | scenarios 1–15 | proxy→Python |
| 6 | OpenHands/LLM/GitHub | `nexus-openhands`, `nexus-llm`, `nexus-github` | P5 | — | integration | live smoke | proxy→Python |
| 7 | Desktop/Mobile | Tauri 2 | P6 | — | build | `.deb/.exe/.msi/.apk` artefacts | — |
| 8 | Retire Python (per domain) | — | P7 | R6 | full suite | zero divergence for the window | keep Python |

Each phase is small and independently verifiable before the next.

---

## 20. Rollback strategy (INFERRED)

- **Deploy level:** Render "Deploy previous" → `42609a4` (the proven live
  build).
- **Database level:** migrations are additive and idempotent; rollback is
  forward-only (new migrations), never destructive.
- **Rust-migration level:** the reverse proxy switches a domain back to Python
  immediately; both read one database; the one-writer rule (§15.5) means no
  domain was ever double-written.
- **Python stays available** until the migration is confirmed complete.

---

## 21. Acceptance criteria

1. Production = current `main`; deploys green; `/healthz` and `/readyz` OK.
2. The `role "(authenticated)" does not exist` error is fixed and covered by a
   static guard test (as in the local branch:
   `tests/test_migration_sql_guards.py`).
3. The parity matrix (§12) is fully green, or exceptions are explicitly
   documented.
4. The §17.1 scenarios (1–15) pass on Rust.
5. Benchmarks: read paths not worse than the Python baseline; p95/p99 measured
   on staging.
6. Rollback exercised in a drill.
7. `render.yaml` reconciled with reality (separate analysis).

---

## Appendix A — Full CRUD lifecycle matrix

For every entity: **C**reate, **R**ead, **U**pdate, **D**elete, plus archive /
trash / restore / permanent delete, dependencies, RBAC and API contract. Where
an operation does not apply, the reason is given.

Conventions: RBAC is membership-scoped (`principal` + project membership);
a non-member receives **404**, never 403 (`web_server.py`, `test_route_auth`).
"No permanent delete" is a deliberate design choice (soft delete + retention),
not a gap, unless stated.

### A.1 Projects — `nexus_projects` (`nexus_projects.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create | ✅ | `create` | `POST /api/v1/projects` | member of new project (owner) | — |
| Read (list) | ✅ | `list` | `GET /api/v1/projects` (`?archived=1`, `?deleted=1`) | member | hides archived/deleted by default |
| Read (one) | ✅ | `get` | `GET /api/v1/projects/{id}` | member | 404 for a non-member |
| Update (rename/description) | ✅ | `rename`/update | `PATCH /api/v1/projects/{id}` | member | — |
| Archive / restore | ✅ | `set_archived` | `PATCH /api/v1/projects/{id}` with `{"archived": bool}` | member | reversible; data untouched |
| Trash (soft delete) / restore | ✅ | `delete` / restore | `PATCH /api/v1/projects/{id}` with `{"deleted": bool}` | member | refuses while an active run exists |
| Permanent delete | ❌ by design | — | — | — | nothing is hard-deleted; no cascade to get wrong (`0038`) |
| Dependencies | — | — | — | — | conversations, runs, repo links (kept on archive; new work refused in trash) |

### A.2 Conversations — `nexus_conversations` (`nexus_conversations.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create | ✅ | `create` | `POST /api/v1/projects/{id}/conversations` | member | refused if the project is in the trash |
| Read (list) | ✅ | `list` | `GET .../conversations` | member | pinned first; archived/deleted excluded by default |
| Read (one) | ✅ | `get` | `GET .../conversations/{cid}` | member | joins the repository |
| Update (rename/mode) | ✅ | `rename`, `set_mode` | `PATCH .../conversations/{cid}` with `{"title"}` / `{"mode"}` | member | — |
| Pin / unpin | ✅ | `set_pinned` | `PATCH .../conversations/{cid}` with `{"pinned": bool}` | member | durable; does not touch `updated_at` |
| Archive / unarchive | ✅ | `set_archived` | `PATCH .../conversations/{cid}` with `{"archived": bool}` | member | `DELETE` archives; `PATCH {"archived": false}` restores |
| Trash / restore | ✅ | `set_deleted` | `PATCH .../conversations/{cid}` with `{"deleted": bool}` | member | a stamp (`deleted_at`) |
| Permanent delete | ✅ (retention) | `purge_deleted` (worker) | background | system | after the retention window |
| Dependencies | — | — | — | — | messages, generations, runs |

### A.3 Messages — `nexus_conversation_messages`

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create | ✅ | via chat turn | `POST .../conversations/{cid}/message` | member | the turn writes it |
| Read (history) | ✅ | `nexus_conversations` | `GET .../messages` | member | capped page (500) |
| Update | ❌ | — | — | — | messages are an append-only history; no edit route |
| Delete (single) | ❌ | — | — | — | removed with the conversation, not individually |
| Archive / trash / restore | n/a | — | — | — | a message has no lifecycle of its own |
| Permanent delete | ✅ (with conversation) | purge | background | system | cascade with the conversation |

### A.4 Tasks — `nexus_run_tasks` (`nexus_lab_store.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create | ✅ | `start_run` (planner) | via the chat turn | member | created as part of a run DAG |
| Read | ✅ | store queries | API/UI | member | part of a run view |
| Update (state/lease) | ✅ | `claim_task`, `retry_task` | background | system | lease + fencing token |
| Retry | ✅ | `retry_task` | via run retry | member | bounded |
| Delete | ❌ | — | — | — | tasks belong to a run; removed with it |
| Archive / trash / restore | n/a | — | — | — | lifecycle is the run's |

### A.5 Runs — `nexus_runs` (`nexus_lab_store.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create (start) | ✅ | `start_run` | via the chat turn | member | one active run per goal (partial unique index) |
| Read (list/one) | ✅ | store | `GET .../runs` | member | — |
| Cancel | ✅ | cancel path | `POST /api/workflows/{id}/cancel` | member | does not stop a Cloud conversation (limitation) |
| Retry failed | ✅ | `retry_failed` | `POST .../retry` | member | — |
| Hide / show | ✅ | `hidden_at` | API/UI | member | hides from the active list |
| Remove from history | ✅ | `delete_run` | `DELETE .../runs/{id}` | member | only a finished run; clears the goal pointer |
| Archive / trash / restore | n/a | — | — | — | a run is history; hide/remove covers the need |
| Permanent delete | ✅ | `delete_run` | as above | member | only a terminal run |

### A.6 OpenHands Accounts — `nexus_openhands_accounts`

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create (add) | ✅ | `add` | `/api/openhands/accounts` | owner principal | key encrypted (Fernet) |
| Read (list) | ✅ | `list_accounts` | `GET .../accounts` | owner | key never returned |
| Update (edit/rotate) | ✅ | `edit`, `rotate` | `PATCH .../accounts/{id}` | owner | — |
| Verify / check | ✅ | `verify`, `check_connection` | API | owner | live probe |
| Set default | ✅ | `set_default` | API | owner | — |
| Revoke | ✅ | `revoke` | `DELETE .../accounts/{id}` | owner | — |
| Remove | ✅ | `remove` (force) | API | owner | refuses while in use unless forced |
| Archive / trash / restore | n/a | — | — | — | an account is enabled/disabled, not archived |
| Dependencies | — | — | — | — | pool membership, assigned tasks |

### A.7 LLM Providers — `providers.py` (config-only, not rows)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create / Update | ⚠️ config | env (`GEMINI_API_KEY`, …) | `/api/config` (read) | owner | providers are configured by env, not CRUD rows |
| Read (probe) | ✅ | `probe` | `/api/providers` | member | live status |
| Delete | n/a | — | — | — | removing a key from env disables the provider |
| Archive / trash / restore | n/a | — | — | — | no lifecycle; the cascade order is fixed |

### A.8 GitHub — `nexus_github_accounts` (`nexus_github_accounts.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create (add) | ✅ | `add` | `/api/github/accounts` | owner | token encrypted |
| Read (list) | ✅ | `list_accounts` | `GET .../accounts` | owner | token never returned |
| Update (rotate) | ✅ | `rotate` | `PATCH .../accounts/{id}` | owner | — |
| Verify | ✅ | `verify` | API | owner | live probe |
| Bind to project | ✅ | `bind_project` | API | member | one account per project |
| Revoke | ✅ | `revoke` | `DELETE .../accounts/{id}` | owner | — |
| Read repositories / write (guarded) | ✅ | `github_client` | `/api/github/repositories` | member | writes behind a confirmation token |
| Archive / trash / restore | n/a | — | — | — | accounts are added/revoked |

### A.9 Files — `nexus_files`, `nexus_workspaces` (`file_lab.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create (upload) | ✅ | `upload` | `POST .../files` | member | size cap (`NEXUS_MAX_UPLOAD_BYTES`) |
| Read (list/preview/download) | ✅ | `list_files`, `preview`, download | `GET .../files` | member | text/image inline; others download |
| Update (put workspace file) | ✅ | `put_workspace_file` | `PUT .../workspace-files` | member | — |
| Delete (file) | ✅ | `delete_file` | `DELETE .../files/{id}` | member | — |
| Delete (workspace) | ✅ | `delete_workspace` | `DELETE .../workspaces/{id}` | member | — |
| Archive / trash / restore | n/a | — | — | — | files are uploaded/removed, no archive state |
| Permanent delete | ✅ | `delete_file` | as above | member | immediate |

### A.10 MCP — `mcp_server.py`

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Server (tools) | ✅ | `mcp_server.py` | `POST /mcp` | caller's authority | a tool call carries the authenticated user's role |
| Client (to external MCP) | ❌ design | — | — | — | `docs/MCP_ARCHITECTURE.md` (design only) |
| CRUD lifecycle | n/a | — | — | — | MCP is a transport, not an entity |

### A.11 Notifications — `nexus_notifications`

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create | ✅ | run completion | background | system | written by the lab |
| Read | ✅ | API | `GET .../notifications` | member | — |
| Update (read flag) | ⚠️ | — | — | member | UNKNOWN: no explicit read-flag route found |
| Delete | ⚠️ | — | — | — | UNKNOWN: no explicit delete route found |
| Archive / trash / restore | n/a | — | — | — | a notification is ephemeral |

### A.12 Memory — `nexus_memory`, `nexus_project_context` (`nexus_context.py`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Create (remember) | ✅ | `remember` | internal/API | member | — |
| Read (retrieve) | ✅ | `retrieve` | internal/API | member | excludes superseded |
| Update (supersede) | ✅ | `supersede` | internal | member | old row marked `superseded_by` |
| Delete | ❌ | — | — | — | memory is superseded, not deleted (an audit trail) |
| Archive / trash / restore | n/a | — | — | — | supersede is the lifecycle |

### A.13 Settings — `web_server.py` (`/api/config`)

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Read | ✅ | `/api/config`, `/api/capabilities` | `GET` | member | names/booleans only |
| Update | ⚠️ env | Render env / `.env` | — | operator | not a CRUD entity; changed by env + restart |
| Delete | n/a | — | — | — | — |

### A.14 Users and Access — `web_auth.py`, `nexus_project_members`

| Operation | Applicable | Where | Contract | RBAC | Notes |
|---|---|---|---|---|---|
| Login / logout | ✅ | `web_auth` | `/login`, `/logout` | — | PBKDF2 + server-side session + CSRF |
| Session read | ✅ | `web_auth` | `/api/session` | user | — |
| Create user | ❌ | — | — | — | single-owner model (`AUTH_USERNAME`/`AUTH_PASSWORD_HASH`) |
| Update password | ✅ | operator action | env change | operator | ends sessions |
| Delete user | n/a | — | — | — | one operator principal by default |
| Membership add/remove | ✅ | `nexus_project_members` | internal | project owner | drives RBAC and 404 isolation |
| Archive / trash / restore | n/a | — | — | — | access has no archive state |

**Legend for "not applicable":** the entity has no independent lifecycle
(messages, tasks, generations), or the operation is deliberately excluded by
design (no hard delete; no per-user accounts), or the surface is a transport
(MCP) or configuration (providers, settings).

---

## Appendix B — Evidence index

| Claim | Evidence |
|---|---|
| Production frozen on `42609a4` | Render deploys API (MCP) |
| Deploy failures | Render events: `deploy_ended`, `nonZeroExit=1` |
| `role "(authenticated)"` error | Render runtime logs (psycopg traceback) |
| Migration defect | `migrations/0037_conversation_management.sql:54` |
| Fix location | local branch `nexus/perf-reliability-lifecycle` (`82f5ae7`) |
| Latency baseline | `docs/PERFORMANCE.md`, `tools/benchmark.py` |
| CPU/RAM | Render metrics API (MCP), 24h |
| DB provider | `cloud_store.py:234`, `DEPLOY.md:432-454` |
| Concurrency contract | `tests/test_concurrent_runs_policy.py` |
| Security posture | `web_auth.py`, `secrets.py`, `docs/SECURITY.md` |

---

*End of specification. No implementation, deployment, database change or
traffic change has been performed. Awaiting review and explicit authorization
for the next step (the isolated migration-0037 fix PR).*
