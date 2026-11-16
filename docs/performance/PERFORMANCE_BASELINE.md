# NEXUS — Performance Baseline

> **STATUS: MEASURED BASELINE.** The starting numbers for the performance
> program, re-taken on the current code before any change.
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `a7442632bb05492f65f72b1bf2e3ec94436625d5` (`origin/main`)
- **Environment:** PostgreSQL 17.11, Python 3.13.15, 4 vCPU / 16 GB RAM,
  isolated database `nexus_perfevo`.
- **Instruments:** `tools/benchmark.py`, `benchmarks/db_concurrency_bench.py`,
  `benchmarks/api_bench.py`, `benchmarks/scheduler_bench.py`,
  `benchmarks/events_files_bench.py`.

---

## 1. Method (MEASURED)

- Real server (`orchestration.web_server.App` + `WebServer`) against a real
  PostgreSQL; the stand-in OpenHands/GitHub/LLM servers the test suite uses.
- Warm-up before measurement; repeated runs; p50/p90/p99 reported.
- The database is reset per run (`_reset_schema`) so numbers are not polluted.
- No production load; no real OpenHands Cloud or paid LLM calls.

## 2. Read paths (MEASURED, `tools/benchmark.py`, repeat 30, 5 projects × 20 conversations × 50 messages)

| Path | p50 | p90 | p99 |
|---|---:|---:|---:|
| studio_status | 0.75 | 1.12 | 9.45 |
| list_projects | 1.24 | 1.61 | 2.93 |
| open_project | 1.57 | 1.87 | 2.04 |
| list_conversations | 1.67 | 2.10 | 4.27 |
| open_conversation | 1.57 | 2.01 | 2.78 |
| fetch_history | 2.50 | 2.96 | 3.04 |
| commands_registry | 0.86 | 1.01 | 2.20 |
| search_by_content | 1.87 | 2.63 | 4.59 |
| export_conversation | 2.90 | 3.29 | 3.67 |
| chat_turn (stand-in LLM) | 19.26 | 21.58 | 24.56 |
| start_run (stand-in) | 51.26 | 55.64 | 55.64 |
| store_dashboard_counts | 0.60 | 0.61 | 1.98 |
| file_upload (4 KiB) | 2.80 | 2.93 | 4.71 |

All values in milliseconds.

## 3. DB concurrency (MEASURED, `benchmarks/db_concurrency_bench.py`, per-worker 40)

Variant A = the current single connection + `RLock`; B = a Python
`psycopg_pool`.

| Conc | A tput ops/s | A p99 ms | B tput ops/s | B p99 ms |
|---:|---:|---:|---:|---:|
| 1 | 1,942 | 3.1 | 2,867 | 1.7 |
| 5 | 2,242 | 7.0 | 3,204 | 5.3 |
| 10 | 2,520 | 44.3 | 4,337 | 5.5 |
| 25 | 2,803 | 201.5 | 4,345 | 11.8 |
| 50 | 3,056 | 359.8 | 4,530 | 20.4 |
| 100 | 3,140 | **747.1** | 4,425 | **34.7** |

**The headline baseline:** A's p99 grows without bound while throughput stays
flat (a lock convoy); B's p99 stays ~35 ms. This is the number Phase 1 targets.

## 4. Scheduler / execution (MEASURED, `benchmarks/scheduler_bench.py`)

| Metric | Value |
|---|---|
| `start_run` (40-task plan) p50 | ~17 ms |
| `claim_task` p50 | ~2.4 ms |
| claim throughput, 1→25 workers | flat **~250–300 claims/s** |
| `reclaim_expired` scan p50 | ~0.5 ms |
| `queue_cleanups` p50 | ~2.5 ms |
| dashboard counts p50 | ~0.6 ms |

## 5. Events & files (MEASURED, `benchmarks/events_files_bench.py`)

| Metric | p50 |
|---|---:|
| event write | 0.84 ms |
| file upload 4 KiB / 64 KiB / 512 KiB / 2 MiB | 1.3 / 1.8 / 8.1 / 23.7 ms |
| archive inspect 100 / 1000 / 2000 members | 2.3 / 20.9 / 39.3 ms |
| archive extract 1000 members | 216 ms |

## 6. Frontend (MEASURED, static analysis)

- `orchestration/studio/app.js`: 198,524 B (gzip 50,473 B); whole `studio/`
  ~428 KB.
- Polling: 1 s (pending) and 5 s (work), both visibility-gated.

## 7. Production (MEASURED, read-only)

- `/healthz` 200, `/readyz` 200; `/api/studio` 401 in ~0.19 s; `/` 303 in
  ~0.18 s. Dominated by network + Cloudflare + cold start, not Python compute.

## 8. Not measured (UNKNOWN / BLOCKED)

- Production p95/p99 under real load (BLOCKED: no paid Render metrics / OTel).
- Long-run RSS/threads/descriptors (UNKNOWN: Phase 7).
- Real OpenHands/LLM/GitHub latency (BLOCKED: no load on real services).
