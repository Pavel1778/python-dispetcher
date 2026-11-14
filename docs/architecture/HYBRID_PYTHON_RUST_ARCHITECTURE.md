# NEXUS — Hybrid Python / Rust Architecture

> **STATUS: ARCHITECTURAL SPECIFICATION — NOT AN IMPLEMENTATION AUTHORIZATION.**
> This document records the hybrid (Python control plane + Rust performance
> layer) architecture. No runtime code is changed by it, no Rust module is
> created, no migration is added, no API contract is changed.
>
> Evidence classes: **VERIFIED** (checked against code / measured),
> **INFERRED**, **UNKNOWN**, **BLOCKED**.

- **Base commit:** `9e01f2f9e088f52bc0d4aeb5b2ac2ae8727885df` (`origin/main`)
- **Performance audit commit:** `2d677ff1de77e13c1ea79e156cab94d63de58b3b`
- **Branch:** `architecture/hybrid-python-rust`
- **Sibling documents:** [RUST_COMPONENT_SELECTION](./RUST_COMPONENT_SELECTION.md),
  [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md),
  [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md),
  [HYBRID_ARCHITECTURE_RISK_REGISTER](./HYBRID_ARCHITECTURE_RISK_REGISTER.md).

---

## 1. Decision in one paragraph

NEXUS stays a **Python control plane** for everything that is API, business
logic, projects, users, orchestration, OpenHands, LLM, GitHub and integration
work. **Rust is a computational layer** for the few components where a measured
benchmark shows a material gain in speed, memory, concurrency or reliability —
and only after the *optimized Python* variant has been measured first. The
shared foundation is the existing PostgreSQL schema and the existing
infrastructure. Nothing is rewritten for its own sake.

This mirrors the earlier decision ([FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md))
and adds the hybrid target: **Python control plane + optional Rust native
libraries in-process**, not microservices and not a full rewrite.

---

## 2. Current architecture (VERIFIED)

```
browser (studio)            machine client (CLI / MCP)
      │                              │
      ▼                              ▼
┌───────────────────────────────────────────────────────────┐
│ orchestration/web_server.py — one ThreadingHTTPServer       │
│  auth (session+bearer+CSRF) · routes · lab loop thread      │
└───────┬───────────┬───────────┬───────────┬────────────────┘
        ▼           ▼           ▼           ▼
  nexus_intent  nexus_conversations  nexus_lab   providers
  nexus_commands  nexus_generation   (scheduler,  (Gemini→Groq→
        │                            executor,     OpenRouter→
        ▼                            verifier)     OpenHands)
  nexus_specs / nexus_spec_planner   nexus_lab_store ── cloud_store (PostgreSQL)
        │                              ▼
  providers.py                 openhands_router / pool
                               CloudClient / CloudDirectClient
```

- **One process, stateful**; three background threads (`web_server.py:631-637`).
- **One PostgreSQL connection behind a `threading.RLock`**
  (`cloud_store.py:276-281`, `_tx` at `:536`). **Measured lock convoy** (§4).
- Storage: PostgreSQL via `SUPABASE_DB_URL`.
- Files: 62 modules in `orchestration/`, ~77k LOC, 1,489 test functions.

---

## 3. Target hybrid architecture

```
                       Python control plane (unchanged owner of every domain)
   ┌──────────────────────────────────────────────────────────────────────┐
   │  web_server.py  ·  nexus_* services  ·  cloud_store / nexus_lab_store  │
   │  OpenHands  ·  LLM  ·  GitHub  ·  MCP  ·  conversations  ·  projects   │
   └───────────┬───────────────────────────────────────────────┬──────────┘
               │  in-process calls (PyO3, no serialization)     │  SQL
               ▼                                                ▼
   ┌─────────────────────────────┐                  ┌──────────────────────┐
   │  Rust native extension(s)    │                  │  PostgreSQL (shared) │
   │  e.g. nexus_archive,         │                  │  same schema, same   │
   │       nexus_filehash, …      │                  │  migrations          │
   │  (only measured winners)     │                  └──────────────────────┘
   └─────────────────────────────┘
```

**VERIFIED principle:** the Python process remains the **only writer** of every
domain; a Rust extension is a *function library* the Python process calls, so
the "one writer per domain" rule
([API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) §5) is preserved
by construction — there is no second writer to coordinate with.

### 3.1 Mermaid: control flow

```mermaid
flowchart TB
  subgraph Client
    W[Web studio]:::c
    C[CLI / MCP]:::c
  end
  subgraph Py["Python control plane (single writer)"]
    API[web_server.py routes]
    SVC[nexus_* services]
    STORE[cloud_store / nexus_lab_store]
    EXT[Rust native ext (PyO3)]
    API --> SVC --> STORE
    SVC -->|CPU-bound call| EXT
  end
  subgraph Data
    PG[(PostgreSQL)]
  end
  subgraph External["External services (unchanged)"]
    OH[OpenHands Cloud]
    LLM[LLM providers]
    GH[GitHub]
  end
  W --> API
  C --> API
  STORE --> PG
  SVC --> OH
  SVC --> LLM
  SVC --> GH
  classDef c fill:#eef,stroke:#88a;
```

---

## 4. What the measurements say (VERIFIED, from the audit)

