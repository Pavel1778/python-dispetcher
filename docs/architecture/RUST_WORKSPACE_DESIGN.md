# NEXUS — Rust Workspace Design

> **STATUS: SPECIFICATION.** Preliminary Rust workspace/crate design. No crates
> are created at this stage.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md),
[EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md),
[API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md).

---

## 1. Workspace layout (preliminary — not a requirement to create all now)

```
nexus/
├─ Cargo.toml                 # [workspace] members
├─ crates/
│  ├─ nexus-core              # Foundation: ids, error, config, time, telemetry
│  ├─ nexus-config            # configuration loading + secret handles
│  ├─ nexus-db                # SQLx pool, migrations, repositories (shared)
│  ├─ nexus-auth              # Identity & Access
│  ├─ nexus-projects          # Projects & Workspace
│  ├─ nexus-conversations     # Conversations & Messages
│  ├─ nexus-tasks             # Task Management
│  ├─ nexus-execution         # Execution Engine (run/scheduler/executor/verifier)
│  ├─ nexus-planner           # Planner & DAG
│  ├─ nexus-scheduler         # Scheduler & Dispatcher (may stay inside execution)
│  ├─ nexus-openhands         # OpenHands Runtime
│  ├─ nexus-ai                # AI Gateway (LLM cascade)
│  ├─ nexus-memory            # Context & Memory
│  ├─ nexus-github            # GitHub Integration
│  ├─ nexus-files             # File Laboratory
│  ├─ nexus-mcp               # MCP server (client later)
│  ├─ nexus-verification      # Verification & Evidence
│  ├─ nexus-events            # Events bus
│  ├─ nexus-notifications     # Notifications
│  ├─ nexus-audit             # Audit & Lifecycle
│  ├─ nexus-api               # Axum HTTP surface + domain router
│  ├─ nexus-cli               # CLI binary
│  └─ nexus-observability     # tracing/metrics/health wiring
└─ bins/
   ├─ nexus-server            # the modular-monolith binary
   └─ nexus-worker            # optional separate scheduler process
```

**Not created now.** The list is a target, not a mandate; §6 says what may be
merged into one crate.

---

## 2. Per-crate boundaries and allowed dependencies

| Crate | Owns | May depend on | Must NOT depend on |
|---|---|---|---|
| `nexus-core` | ids, error, time, telemetry | — (nothing) | every domain |
| `nexus-config` | config, secret handles | `nexus-core` | domains |
| `nexus-db` | pool, migrations, shared row types | `nexus-core`, `nexus-config` | domains (only row types) |
| `nexus-auth` | sessions, RBAC | `nexus-core`, `nexus-db`, `nexus-projects` | execution |
| `nexus-projects` | projects, members, binding | `nexus-core`, `nexus-db`, `nexus-auth` | execution, planner |
| `nexus-conversations` | chats, messages, generations | core, db, auth, projects, ai | execution |
| `nexus-tasks` | task entity + state machine | core, db, execution | — |
| `nexus-execution` | runs, scheduler, executor, verifier | core, db, tasks, openhands, github, ai, events, verification | api |
| `nexus-planner` | spec, plan, DAG | core, db, ai, execution | api |
| `nexus-scheduler` | the loop | core, db, execution | api |
| `nexus-openhands` | transport, pool, router | core, db, auth, ai | execution |
| `nexus-ai` | LLM cascade | core | execution, openhands |
| `nexus-memory` | context, memory | core, db, projects, github | execution |
| `nexus-github` | accounts, discovery, guarded writes | core, db, auth, projects | execution |
| `nexus-files` | files, workspaces, archives, voice | core, db, projects | execution |
| `nexus-mcp` | MCP server/tools | all it exposes (leaf) | — |
| `nexus-verification` | GitVerifier, evidence | core, db, github | execution, scheduler |
| `nexus-events` | event bus | core, db | execution |
| `nexus-notifications` | notifications | core, db, events | execution |
| `nexus-audit` | audit, retention, lifecycle | core, db, events | execution |
| `nexus-api` | Axum routes, domain router | all domains | — (top) |
| `nexus-cli` | CLI | `nexus-api` client types, core | server internals |
| `nexus-observability` | tracing/metrics/health | core | domains |

