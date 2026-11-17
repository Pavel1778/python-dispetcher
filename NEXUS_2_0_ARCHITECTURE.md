# NEXUS 2.0 Architecture

The target system: a complete, production-grade autonomous AI development
platform. This document describes the architecture as it **is** after the
integration, names the one authoritative implementation of each component, and
marks what is Rust, what is Python, and what is deliberately deferred.

It is not a wish-list. Every box below exists in the repository and has tests;
where something is partial, it says so.

## 1. Shape

One process, one database, no microservices. The requirement says *"Avoid
unnecessary microservices"* and *"Prefer clear module boundaries, explicit
interfaces, and reliable transaction semantics."* NEXUS is therefore a single
Python service (`orchestration/web_server.py`) with a Rust core library for the
pure decisions, and a separate worker process for autonomous runs that shares
the same database.

```
                         ┌──────────────────────────────────────────┐
   browser / PWA  ──────▶│  web_server  (Python, HTTP + session auth)│
   Android / CLI  ──────▶│                                          │
   MCP client     ──────▶│  routes ─▶ services ─▶ PostgresStore ────┼──▶ PostgreSQL 17
                         └───────┬──────────────────────┬───────────┘
                                 │                      │
                    ┌────────────▼──────────┐  ┌────────▼─────────────┐
                    │ lab_worker (Python)   │  │ rust/ nexus-core     │
                    │ advance runs, poll,   │  │ pure decisions       │
                    │ verify, report        │  │ (parity-tested)      │
                    └────────────┬──────────┘  └──────────────────────┘
                                 │
                    ┌────────────▼──────────────────────────────┐
                    │ external: OpenHands Cloud / agent-server,  │
                    │ GitHub, LLM providers (Gemini/Groq/…/OH)   │
                    └────────────────────────────────────────────┘
```

Two processes, one database: the web server and the lab worker. Both may advance
runs (every claim is a lease), so a restart of either is a non-event. A third
deployment shape runs only the web server with its inline loop
(`NEXUS_LAB_INLINE`).

## 2. Components and their authoritative implementation

| Component | Module(s) | Language | Owner of the invariant |
|---|---|---|---|
| HTTP API + auth | `web_server.py`, `web_auth.py` | Python | one route table; session (PBKDF2 + CSRF) and bearer |
| Project & repository management | `nexus_projects.py`, `nexus_binding.py` | Python | membership is a 404; the allowlist is not enough |
| Orchestration engine | `nexus_lab.py`, `nexus_lab_store.py`, `lab_worker.py` | Python | one claim per task per tick; fencing tokens |
| Planning & DAG | `nexus_spec_planner.py`, `nexus_planner.py`, `nexus_planning.py` | Python | the application validates the plan, never the model |
| Agent/provider adapters | `providers.py`, `openhands_pool.py`, `openhands_router.py`, `cloud_*.py` | Python | failover is for transport, not for coding |
| Execution runtime | `nexus_lab.py` (`OpenHandsExecutor`) | Python | never decides completion; the verifier does |
| Verification & Self-QA | `nexus_lab.py` (`GitVerifier`), `nexus_selfqa.py`, `evidence_trust.py` | Python | a claim is never a proof; only a signature verifies |
| Persistent state | `cloud_store.py`, `nexus_lab_store.py`, `migrations/` | Python + SQL | PostgreSQL is authoritative |
| Notifications | `nexus_lab_store.py` | Python | durable rows, recoverable after restart |
| Observability | `tracing.py`, `nexus_profiler.py`, `openhands_diagnostics.py` | Python | structured logs, correlation ids, redaction |
| **Pure decisions** | `rust/crates/nexus-domain`, `nexus-core` | **Rust** | parity with the Python tables, proven |
| CLI | `cli.py` (HTTP client) + `nexus-rs` (pure) | Python + Rust | one command surface, two implementations of the pure part |
| MCP server | `mcp_server.py` | Python | stdio and Streamable HTTP; membership re-checked |
| Studio UI | `studio/` | JS (no build) | one screen; CSP-safe; contrast asserted |

### Why the split is where it is

The Rust core is the **deterministic** part: state machines, DAG validation, the
plan hash, the retry arithmetic, the claim decision. These are pure - no I/O, no
clock, no database - which is exactly what makes them comparable across
languages. Everything that touches the network, the database or the clock stays
in Python until the parity gate can be widened to cover it (see
`RUST_MIGRATION.md`, section 8).

## 3. The interfaces

### HTTP

The API is versioned under `/api/v1/`. Authentication is two independent paths
(requirement: never let one replace the other):

- a **browser session** (`web_auth.py`): PBKDF2 password, server-side session
  rows, double-submit CSRF, sliding TTL with a hard cap;
- a **bearer** `WEB_AUTH_TOKEN` for machine clients (CLI, MCP, integrations).

