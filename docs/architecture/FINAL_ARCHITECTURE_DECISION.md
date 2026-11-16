# NEXUS — Final Architecture Decision

> **STATUS: DECISION RECORD.** This document records the architecture decision
> of 2026-11-15 and is the entry point for the NEXUS architecture set. It does
> **not** authorize implementation. Rust implementation has not started.
>
> Evidence classes: **VERIFIED** (checked against code/infra), **INFERRED**
> (reasoned conclusion), **UNKNOWN** (insufficient data), **BLOCKED** (cannot
> be checked with current access).

- **Decision date:** 2026-11-15
- **Base commit:** `a16da031ce37cd2e36216d3c084ebb5934e9524f` (`origin/main`)
- **Branch:** `docs/final-rust-architecture`
- **Related documents:**
  [RUST_MIGRATION_BLUEPRINT](./RUST_MIGRATION_BLUEPRINT.md) (forensic source),
  [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md),
  [RUST_WORKSPACE_DESIGN](./RUST_WORKSPACE_DESIGN.md),
  [EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md),
  [PYTHON_RUST_MIGRATION_MATRIX](./PYTHON_RUST_MIGRATION_MATRIX.md),
  [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md),
  [CLIENTS_ARCHITECTURE](./CLIENTS_ARCHITECTURE.md),
  [SELF_QA_ARCHITECTURE](./SELF_QA_ARCHITECTURE.md),
  [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md),
  [RUST_MIGRATION_RISK_REGISTER](./RUST_MIGRATION_RISK_REGISTER.md).

## Superseding refinement (added 2026-11-15)

A later stage refines this decision into a **hybrid** target: the Python control
plane stays the single writer of every domain, and Rust is adopted **only** as
an in-process native library (PyO3/maturin) for components where a measurement
beats the *optimized* Python. This narrows, and does not replace, the decision
below. See:

- [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md) — the hybrid target.
- [RUST_COMPONENT_SELECTION](./RUST_COMPONENT_SELECTION.md) — ranking + framework.
- [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md) — the contract.
- [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md) — the phases.
- [HYBRID_ARCHITECTURE_RISK_REGISTER](./HYBRID_ARCHITECTURE_RISK_REGISTER.md) — the risks.

Where the two differ, the hybrid documents refine the strategy (native library
first, not a full rewrite) and the performance findings
([PERFORMANCE_DRIVEN_RUST_MATRIX](./PERFORMANCE_DRIVEN_RUST_MATRIX.md)) govern:
no component is RUST_FIRST today.

---

## 1. Decision

**VERIFIED (decision, not a code fact):**

1. **Strategy:** a phased Python → Rust **Strangler Migration**. Python remains
   the production implementation throughout.
2. **Target backend:** a Rust **modular monolith** (Rust stable, Tokio, Axum,
   SQLx, Serde, tracing, OpenTelemetry, PostgreSQL) with an API gateway /
   domain router. No microservices without proven operational need.
3. **Python remains a full working fallback** until functional parity is proven
   and Rust has run a stable observation window.
4. **Two equal product tracks** (below).
5. **Clients:** Web, Desktop, Android, CLI — one shared identity, project,
   data, access and event model.
6. **Full CRUD and UI parity are mandatory** for every applicable entity. An
   API route without a reachable UI screen is **not** a delivered user function
   (see §4 and the migration matrix).

---

## 2. Product architecture — two tracks, one foundation

NEXUS is one platform with two product tracks that share Projects, Identity,
Access, Events, Storage and API.

### 2.1 Track A — AI Development Platform (VERIFIED, code exists)

| Capability | Python today | Notes |
|---|---|---|
| Projects | `nexus_projects.py`, `nexus_projects` | create/archive/trash/restore |
| AI IDE / Editor | `nexus_ai_ide` (routes), studio viewer | API-only surfaces exist |
| Terminal | MCP tools / agent side | no first-class NEXUS terminal |
| Git / GitHub | `github_client.py` | read live; writes guarded |
| File Laboratory | `file_lab.py`, `nexus_files.py` | API-only (no screen) |
| MCP | `mcp_server.py` (server) | client is design only |
| Workspace | `nexus_workspaces` | API-only |
| Collaboration | — | **UNKNOWN / not implemented** |
| Notifications | `nexus_notifications` | API-only |

