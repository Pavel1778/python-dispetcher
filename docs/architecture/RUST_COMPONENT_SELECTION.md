# NEXUS — Rust Component Selection

> **STATUS: DECISION FRAMEWORK + CANDIDATE RANKING.** This document ranks the
> possible Rust components. A ranking is **not** an approval to implement; each
> candidate is approved separately, on evidence, after the optimized-Python
> variant is measured.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `9e01f2f9e088f52bc0d4aeb5b2ac2ae8727885df`
- **Parent:** [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md)
- **Interfaces:** [RUST_PYTHON_INTERFACES](./RUST_PYTHON_INTERFACES.md)
- **Roadmap:** [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md)
- **Measurements:** [PERFORMANCE_DRIVEN_RUST_MATRIX](./PERFORMANCE_DRIVEN_RUST_MATRIX.md)

---

## 1. Decision framework (the standard)

A Rust port is justified only when **all** hold:

1. A **measured** bottleneck exists (not a suspicion).
2. The **optimized Python** variant does not close it sufficiently.
3. A Rust prototype shows a **material** gain on the same scenario.
4. The gain appears in a **real NEXUS user scenario**, not only a micro-benchmark.
5. **Functional parity** is proven (same inputs → same outputs, same errors).
6. **Reliability** does not worsen.
7. CPU/RAM stay acceptable.
8. Migration + maintenance cost is justified.
9. A tested **rollback** exists.
10. Python **fallback** is kept without a dual-writer risk.

**Thresholds (preliminary, from the task):**

| Kind | Threshold |
|---|---|
| latency-sensitive | ≥ 20 % p95 improvement |
| throughput-bound | ≥ 30 % throughput improvement |
| memory-bound | ≥ 20 % RAM reduction |

Below threshold → a written justification is required, or the port is not done.

**Mandatory comparison:** Rust is compared against **optimized Python**, with
identical inputs/environment, warm-up, several runs, p50/p90/p95/p99,
throughput, CPU/RAM, error rate, integration cost and the **end-to-end** user
effect. A faster internal benchmark that does not move the user scenario is not
enough.

---

## 2. Candidate ranking (highest to lowest priority)

| Rank | Candidate | Character | Measured today | Rust upside (INFERRED) | Meets threshold? | Verdict |
|---|---|---|---|---|---|---|
| 1 | **Archive Engine** (inspect + safe extract) | CPU | inspect 2000 = 39 ms; extract 1000 = **216 ms** | 2–5× on CPU-bound parse/extract | **likely** on extract, borderline on inspect | **pilot candidate** |
| 2 | **File Processing** (hashing, large-file handling) | CPU/IO | upload 2 MiB = 23.7 ms; sha256 included | sha256 already C; modest upside | **no** for typical sizes | **hold** (revisit at larger sizes) |
| 3 | **Repository / code indexing** | CPU/IO + external | not measured in isolation | large upside on big repos | **NEEDS_MORE_DATA** | **hold** |
| 4 | **Event Processing** | I/O | event write 0.84 ms | none for a single INSERT | **no** | **keep Python** |
| 5 | **Execution Engine** | control | claim 2.4 ms; flat throughput (lock-bound) | async + typed states | **no** for speed; **maybe** for correctness later | **later, separate review** |
| — | DB access layer | I/O + lock | p99 706 ms @100c | pool (Python) already gives 19× | **no — Python pool wins first** | **PYTHON_OPTIMIZE** |
| — | HTTP/API layer | I/O | 0.6–1.7 ms | none | **no** | **keep Python** |
| — | OpenHands / LLM / GitHub | external | provider-bound | none | **no** | **keep Python** |
| — | Frontend | asset | 198 KB JS | n/a | **no** (asset work, not Rust) | **optimize assets** |
| — | MCP / memory / notifications | control | cheap | none | **no** | **keep Python** |

**Rust-first today: none.** The single highest-value change is a Python
connection pool ([PERFORMANCE_OPTIMIZATION_ROADMAP](./PERFORMANCE_OPTIMIZATION_ROADMAP.md) §3 P1).
The first Rust candidate, if the pilot confirms it, is the **Archive Engine**.

---

## 3. Candidate detail

### 3.1 Archive Engine (rank 1 — pilot)

- **Current:** `orchestration/archive_safety.py` (`open_zip`, `open_archive`,
  limits `max_entries=2000`, `max_total_uncompressed=256 MiB`, `max_depth=16`)
  and `orchestration/file_lab.py` (`extract_into_workspace`). **VERIFIED.**
- **Responsibility:** validate a ZIP/TAR (path traversal, symlink, zip-bomb,
  entry count, depth) and extract it safely into a workspace.
- **Character:** **CPU-bound** (VERIFIED: extract 1000 members = 216 ms;
  inspect 2000 = 39 ms).