---

## 3. Shared types, error handling, configuration

### 3.1 Shared types (`nexus-core`)

- Newtype IDs: `ProjectId`, `ConversationId`, `RunId`, `TaskId`, `AccountId`,
  `FileId`, `SpecId`, `GoalId` (UUID-backed).
- `Principal` (the authenticated operator), `Role`.
- `Paged<T>`, `Money`-free, `Timestamp` (UTC).

### 3.2 Error handling

- One `NexusError` enum in `nexus-core`; domains return `Result<T, NexusError>`.
- A single HTTP mapping in `nexus-api`: `NotFound → 404` (this is how project
  isolation becomes 404, never 403), `Forbidden → 403` (only where the Python
  code uses it), `Conflict → 409`, `Validation → 400`, `Unavailable → 503`.
- No `unwrap()` in domain paths; a `thiserror`-based enum; `anyhow` only at the
  binary edge.

### 3.3 Configuration

- `nexus-config` loads the same variable names as `docs/ENVIRONMENT.md`
  (VERIFIED list: `SUPABASE_DB_URL`, `AUTO_MIGRATE`, `PSYCOPG_PREPARE`,
  `OPENHANDS_*`, `AUTH_*`, `WEB_AUTH_TOKEN`, `GITHUB_TOKEN`,
  `NEXUS_*`, `GEMINI_*`, `GROQ_*`, `OPENROUTER_*`).
- Secrets are read through handles that never `Debug`-print their value
  (mirrors `providers.Provider.__repr__` and `secrets.py`).

### 3.4 Database access

- `nexus-db` holds the `sqlx` pool and the migrations runner (the same
  `migrations/*.sql`, same checksum discipline).
- Repositories live in their domain crate; `nexus-db` only owns the pool, the
  migration runner and shared row types.

### 3.5 Async boundaries

- Axum handlers are async; blocking work (PBKDF2 at 600k iterations) runs on
  `tokio::task::spawn_blocking` (INFERRED: PBKDF2 is CPU-heavy and must not
  block the runtime).
- The scheduler loop is a long-lived async task; the OpenHands poll is an
  async interval, not a thread sleep.

---

## 4. Public API contracts

- HTTP: identical paths and shapes to the current API where a domain is cut
  over (see [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md)).
- Inter-crate: typed Rust APIs, no stringly-typed cross-domain calls.
- External (OpenHands/GitHub/LLM): clients with timeouts, 429 handling and a
  failure taxonomy identical to the Python clients.

---

## 5. Testing strategy per crate

| Level | Scope | Tooling |
|---|---|---|
| Unit | pure logic (state machine, DAG validation, redaction) | `cargo test` |
| Integration | repository + real PostgreSQL | `sqlx` test DB, `#[sqlx::test]` |
| Contract | HTTP shape vs the Python API | golden JSON fixtures |
| Concurrency | leases, claims, duplicate requests | `tokio::test` + real DB |
| Failure injection | OpenHands/GitHub/LLM outages | trait-based fakes (no mocks of the code under test) |
| End-to-end | the 15 mandatory scenarios | the existing acceptance kit, ported |

**No mocks of the code under test** — the Python suite's discipline
(`tests/fake_*` are stand-in *servers*, not mocks of NEXUS) is carried over.

---

## 6. What stays a library vs. what may become a process

- **Stay libraries inside `nexus-server`:** core, config, db, auth, projects,
  conversations, tasks, planner, openhands, ai, memory, github, files, mcp,
  verification, events, notifications, audit, observability, api.
- **May become a separate process (only with evidence):** `nexus-scheduler`
  (the run loop) and `nexus-cli` (already a binary). The Python deployment
  already supports this split (`lab_worker.py` vs `web_server.py`), so the
  Rust split mirrors an existing, working topology.

---

## 7. Migration order (initial, refined in the roadmap)

`nexus-core` → `nexus-config`/`nexus-db` → `nexus-api` (read-only) →
`nexus-auth` → `nexus-projects` → `nexus-conversations` → `nexus-tasks` →
`nexus-execution` (+scheduler) → `nexus-openhands`/`nexus-ai`/`nexus-github` →
`nexus-verification` → clients. See [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md).
