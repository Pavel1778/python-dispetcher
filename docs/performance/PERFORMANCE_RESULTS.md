# NEXUS — Performance Results

> **STATUS: MEASURED RESULTS.** Before/after for each phase of the performance
> program. Numbers are taken with the same instruments and environment as
> [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

---

## Phase 1 — PostgreSQL connection pool (MEASURED)

**Change:** the single connection + `threading.RLock` in
`orchestration/cloud_store.py` is replaced by a `psycopg_pool.ConnectionPool`
(commit `c3127e8`, plus `67769d1` for close/reopen semantics). No schema, API
or business-logic change.

**Instrument:** `benchmarks/db_concurrency_bench.py` (same SQL, same data,
same PostgreSQL 17.11). Variant A = the old model; B = the pool. A/B are 2-run
averages.

| Conc | A tput ops/s | A p99 ms | B tput ops/s | B p99 ms | p99 change |
|---:|---:|---:|---:|---:|---:|
| 1 | 2,321 | 1.7 | 4,128 | 1.3 | −24 % |
| 5 | 3,364 | 23.5 | 4,690 | 3.2 | −86 % |
| 10 | 3,132 | 63.9 | 5,139 | 4.8 | −92 % |
| 25 | 2,950 | 189.9 | 4,178 | 12.0 | −94 % |
| 50 | 2,670 | 402.1 | 4,471 | 20.0 | −95 % |
| 100 | 2,524 | **706.4** | 4,321 | **37.2** | **−95 %** |

**Result:** the p99 target (≤ 50 ms at 100 concurrent) is met (37 ms); throughput
improves ~1.7×; the lock convoy is gone. This is pure Python — no Rust.

| Metric | Before | After | Difference | Result |
|---|---|---|---|---|
| DB p99 @100c | 706–747 ms | ~35–37 ms | −95 % | **target met (≤50 ms)** |
| DB throughput @10c | 2.5–3.1k | 4.3–5.1k | +70–90 % | improved |
| DB throughput @50c | 2.7–3.1k | 4.5k | +50–70 % | improved |

**Correctness (VERIFIED):** the full test suite passes with the pool; the
connection is always returned (success, error, cancel); an outage is still a
503 with no driver text; a dropped connection still costs the caller nothing;
preparation stays off by default; an aborted transaction is still recovered.

---

## Phase 2 — API concurrency (see CONCURRENCY_AND_BACKPRESSURE.md)

_To be filled after Phase 2._

## Phase 3 — Scheduler (see SCHEDULER_PERFORMANCE.md)

_To be filled after Phase 3._

## Phase 4 — Files & archive (see ARCHIVE_ENGINE_BENCHMARK.md)

_To be filled after Phase 4._

## Phase 5 — Rust archive pilot (see docs/architecture/RUST_ARCHIVE_ENGINE_PILOT.md)

_To be filled after Phase 5._