Every project-scoped route calls a service method that takes the principal and
the project id, and returns `NotFound` for a non-member - never `Forbidden`,
because a `403` confirms the resource exists. `tests/test_project_isolation.py`
pins this at both boundaries.

### The event protocol

State changes append a project event (`nexus_project_events`) in the same
transaction as the change. The event kinds (`run.started`, `task.retry`,
`goal.created`, …) are the audit trail and the source of the run timeline the
studio shows. An event is never written outside the transaction it describes, so
the timeline cannot claim something the database did not commit.

### The Rust boundary

JSON, versioned by shape, compared by the parity test. There is no in-process
FFI yet (PyO3) because there is no measured benefit: the migrated decisions are
called once per task or per plan, not in a hot loop, and the Rust figures already
include a process start. See `RUST_PYTHON_COMPATIBILITY.md`.

## 4. Transaction and concurrency model

PostgreSQL is the single source of truth. The invariants that matter:

- **One claim per task per tick.** `claim_task` locks one candidate row with
  `FOR UPDATE SKIP LOCKED`, so N workers claim N distinct tasks.
- **One order for locks.** The run row is locked before task rows, everywhere
  (a cancel and a claim can wait for each other but cannot deadlock - the fix
  integrated from PR #47).
- **Fencing tokens.** Every mutation carries a `lease_token`; a worker whose
  lease was taken away writes to no row (a no-op), not to the wrong one.
- **The workspace lock is the index.** A partial unique index
  (`uq_tasks_workspace_active`) is what keeps two tasks off one branch; the
  read-then-check is only the fast path.
- **Idempotency is in the database.** A unique key on workflows, goals and runs
  absorbs a retry; a client-side check cannot.

## 5. Security model (summary)

Full detail in [`SECURITY_MODEL.md`](SECURITY_MODEL.md). The architecture-level
points:

- least privilege: the app connects as the database owner and bypasses RLS on
  purpose, while the session-key tables have RLS enabled and PUBLIC revoked;
- secrets are Fernet-encrypted (`secrets.py`, one cipher) and never returned by
  any read; `/api/system` and `/api/capabilities` report presence only;
- outbound URLs are checked by `net_guard` (public addresses only, re-checked at
  connect time), with the opt-in read from `nexus_policy`, not a second switch;
- a redirect is refused (`CloudClient`) or followed only within one origin
  (`OpenHandsClient`), so a key cannot leak to a third host.

## 6. Deployment

One Render web service and one Render worker service (or Docker Compose, or
systemd), against one PostgreSQL. Migrations are idempotent and applied by the
process on first connect. `/healthz` is a one-line liveness probe; `/readyz`
checks the database. TLS and the reverse proxy are the platform's. See
[`OPERATIONS.md`](OPERATIONS.md) and [`RELEASE_GUIDE.md`](RELEASE_GUIDE.md).

## 7. What is deferred, and why

- **The Rust HTTP service (Axum/Tokio/SQLx).** Deferred until a component is
  ready to be authoritative; adding it now would be a service with nothing to
  serve that the Python service does not already serve.
- **The intent router and task-kind derivation in Rust.** Language-sensitive
  regexes with a documented history of subtle bugs; rewriting them risks
  divergence for no behavioural gain.
- **Microservices.** Not adopted. The requirement forbids them unless necessary,
  and nothing here needs them: the load is network-bound, and one database with
  leases already gives safe parallelism.
- **A local terminal / editor execution.** Execution happens in OpenHands
  Cloud, not in the NEXUS process. The IDE reads files through the GitHub API.

## 8. How this maps to the requirement

| Requirement section | Where it lives |
|---|---|
| 4. Architecture | this document |
| 5. Project isolation | `tests/test_project_isolation.py`, the service layer |
| 6. Multi-agent orchestration | `openhands_pool.py`, `openhands_router.py`, `nexus_executors.py` |
| 7. Autonomous planning | `nexus_spec_planner.py`, `nexus_planner.py` |
| 8. Autonomous development loop | `nexus_lab.py` (`LabScheduler`), `lab_worker.py` |
| 9-13. Rust migration | `rust/`, `docs/RUST_MIGRATION.md`, `docs/RUST_PYTHON_COMPATIBILITY.md` |
| 14-16. Workspace / IDE / GitHub | `studio/`, `nexus_binding.py`, `github_client.py` |
| 17. Self-QA | `nexus_selfqa.py`, `evidence_trust.py` |
| 18-21. Security / DB / observability / recovery | `SECURITY_MODEL.md`, `OPERATIONS.md` |
| 22-24. UX / clients / notifications | `studio/`, `packaging/`, notifications in the store |
| 25-27. Testing / acceptance / deployment | `docs/INTEGRATION_TESTING.md`, `RELEASE_GUIDE.md` |
