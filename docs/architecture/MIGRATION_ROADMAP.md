# NEXUS — Migration Roadmap

> **STATUS: SPECIFICATION.** The phased strangler roadmap, broken into small
> independently verifiable steps. No calendar promises.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md),
[PYTHON_RUST_MIGRATION_MATRIX](./PYTHON_RUST_MIGRATION_MATRIX.md),
[RUST_MIGRATION_RISK_REGISTER](./RUST_MIGRATION_RISK_REGISTER.md).

---

## 1. The ten-step strangler sequence

1. Python remains the production implementation.
2. Rust foundation in an isolated environment.
3. API compatibility.
4. Read-only / shadow verification.
5. Domain-by-domain canary.
6. Explicit writer-ownership transfer.
7. Rust primary.
8. Python fallback.
9. Stable observation window.
10. Python retirement (per domain).

---

## 2. Phases

| Phase | Goal | Components | Depends on | Risks | Tests | Acceptance | Rollback |
|---|---|---|---|---|---|---|---|
| **P0** | Production unblocked | migration 0037 | — | — | `test_schema_bootstrap` | done: `383a234` merged, prod live | previous deploy |
| **P1** | Rust foundation | `nexus-core`, `nexus-config`, `nexus-db`, `nexus-observability` | — | — | unit + migration parity | types/config/migrations match | delete the crates |
| **P2** | Read-only API | `nexus-api` (GET routes) + `sqlx` | P1 | R5 | contract (golden JSON) | read parity on canary | proxy → Python |
| **P3** | Identity & Access | `nexus-auth` | P2 | R7 | security + 404 isolation | login/session parity | proxy → Python |
| **P4** | Projects & Conversations | `nexus-projects`, `nexus-conversations` | P3 | R6 | lifecycle + e2e | CRUD/pin/trash parity | proxy → Python |
| **P5** | Execution Engine + Scheduler | `nexus-tasks`, `nexus-execution`, `nexus-scheduler` | P4 | R1, R4, R6 | the 15 scenarios | all scenarios pass | proxy → Python |
| **P6** | Integrations | `nexus-openhands`, `nexus-ai`, `nexus-github`, `nexus-memory`, `nexus-verification` | P5 | R5, R6 | integration + failure injection | live smoke + failover | proxy → Python |
| **P7** | Files, MCP, Audit | `nexus-files`, `nexus-mcp`, `nexus-audit`, `nexus-notifications`, `nexus-events` | P6 | R8, R11 | safety + protocol | parity | proxy → Python |
| **P8** | Clients | Web rewrite, `nexus-cli`, Desktop/Android spikes | P7 | R9, R10, R12 | build + e2e | packaging artefacts build | keep web |
| **P9** | Retire Python (per domain) | — | P8 | R6, R14 | full suite | zero divergence for the window | keep Python |

---

## 3. Per-phase detail (goal · tasks · components · dependencies · risks · tests · acceptance · result · rollback)

Each phase follows the table above; the pattern is fixed:

- **Goal** — one sentence, one outcome.
- **Tasks** — small, ordered, each independently reviewable.
- **Components** — the crates/tables touched.
- **Dependencies** — the previous phase(s).
- **Risks** — the register IDs.
- **Tests** — the suite that must pass.
- **Acceptance** — the observable criterion.
- **Result** — the artefact (a green suite, a canary at 5%).
- **Rollback** — the proxy switch or the deploy rollback.

---

## 4. The 15 mandatory scenarios (acceptance for P5)

1. Two independent tasks in one conversation.
2. Refusal of conflicting concurrent launches.
3. Different projects in parallel.
4. Cancel a task.
5. Server restart during a task.
6. Recovery of unfinished tasks.
7. Loss of connection to OpenHands.
8. LLM error.
9. Loss of PostgreSQL connection.
10. Repeated request after a timeout.
11. One OpenHands account unavailable.
12. Re-run of a finished task.
13. Project isolation.
14. Project deletion and its data.
15. State sync between clients.

Pass criterion per scenario: see [EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md) §6.

---

## 5. Definition of Done (for this architecture stage)

This stage is complete when:

- The current system is fully inventoried.
- Every Python module is classified.
- Every DB object is mapped to a domain.
- Every API endpoint is mapped to a future Rust interface.
- Every user function is mapped to a UI surface.
- The full CRUD matrix exists.
- Domain boundaries are defined.
- The Rust workspace design exists.
- The Execution Engine is specified.
- Python/Rust ownership and rollback are defined.
- The client architecture is described.
- The Self-QA safety model is defined.
- The performance methodology is defined.
- The roadmap is split into independent steps.
- The risks are documented.
- No document contradicts another.

A requirement is **not** counted as done on the strength of a heading or a
keyword alone.

---

## 6. Performance methodology (no language-based speed claims)

- Measure with `tools/benchmark.py` under the **same** scenario, dataset and
  infrastructure for Python and Rust.
- Report p50/p90/p99 per path; commit the raw JSON artefacts.
- LLM/OpenHands-bound paths are excluded from any speed claim by construction.
- Infrastructure differences (Render Free vs paid, cold start) are reported
  separately from code differences.
- Add tracing, metrics, structured logs, correlation IDs, task/run tracing,
  external-provider latency and recovery metrics as part of P2/P5.

---

## 7. Update 2026-11-15 — measured performance findings (added, history preserved)

A measured audit (`research/performance-audit`) confirmed the read paths are
single-digit milliseconds, but **revised one earlier conclusion**: under
concurrency the single DB connection + `RLock` is a real hotspot. Measured on
PostgreSQL 17.11: p99 **706 ms @100 concurrent** (current) vs **37 ms** with a
Python connection pool vs **35 ms** with Rust/SQLx. The scheduler claim
throughput is **flat ~250–300 claims/s** from 1→25 workers.

**Consequence for this roadmap:** a **PYTHON_OPTIMIZE phase (P0.5)** is added
**before** P1 — replace the single connection with a pool. The only measured
structural bottleneck is solvable in Python, so **no component is RUST_FIRST
today**. See [PERFORMANCE_DRIVEN_RUST_MATRIX](./PERFORMANCE_DRIVEN_RUST_MATRIX.md)
and [PERFORMANCE_OPTIMIZATION_ROADMAP](./PERFORMANCE_OPTIMIZATION_ROADMAP.md).

*Confirmed by the audit:* read paths single-digit ms; OpenHands/LLM paths
provider-bound. *Revised:* "no NEXUS-side hotspot" holds only at low
concurrency.

---

## 8. Update 2026-11-15 — hybrid refinement (added, history preserved)

The strategy is refined to a **hybrid** shape: the Python control plane stays
the single writer of every domain; Rust is adopted only as an in-process native
library (PyO3/maturin) for measured CPU-bound components. This changes the
**mechanism** (native library, not a full rewrite) but not the phase gates:

- Phase 1 (Python optimization: the DB pool) still comes **first** and, if it
  meets the threshold, closes the DB layer in Python.
- Phase 2 (the first Rust pilot) targets the **Archive Engine** (CPU-bound,
  measured), not the DB or the execution engine.
- The full-migration phases P2–P9 below remain the long-term option, gated on
  measured wins; they are not the default path.

See [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md) for the hybrid
phases; this document's phases remain the full-migration reference.