### 2.2 Track B — Autonomous OpenHands Orchestrator (VERIFIED)

Prompt intake → planning (`nexus_specs`, `nexus_spec_planner`) → task
decomposition → DAG (`nexus_plan*`, `nexus_runs`/`nexus_run_tasks`) →
scheduler (`LabStore`) → dispatcher (`OpenHandsExecutor`) → OpenHands accounts
(`openhands_pool`/`router`) → execution → retries → recovery → verification
(`GitVerifier`) → evidence (`nexus_evidence`) → autonomous repair loops
(partial).

### 2.3 Shared foundation (VERIFIED)

Projects, Identity & Access, Events, Storage (PostgreSQL), API. Both tracks
read and write the same project-scoped tables; isolation is by membership
(a non-member gets 404).

---

## 3. Backend architecture (INFERRED — target)

- Rust stable, Tokio, Axum, SQLx, Serde, tracing + tracing-subscriber,
  OpenTelemetry, PostgreSQL.
- Modular monolith; a single binary with an internal domain router.
- `sqlx` connection pool replaces the single connection + `RLock`
  (`cloud_store.py:276-281`) — this removes the serialization ceiling.
- The single-writer invariant (`LabStore` is the only writer of run/task
  terminal state) is preserved per domain.

Trade-off (recorded, not hidden): `sqlx` compile-time-checked SQL needs a live
database at build time or an offline query cache.

---

## 4. UI parity rule (VERIFIED rule; gap counts in the matrix)

**Rule:** a capability counts as delivered only when a user can reach and
complete it in a client. The following are implemented in code but have **no
screen** (VERIFIED, `docs/KNOWN_LIMITATIONS.md`): lab file uploads,
workspaces, voice notes, evidence, plan approval, GitHub create-repository /
visibility confirmations, the legacy task form and queue, system diagnostics,
the allowlist editor, OpenHands account management. They are reachable only
through API/CLI/MCP.

**Consequence for the migration:** the parity matrix
([PYTHON_RUST_MIGRATION_MATRIX](./PYTHON_RUST_MIGRATION_MATRIX.md)) marks each
row's surface, and the roadmap adds the missing screens where the product track
requires them.

---

## 5. Current-state facts this decision rests on (VERIFIED)

| Fact | Evidence |
|---|---|
| Python service, stdlib-only, 62 modules in `orchestration/`, ~77.5k LOC | `git ls-files`, `wc -l` |
| One process, three background threads | `web_server.py:631-637` |
| One DB connection + `RLock` | `cloud_store.py:276-281` |
| 55 API endpoints | `web_server.py` route comments |
| 50 tables, 37 migrations, 89 FKs, 2 triggers | `migrations/*.sql` |
| 1,489 test functions | `tests/*.py` |
| Production live and healthy | `/healthz`, `/readyz` (200) |
| The 0037 defect is fixed in `main` | commit `383a234` |
| Measured read latency is single-digit ms | `docs/PERFORMANCE.md` |

---

## 6. What this decision does NOT say

- It does **not** claim Rust is faster. Rust is not expected to speed up
  LLM/OpenHands-bound paths ([RUST_MIGRATION_BLUEPRINT](./RUST_MIGRATION_BLUEPRINT.md) §7).
- It does **not** authorize deleting Python, changing `main`, production,
  Render, or the database.
- It does **not** authorize microservices.
- It does **not** start implementation.

---

## 7. Non-negotiable constraints (carried from the task)

- Python and Rust must never write the same entity concurrently without a
  proven consistency protocol ([API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) §5).
- Schema changes during migration are additive (expand/contract).
- Rollback must be real: a route switch alone is insufficient if new data is
  not readable by the old implementation.

---

## 8. Decision acceptance

This decision is accepted when the ten documents listed at the top exist, do
not contradict each other, and every claim is evidence-classified. See
[MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md) for the Definition of Done and the
final report.
