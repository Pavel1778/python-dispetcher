# NEXUS — Performance Operations Guide

> **STATUS: OPERATIONS GUIDE.** How to measure, tune and diagnose performance
> in NEXUS. Written with the performance program (Phases 0–8).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Related: [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md),
[PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md),
[DB_POOL_OPERATIONS](./DB_POOL_OPERATIONS.md),
[CONCURRENCY_AND_BACKPRESSURE](./CONCURRENCY_AND_BACKPRESSURE.md),
[SCHEDULER_PERFORMANCE](./SCHEDULER_PERFORMANCE.md),
[ARCHIVE_ENGINE_BENCHMARK](./ARCHIVE_ENGINE_BENCHMARK.md),
[LONG_RUN_STABILITY](./LONG_RUN_STABILITY.md).

---

## 1. The instruments (VERIFIED)

| Instrument | Measures |
|---|---|
| `tools/benchmark.py` | read paths, chat turn, run start, upload (the real server) |
| `benchmarks/db_concurrency_bench.py` | DB access under concurrency (A vs B) |
| `benchmarks/api_bench.py` | HTTP paths, single + concurrency sweep |
| `benchmarks/scheduler_bench.py` | start_run, claim, reclaim, cleanup |
| `benchmarks/events_files_bench.py` | event write, upload, archive inspect/extract |
| `benchmarks/rust-db/`, `benchmarks/rust-archive/` | isolated Rust prototypes |

Run any of them with `TEST_SUPABASE_DB_URL=postgresql://... python <instrument>`.
They reset the schema, so use a throwaway database, never production.

## 2. What each number means (MEASURED, this program)

| Metric | Baseline | After |
|---|---:|---:|
| DB p99 @100 concurrent | 706–747 ms | **~35 ms** (pool) |
| API p99 @50 concurrent | 1264–1884 ms | **~81 ms** (accept backlog) |
| Archive extract 1000 members | 165 ms | **78 ms** (executemany) |
| app.js over the wire | 207 KB | **52 KB** (gzip) |
| RSS / threads / FDs / DB conns (140 s) | — | **flat** (no leak) |

## 3. Tuning knobs (VERIFIED)

| Variable | Default | When to change |
|---|---|---|
| `NEXUS_DB_POOL_MAX` | 8 | raise only if `/api/system` `db_pool.waiting` > 0 **and** the database/PgBouncer limit allows |
| `NEXUS_DB_POOL_MIN` | 1 | raise to keep more warm connections under steady load |
| `NEXUS_DB_POOL_TIMEOUT` | 30 s | lower to fail faster under an outage |
| `PSYCOPG_PREPARE` | off | `session` only for a direct, session-mode DSN (never behind a pooler) |
| `NEXUS_LAB_MAX_ACTIVE_TASKS` | 8 | raise for more concurrent tasks, if the OpenHands accounts allow |

## 4. Diagnosing a slowdown

1. **`GET /api/system`** — `database.ok`, `openhands.ok`, `db_pool.waiting`.
   `db_pool.waiting > 0` = connection pressure; `database.ok=false` = the
   database, not NEXUS.
2. **`GET /readyz`** — a real DB round trip; 503 means the database is down.
3. **Which layer?** Run `benchmarks/db_concurrency_bench.py` (DB alone) and
   `benchmarks/api_bench.py` (HTTP). If the DB is fast and the API is slow under
   concurrency, it is the HTTP layer (accept queue / threads); if both are slow,
   it is the database.
4. **Read vs write:** read paths are single-digit ms; a slow path is usually a
   database round trip (an N+1) — look for a loop of `cur.execute` that could be
   one `executemany` or one query.

## 5. Known limits (VERIFIED / UNKNOWN)

- **Thread-per-connection**, HTTP/1.0 (no keep-alive): fine for the current
  traffic, a limit at very high concurrency (UNKNOWN, not measured).
- **One run claims one task at a time** (by design); many runs claim in parallel
  ([SCHEDULER_PERFORMANCE](./SCHEDULER_PERFORMANCE.md)).
- **External services dominate the long waits** (OpenHands, LLM, GitHub):
  no NEXUS change moves them; measure them separately.
- **Production p95/p99** need paid Render metrics or OTel (BLOCKED here).

## 6. Rules (from the program)

- No speed claim without a reproducible benchmark under the same scenario.
- Optimize Python first; compare Rust against the **optimized** Python.
- Correctness and safety before throughput; never weaken a guarantee for a
  number.
- The database is the shared resource: bound the pool, do not move the queue.