- **Measured Python baseline:** as above.
- **Python optimization first:** stream instead of holding the whole archive;
  move extraction off the request thread; reuse a single decompressor. Expected
  to help the request path but not the raw CPU cost.
- **Rust feasibility:** high (`zip`/`flate2`/`tar` crates; a pure function
  `validate(bytes) -> Report` and `extract(bytes, dest) -> Manifest`).
- **Benchmark required:** the same archives (100/1000/2000 members; the
  declared-bomb and symlink cases), same machine, p50/p90/p99, before/after.
- **Risks:** a parser bug is a **security** bug (path traversal / symlink);
  mitigated by property/fuzz tests and parity tests against the Python limits.
- **Migration:** implement the Rust function, call it from `file_lab` behind a
  flag; keep the Python path; parity + fuzz first.
- **Fallback:** the Python implementation stays; a failed import uses it.
- **Abandon condition:** if the measured extract gain is < 20 % p95 **and** the
  user scenario does not improve, drop the port.

### 3.2 File Processing (rank 2 — hold)

- **Current:** `file_lab.upload` (sha256 + INSERT), `nexus_repo_reader.py`
  (`MAX_FILE_BYTES=400_000`, `MAX_PATHS_PER_REQUEST=8`). **VERIFIED.**
- **Character:** hashing is already C (`hashlib`); upload is I/O-bound.
- **Verdict:** no Rust upside for typical sizes; revisit if multi-hundred-MB
  files become common. **NEEDS_MORE_DATA** at larger sizes.

### 3.3 Repository / code indexing (rank 3 — hold)

- **Current:** `nexus_repo_reader.py`, `nexus_context.py` (project context
  refresh). Reading is via GitHub (external).
- **Character:** CPU (tree analysis) + external (GitHub fetch).
- **Verdict:** potential upside on large repositories, but **not measured**.
  A benchmark on a large repo is required before any decision. **NEEDS_MORE_DATA.**

### 3.4 Event Processing (rank 4 — keep Python)

- **Current:** `cloud_store._event` (one INSERT, 0.84 ms), `task_events`,
  `nexus_project_events`, notification lifecycle (`nexus_lab_store._notify`,
  `set_notification_read`, … — added in `main`). **VERIFIED.**
- **Verdict:** single-INSERT writes; no Rust benefit. **PYTHON_KEEP.**

### 3.5 Execution Engine (rank 5 — later, separate review)

- **Current:** `nexus_lab.py`, `nexus_lab_store.py` (claim 2.4 ms, leases,
  fencing, DAG gating, recovery).
- **Character:** control; the bottleneck is the DB lock, not the language.
- **Verdict:** **do not port before** the task model (leases, idempotency,
  cancellation, recovery) is stable and the DB pool is in place. If ported
  later, it is for **correctness/type-safety**, not measured speed — and only
  after a dedicated architecture review. **RUST_LATER.**

---

## 4. Decision matrix (one row per domain)

| Domain | Measured bottleneck | Optimized Python closes it? | Rust gain | Decision | Confidence |
|---|---|---|---|---|---|
| DB access | yes (lock convoy) | **yes (19×)** | marginal over pool | PYTHON_OPTIMIZE | high |
| HTTP/API | no (at low load) | yes | none | PYTHON_KEEP | high |
| Scheduler claim | yes (flat tput) | **yes (pool)** | marginal | PYTHON_OPTIMIZE | high |
| Dispatcher/execution | no (control) | n/a | none for speed | RUST_LATER | medium |
| OpenHands | external | n/a | none | PYTHON_KEEP | high |
| LLM providers | external | n/a | none | PYTHON_KEEP | high |
| Events | no | n/a | none | PYTHON_KEEP | high |
| Archive inspect | CPU (39 ms) | partially | 2–5× | **pilot** | medium |
| Archive extract | CPU (216 ms) | partially | 2–5× | **pilot** | medium |
| File hashing | no | n/a | none | PYTHON_KEEP | high |
| Repo indexing | not measured | unknown | unknown | NEEDS_MORE_DATA | low |
| GitHub | external | n/a | none | PYTHON_KEEP | high |
| MCP | no | n/a | none | PYTHON_KEEP | high |
| Memory | no | n/a | none | PYTHON_KEEP | medium |
| Notifications | no | n/a | none | PYTHON_KEEP | medium |
| Frontend assets | asset size | yes (minify/split) | n/a | PYTHON_OPTIMIZE | high |
| Startup/migrations | one-time | n/a | none | PYTHON_KEEP | high |

---

## 5. What would change a verdict

A candidate moves up only with **new measurements**:

- Archive: a benchmark showing extract p95 gain < 20 % → abandon.
- Repo indexing: a large-repo benchmark → promote or drop.
- Execution engine: a proven correctness/reliability need → separate review.
- Any domain: a new load profile where NEXUS-side compute dominates.

Until then, the ranking above stands and **nothing is implemented**.
