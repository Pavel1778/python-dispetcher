# NEXUS — Hybrid Migration Roadmap

> **STATUS: ROADMAP SPECIFICATION.** The phased plan to reach the hybrid
> architecture. No phase is executed by this document; each is approved
> separately on evidence.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `9e01f2f9e088f52bc0d4aeb5b2ac2ae8727885df`
- **Parent:** [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md)
- **Selection:** [RUST_COMPONENT_SELECTION](./RUST_COMPONENT_SELECTION.md)
- **Interfaces:** [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md)
- **Risks:** [HYBRID_ARCHITECTURE_RISK_REGISTER](./HYBRID_ARCHITECTURE_RISK_REGISTER.md)
- **Performance plan:** [PERFORMANCE_OPTIMIZATION_ROADMAP](./PERFORMANCE_OPTIMIZATION_ROADMAP.md)

---

## Phase 0 — Architecture baseline

**Goal:** freeze the hybrid target and the contracts before any code.

| Item | Detail |
|---|---|
| Tasks | approve domain boundaries; define the Rust↔Python contract; keep the Python control plane as the single writer |
| Dependencies | this document set |
| Acceptance | all five hybrid documents exist, agree, and match the code |
| Tests | documentation consistency checks (links, no contradictions) |
| Benchmark | none |
| Risks | R1 (unclear boundaries), R9 (over-scoping) |
| Rollback | n/a (docs only) |
| DoD | documents approved; no runtime change |

---

## Phase 1 — Python optimization (no Rust)

**Goal:** remove the one measured structural bottleneck **in Python** before any
Rust work.

| Item | Detail |
|---|---|
| Tasks | **P1** replace the single connection + `RLock` with `psycopg_pool`; re-run the DB benchmark; **P2** bound API concurrency; investigate scheduler claim throughput; frontend asset/polling optimization |
| Dependencies | Phase 0 |
| Acceptance | DB p99 @100c ≤ 50 ms (from 706 ms); API p99 @50c within 2× of single-request; no functional regression |
| Tests | full suite green; the DB benchmark A vs B re-run; a functional smoke on the pool path |
| Benchmark | `benchmarks/db_concurrency_bench.py`, `benchmarks/api_bench.py` (existing) |
| Risks | PF1/PF2 (pool breaks single-writer / oversubscription) |
| Rollback | revert the commit; no schema change |
| DoD | thresholds met **or** documented why not; the DB layer stays Python if P1 meets them |

**Gate:** if P1 meets the threshold, **no Rust port of the DB layer** — it is
closed.

---

## Phase 2 — First Rust pilot (Archive Engine)

**Goal:** prove the hybrid mechanism end-to-end on the highest-ranked candidate.

| Item | Detail |
|---|---|
| Tasks | implement the Rust archive validate/extract as a PyO3 extension (maturin); wire it into `file_lab` behind a flag; keep the Python path; parity + fuzz tests; benchmark |
| Dependencies | Phase 1 (a stable, fast store); [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md) |
| Acceptance | extract p95 ≥ 20 % better than **optimized** Python on the same archives; parity on all limits and refusals; fuzz clean; fallback works |
| Tests | parity (same manifest), property tests (path traversal/symlink), fuzz (zip/tar), concurrency (GIL released), regression benchmark |
| Benchmark | new `benchmarks/archive_bench.py` (same archive set as the audit: 100/1000/2000 members) |
| Risks | R2 (parser security), R3 (wheel/ABI), R6 (parity), R7 (GIL) |
| Rollback | flag off → Python path; uninstall the wheel |
| DoD | threshold met **and** parity/fuzz green **and** fallback exercised, else the pilot is abandoned |

---

## Phase 3 — Additional performance modules

**Goal:** port further components **only** on their own evidence.

| Item | Detail |
|---|---|
| Tasks | File Processing / Repository indexing / Event Processing — each requires its own benchmark and approval |
| Dependencies | Phase 2 (the mechanism proven) |
| Acceptance | per-component threshold met; parity + tests green |
| Tests | per component (parity, fuzz where a parser, concurrency) |
| Benchmark | a dedicated benchmark per component, same-scenario as its Python baseline |
| Risks | R1, R5 (cost), R8 (maintenance) |
| Rollback | per-component flag |
| DoD | only the components that pass are ported; the rest stay Python |

**Note (VERIFIED from the audit):** Event Processing has **no** measured
bottleneck; File Processing hashing is already C. These are likely to be
**kept in Python** unless a new measurement changes it.

---

## Phase 4 — Execution Engine (separate architecture review)

**Goal:** consider the execution engine only after the orchestration semantics
are stable.

| Item | Detail |
|---|---|
| Tasks | a full audit of leases, idempotency, cancellation, recovery, DAG gating; then decide (Rust for correctness/types, or keep Python) |
| Dependencies | Phase 1–3; the task model stable |
| Acceptance | a written review; a correctness case (not a speed case) for any port |
| Tests | the 15 mandatory scenarios ([EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md) §6) |
| Benchmark | not a speed benchmark; a reliability/failure-injection suite |
| Risks | R4 (dual writers if a second process is introduced), R6 |
| Rollback | keep the Python engine; a Rust engine is a separate process only with a proven need |
| DoD | an explicit decision, evidence-based |

**VERIFIED constraint:** the Execution Engine is **not** ported before the
current task model (leases, idempotency, cancellation, recovery) is stable.

---

## Order-change rule

The phase order changes **only** with measured arguments. A component may be
promoted or dropped by a benchmark, never by preference.

---

## Compatibility & rollback (all phases)

- **Schema:** unchanged; the Python store keeps applying `migrations/*.sql`
  ([API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) §4).
- **API/behaviour:** unchanged for clients.
- **One writer per domain:** preserved — a Rust extension is a function
  library, not a writer.
- **Rollback:** a feature flag forces the Python path; the Python
  implementation is never deleted until parity + a stability window pass.
- **Stability window:** after a component passes parity and its benchmark, it
  runs in production for an agreed window before the Python fallback is marked
  retired.

---

## Definition of Done (this architecture stage)

- Domain analysis done; characters classified; measurements cited.
- Hybrid architecture fixed; three integration options compared with a reason.
- Candidate ranking + decision framework written.
- Interfaces (PyO3, contracts, one-writer, parity) specified.
- Roadmap with phases, dependencies, acceptance, tests, risks, rollback, DoD.
- Risk register written.
- Existing documents updated only where required; history preserved.
- All documents consistent, links valid, no unproven performance claims, no
  secrets.