| Domain | Measured (this project) | Character | Source |
|---|---|---|---|
| DB access under concurrency | p99 **706 ms** @100c (current) vs **37 ms** pool (Python) vs **35 ms** Rust | I/O-bound + lock | `PERFORMANCE_DRIVEN_RUST_MATRIX.md` §1 |
| API read paths (single) | p50 0.6–1.7 ms | I/O + cheap CPU | same §2 |
| API under concurrency | p99 **1264 ms** @50c | lock + thread-per-request | same |
| Scheduler claim | flat **~250–300 claims/s** at 1→25 workers | lock-bound | same |
| Event write | 0.84 ms | I/O | same |
| File upload 2 MiB | 23.7 ms | I/O + sha256 | same |
| Archive inspect 2000 members | 39.3 ms | **CPU** | same |
| Archive extract 1000 members | **216 ms** | **CPU** | same |
| chat_turn / start_run | 18 / 48 ms (stand-in) | **external-bound** | same |
| Frontend bundle | 198 KB (gzip 50 KB) | static asset | same |

**The only measured structural bottleneck is the DB connection model, and
optimized Python removes most of it.** Rust wins *only* on CPU-bound work
(archive), and there the absolute numbers are small.

---

## 5. Three integration options compared

| Option | Description | Pros | Cons | Verdict |
|---|---|---|---|---|
| **A. Python + Rust native libraries (PyO3/maturin)** | Rust compiled to a Python extension; called in-process | no IPC, no serialization, no new deploy unit, one process, one writer, trivial fallback (import fails → Python path) | needs a wheel per platform/ABI; GIL boundary must be released for CPU work | **chosen** |
| **B. Python + Rust sidecar processes** | Rust runs as a separate process; IPC/gRPC | language isolation, independent scaling | IPC + serialization + a second deploy unit + a second writer to coordinate + ops cost | only if a component needs its own process (e.g. long CPU job) |
| **C. Full Rust rewrite** | everything in Rust | one language, max control | 77k LOC + 1,489 tests to port; loses the working system; the measured bottleneck is solvable in Python | **rejected** for now |

**Why A wins (INFERRED):** the measured wins are small, local, CPU-bound
functions (archive inspect/extract, hashing, scanning). A native library calls
them with no serialization and no second writer; a sidecar would add IPC cost
larger than the function itself, and a full rewrite is unjustified by the
measured profile. A stays reversible: a failed import falls back to the Python
implementation.

**When B is justified (INFERRED):** if a Rust job is long-running and
CPU-heavy enough to block the request path even with the GIL released (a
future large-repo indexer), it moves to a sidecar/worker — the pattern the
project already uses for `lab_worker.py`.

---

## 6. Domain ownership map

| Domain | Owner (writer) | Layer | Rust candidate? |
|---|---|---|---|
| HTTP/API | Python | control | no |
| PostgreSQL access | Python | control | no (fix with a pool) |
| Scheduler / task claim | Python | control | no (lock-bound, fixed by the pool) |
| Dispatcher / execution engine | Python | control | later (after semantics stabilize) |
| OpenHands Cloud | Python | external-bound | no |
| LLM orchestration | Python | external-bound | no |
| Events | Python | control | no |
| **Archive inspection/extraction** | Python (+Rust ext) | **CPU** | **yes (pilot)** |
| File processing / hashing | Python (+Rust ext) | CPU/IO | maybe |
| Repository/code indexing | Python | CPU/IO + external | maybe (later) |
| GitHub | Python | external-bound | no |
| MCP | Python | control | no |
| Memory | Python | control | no |
| Notifications | Python | control | no |
| Frontend assets/polling | Python-served static | asset | no (minify/split) |
| Startup/migrations/recovery | Python | control | no |

Detail and ranking: [RUST_COMPONENT_SELECTION](./RUST_COMPONENT_SELECTION.md).

---

## 7. Shared contracts (Python ↔ Rust)

- **Error format:** one structured error shape; a Rust extension raises a
  Python exception mapped to the same error taxonomy the API already uses
  (`NotFound → 404`, `Validation → 400`, …). See
  [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md) §3.
- **Logging/tracing:** the Rust extension emits `tracing` events bridged to the
  Python logging/metrics; correlation IDs are passed in, never invented.
- **Config:** the extension reads the same environment variable names
  (`docs/ENVIRONMENT.md`), never its own config file.
- **Data formats:** inputs/outputs are `serde`-defined structs mirrored by
  Python dataclasses; a contract test pins both.

---

## 8. Compatibility guarantees (VERIFIED requirements)

- **Schema:** the Rust extension never touches the schema; the Python store
  keeps applying the same `migrations/*.sql`.
- **API/behaviour:** existing clients see no change; a Rust extension is an
  internal function.
- **Clients:** Web/CLI/MCP unchanged; Desktop/Android (Tauri) remain a separate
  question ([CLIENTS_ARCHITECTURE](./CLIENTS_ARCHITECTURE.md)).
- **Fallback:** the Python implementation of every Rust-candidate function
  stays in the tree until parity + a stability window pass
  ([HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md) §5).

---

## 9. What this document does NOT decide

- It does not authorize any Rust implementation.
- It does not claim Rust is faster anywhere not measured.
- It does not choose sidecar or rewrite — both are rejected as the default.
- It does not change the schema, the API, Render, or `main`.
