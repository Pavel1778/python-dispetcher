# NEXUS — Hybrid Architecture Risk Register

> **STATUS: RISK REGISTER.** Risks specific to the hybrid (Python control plane
> + Rust performance layer) architecture. Complements the migration risk
> register ([RUST_MIGRATION_RISK_REGISTER](./RUST_MIGRATION_RISK_REGISTER.md)),
> which covers the full-migration path.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `9e01f2f9e088f52bc0d4aeb5b2ac2ae8727885df`
- **Parent:** [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md)
- **Roadmap:** [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md)

Severity = f(probability, impact).

---

| ID | Risk | Prob | Impact | Severity | Detection | Mitigation | Acceptance criteria | Owner |
|---|---|---|---|---|---|---|---|---|
| R1 | Domain boundaries unclear; a component is ported without evidence | Medium | Medium | Medium | the decision framework (selection §1) is mandatory | every port cites a measurement + threshold | each port has a benchmark artefact | Architecture |
| R2 | Rust archive parser introduces a security bug (path traversal / symlink / zip-bomb) | Low | High | **High** | fuzz + property tests; parity with Python limits | reuse the Python limits; fuzz the parser; refuse on any doubt | fuzz clean; parity on every refusal case | File Lab |
| R3 | Wheel/ABI breakage (a platform without a built wheel) | Medium | Medium | Medium | import fails in CI on each target | build manylinux wheels; keep the Python fallback | CI builds the wheel on Linux x86_64 | Foundation |
| R4 | A second writer appears (sidecar/process) and breaks one-writer | Low | High | **High** | a process that writes a domain owned by Python | native extension only (function library, not a writer) | no second writer exists in the hybrid design | Architecture |
| R5 | Integration + maintenance cost exceeds the benefit | Medium | Medium | Medium | cost vs measured gain review | port only above threshold; one crate at a time | each port justified in writing | Architecture |
| R6 | Parity gap (different output/error/Unicode) between Python and Rust | Medium | High | High | parity test suite | contract tests (§interfaces 9); golden manifests | parity suite green before enablement | Foundation |
| R7 | GIL boundary mishandled (CPU work blocks the server) | Medium | Medium | Medium | latency under load with the extension on | `py.allow_threads` for CPU work; concurrency test | load test shows no block | Foundation |
| R8 | Long-term maintenance of two languages | Medium | Medium | Medium | review cadence | one small crate, clear ownership, tests | the crate is small and covered | Architecture |
| R9 | Over-scoping (porting more than measured) | Medium | High | High | the roadmap gate (Phase 1 → 2) | the framework forbids porting without evidence | no component ported without a benchmark | Architecture |
| R10 | Fallback not exercised (the Python path rots) | Medium | Medium | Medium | the fallback flag is tested | keep the Python path in the test suite | the Python path is covered by tests | Foundation |
| R11 | Benchmark conditions drift (invalid comparison) | Medium | High | High | fixed methodology | same data/env/warm-up/runs; commit artefacts | the benchmark reproduces within noise | Performance |
| R12 | A Rust port changes observable behaviour for clients | Low | High | High | contract tests; canary | the extension is internal; no API change | the API contract suite is unchanged | API |
| R13 | Android/desktop assumptions (a Python extension cannot ship there) | Medium | Low | Low | platform matrix (§interfaces 7) | a Tauri app links Rust directly, not through Python | the platform matrix is respected | Clients |

---

## Critical / High summary

- **R2** (parser security) and **R4** (a second writer) are the two structural
  high-severity risks. Both are neutralised by the chosen design: the native
  extension is a **function library** (no second writer), and the archive parser
  ships only with fuzz + parity tests.
- **R6, R9, R11, R12** are high-severity and each has a concrete gate (parity
  suite, roadmap gate, fixed methodology, contract suite).

## Risk ownership map

| Domain | Risks owned |
|---|---|
| Architecture | R1, R4, R5, R8, R9 |
| Foundation | R3, R6, R7, R10 |
| File Lab | R2 |
| Performance | R11 |
| API | R12 |
| Clients | R13 |
