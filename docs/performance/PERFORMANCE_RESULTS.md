# NEXUS — Performance Results (final)

> **STATUS: MEASURED RESULTS.** Before/after for every phase of the performance
> program. Numbers use the same instruments and environment as
> [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

---

## Summary (MEASURED)

| Metric | Baseline | After | Change |
|---|---:|---:|---:|
| DB p99 @100 concurrent | 706-747 ms | **~35 ms** | **-95 %** |
| API p99 @50 concurrent | 1264-1884 ms | **~81 ms** | **-95 %** |
| API throughput @50 concurrent | 275-491 ops/s | **~697 ops/s** | **+40-150 %** |
| Archive extract 1000 members | 165 ms | **78 ms** | **-53 %** |
| app.js over the wire | 207 KB | **52 KB** | **-75 %** |
| RSS / threads / FDs / DB conns (140 s) | - | **flat** | no leak |

All of these are **Python** changes. The Rust pilot did not justify a port.

## Phase 1 — PostgreSQL connection pool (MEASURED)

Single connection + `RLock` -> `psycopg_pool.ConnectionPool`
(`orchestration/cloud_store.py`; commits `c3127e8`, `67769d1`). No schema, API
or business-logic change.

| Conc | A (before) tput | A p99 | B (after) tput | B p99 |
|---:|---:|---:|---:|---:|
| 1 | 2,321 | 1.7 | 4,128 | 1.3 |
| 10 | 3,132 | 63.9 | 5,139 | 4.8 |
| 50 | 2,670 | 402.1 | 4,471 | 20.0 |
| 100 | 2,524 | **706.4** | 4,321 | **37.2** |

Target (<= 50 ms p99 @100) met. Full suite green with the pool.

## Phase 2 — API concurrency (MEASURED)

The real cause of the API tail was the default accept backlog
(`request_queue_size = 5`), not the database. `NexusHTTPServer` sets it to 128
(`orchestration/web_server.py`; commit `c44fbe1`).

| Conc | Before tput | Before p99 | After tput | After p99 |
|---:|---:|---:|---:|---:|
| 25 | 497 | 69.2 | 698 | 45.8 |
| 50 | 491 | **1264-1884** | 697 | **81.5** |

## Phase 3 — Scheduler (MEASURED)

Claims scale with the number of **runs** (~1044/s across 10 runs), not with
workers on one run (flat ~350/s) - a deliberate guarantee (the run's `iteration`
budget must be exact). Not a bottleneck for the deployment's load. No change
made; see [SCHEDULER_PERFORMANCE](./SCHEDULER_PERFORMANCE.md).

## Phase 4 — Files & archive (MEASURED)

The extraction bottleneck was database round trips (150 ms of 165 ms), not CPU
(unpack 15.5 ms). `executemany` in `file_lab.extract_into_workspace` (commit
`2a34a06`):

| Members | Before | After |
|---|---:|---:|
| 100 | 18.3 ms | 9.8 ms |
| 1000 | 165.0 ms | **77.8 ms** |

## Phase 5 — Rust archive pilot (MEASURED, NOT ENABLED)

An isolated Rust zip unpacker is 3.3-4.6x faster than the Python unpack as a
function, but the unpack is a small part of the operation, so the end-to-end
gain is ~13 % - below the >= 20 % p95 threshold. **Verdict: PYTHON_KEEP.** No
Rust code is in the runtime. See
[RUST_ARCHIVE_ENGINE_PILOT](../architecture/RUST_ARCHIVE_ENGINE_PILOT.md).

## Phase 6 — Frontend (MEASURED)

Static assets are now gzipped when the client accepts it (commit `f696a9d`):
`app.js` 206,711 B -> 52,146 B; `index.html` and `app.css` likewise.

## Phase 7 — Long-run stability (MEASURED)

140 s, 8 readers, 90,826 requests: RSS 71 MB, threads ~25-28, FDs ~30, DB
connections 9 - all flat. No leak. See
[LONG_RUN_STABILITY](./LONG_RUN_STABILITY.md).

## Phase 8 — Observability (MEASURED)

`GET /api/system` now reports `db_pool` (size, idle, waiting, requests, wait
time; no credential). `waiting > 0` is the connection-pressure signal. See
[PERFORMANCE_OPERATIONS_GUIDE](./PERFORMANCE_OPERATIONS_GUIDE.md).

## Phase 9 — End-to-end scenarios (MEASURED)

`benchmarks/e2e_scenarios.py`, stand-in external services:

| Scenario | p50 | p95 |
|---|---:|---:|
| open_dashboard | 0.72 | 0.78 |
| list_projects | 1.14 | 1.42 |
| open_conversation | 1.46 | 1.66 |
| fetch_history | 1.82 | 2.07 |
| notifications | 1.89 | 2.00 |
| upload_file | 2.71 | 3.09 |
| run_status | 6.73 | 7.29 |
| send_message (stand-in LLM) | 19.88 | 24.91 |
| start_run (stand-in planner) | 47.03 | 49.72 |

Read paths are single-digit ms; the LLM-bound paths are dominated by the
stand-in provider. In production these are dominated by the network and the
real provider, not by NEXUS.

## Not measured (UNKNOWN / BLOCKED)

- Production p95/p99 under real load (BLOCKED: no paid Render metrics / OTel).
- Real OpenHands/LLM/GitHub latency (BLOCKED: no load on real services).
- A multi-hour soak (UNKNOWN; the 140 s sample is flat).
