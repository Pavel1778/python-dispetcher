# Performance & Resource Baseline

All measurements below are **VERIFIED** against a live UTF-8 PostgreSQL 16.2
instance and the real NEXUS Python stack (`orchestration/`, `benchmarks/`,
`tests/`). No numbers are extrapolated.

---

## 1. Resource-usage baseline (target 2-vCPU / 8 GB laptop profile)

Measured via `/proc/self/status` (`VmRSS`) and `PostgresStore.pool_stats()` on a
live `WebServer` + `App` + `PostgresStore` + `LabScheduler` process:

| State | Process RSS (`VmRSS`) | Share of 8 GB RAM | Pool connections (`size` / `max`) | Pool `waiting` |
|---|---:|---:|---:|---:|
| Python + imports (before `App` boot) | `55.86 MB` | `0.68%` | `0 / 8` | `0` |
| Idle `WebServer` + `App` + `PostgresStore` | `57.45 MB` | `0.70%` | `2 / 8` | `0` |
| After 1 autonomous run (3-task DAG, verified) | `57.45 MB` | `0.70%` | `2 / 8` | `0` |
| After 4 concurrent runs (40 tasks total, 389 DB ops) | `57.88 MB` | `0.71%` | `4 / 8` | `0` (`wait_ms=22`) |

**Key takeaway:** NEXUS adds only **~1.6 MB** over the base Python interpreter at
idle and **+0.43 MB** across 40 executed and verified DAG tasks, well within the
budget of an i3 / 8 GB self-hosted machine.

---

## 2. Scheduler & task-queue benchmark (`benchmarks/scheduler_bench.py --repeat 40`)

Run with `NEXUS_LAB_MAX_ACTIVE_TASKS=0` on PostgreSQL 16.2:

| Operation | Baseline (before) | Optimized (after) | Speedup |
|---|---:|---:|---:|
| **`start_run_40_tasks_ms` (p50)** | `11.587 ms` | **`3.357 ms`** | **3.45x faster (−71.0%)** |
| **`start_run_40_tasks_ms` (p90 / mean)** | `13.200 ms` / `12.034 ms` | **`3.861 ms` / `3.648 ms`** | **3.42x / 3.30x faster** |
| **`claim_task_ms` (p50 / mean)** | `2.040 ms` / `2.025 ms` | **`1.557 ms` / `1.484 ms`** | **1.36x faster (−26.7%)** |
| **`concurrent_claim` (5 workers, 500 tasks)** | `454.1 claims/s` (`1.101 s`) | **`564.1 claims/s` (`0.886 s`)** | **+24.2% throughput** |
| **`concurrent_claim` (1 worker, 500 tasks)** | `432.3 claims/s` (`1.157 s`) | **`511.1 claims/s` (`0.978 s`)** | **+18.2% throughput** |
| **`reclaim_expired_scan_ms` (p50)** | `0.588 ms` | **`0.384 ms`** | **1.53x faster (−34.7%)** |
| **`queue_cleanups_ms` (p50)** | `3.595 ms` | **`2.578 ms`** | **1.39x faster (−28.3%)** |
| **`dashboard_counts_ms` (p50)** | `0.390 ms` | **`0.341 ms`** | **1.14x faster (−12.6%)** |

---

## 3. Core orchestration lifecycle benchmarks

| Scenario | Baseline (p50 / p95) | Optimized (p50 / p95) | Speedup |
|---|---:|---:|---:|
| **10-task sequential DAG full lifecycle (`start_run` + `tick` to `completed`)** | `136.347 ms` / `160.342 ms` | **`61.128 ms` / `62.847 ms`** | **2.23x faster (p95: 2.55x)** |
| **Single-stage inline specification planning (`SpecPlanner.start`)** | `11.120 ms` / `12.154 ms` | **`6.761 ms` / `7.351 ms`** | **1.64x faster (−39.2%)** |
| **Worker sweep over 20 idle paused runs (`active_run_ids` + `tick`)** | `46.555 ms` / `64.064 ms` | **`0.349 ms` / `0.680 ms`** | **133.4x faster (−99.25%)** |
| **Project dashboard stats (`ProjectService.stats`)** | `1.261 ms` / `1.703 ms` | **`0.760 ms` / `0.901 ms`** | **1.66x faster (p95: 1.89x)** |
| **`PostgresStore` startup migration check (39 migrations applied)** | `81.40 ms` | **`3.45 ms`** | **23.6x faster (−95.8%)** |

---

## 4. End-to-end HTTP API scenarios (`benchmarks/e2e_scenarios.py`)

| Scenario | Baseline (p50 / p95) | Optimized (p50 / p95) | Speedup |
|---|---:|---:|---:|
| **`send_message` (full chat turn)** | `13.88 ms` / `17.09 ms` | **`7.60 ms` / `7.71 ms`** | **1.83x faster (p95: 2.22x)** |
| **`start_run` (plan + start run over HTTP)** | `24.65 ms` / `28.75 ms` | **`14.90 ms` / `15.34 ms`** | **1.65x faster (p95: 1.87x)** |
| **`run_status` (`GET /api/v1/projects/{id}/runs/{run}`)** | `3.88 ms` / `5.47 ms` | **`2.64 ms` / `3.95 ms`** | **1.47x faster (−32.0%)** |
| **`fetch_history`** | `1.77 ms` / `2.29 ms` | **`1.60 ms` / `1.82 ms`** | **1.11x faster (p95: 1.26x)** |
| **`open_dashboard`** | `0.88 ms` / `1.02 ms` | **`0.84 ms` / `0.93 ms`** | **1.05x faster** |
| **`list_projects`** | `1.21 ms` / `1.31 ms` | **`1.10 ms` / `1.21 ms`** | **1.10x faster** |
| **`upload_file`** | `2.03 ms` / `2.24 ms` | **`1.71 ms` / `2.71 ms`** | **1.19x faster (p50)** |

---

## 5. Full test suite execution time

| Suite | Baseline | Optimized | Speedup |
|---|---:|---:|---:|
| **Full backend + integration + E2E pytest suite (~2,050 tests)** | `~25–29 min` | **`~4 min 40 s`** | **~6.0x faster** |

---

## 6. Architectural optimizations behind these numbers

1. **Batched DAG task insertion (`LabStore._insert_tasks`):** Replaced $N$ per-task
   `INSERT` statements with a single parameterized `INSERT INTO nexus_run_tasks ... SELECT * FROM unnest(...)`.
2. **Consolidated scheduler tick & run-detail transactions (`LabScheduler`, `LabStore`):**
   Added `_tick_run_row`, multi-milestone `record_run_milestone`, folded
   `completed_at` into `_apply_run_state`, filtered `pending_stops` by
   `abandoned <> '[]'::jsonb`, and added single-transaction `get_run_detail`.
3. **Event-driven worker wakeup & idle-run filtering (`LabWorker`, `App`):**
   Added `LabWorker.wake()` / `App.wake_lab()` triggered immediately on run start,
   resume, retry, or cancel, and filtered `active_run_ids()` to skip paused/failed
   runs that have no active tasks or pending executor stops.
4. **Single-transaction chat turn context & message batching (`ConversationService`):**
   Added `turn_context`, `start_generation_with_context`, and `add_messages`,
   reducing chat-turn DB round-trips from 7–9 transactions to 2.
5. **In-memory single-stage `SpecPlanner.start` context reuse (`SpecPlanner`, `SpecService`):**
   Reuses the already-built `SpecIndex` (`sections`, `requirements`, `links`) and
   loaded specification row on the interactive single-stage path, and added
   `SpecService.stage_context_internal` for multi-stage background planning.
6. **Single-query `ProjectService.stats` & `ProjectRepositoryService` lookups:**
   Consolidated 4 `COUNT(*)` queries in `ProjectService.stats` into 1 query, and
   folded `primary_repository`, `primary_branch`, and `account_for_repository`
   into single-query helpers accepting an existing transaction cursor.
7. **Single-transaction `PostgresStore.apply_migrations` & catalog `verify_schema`:**
   Cached migration file SHA-256 digests in memory, checked all applied migrations
   in one `SELECT filename, checksum FROM schema_migrations`, and replaced
   `information_schema.columns` in `verify_schema` with `pg_class` / `pg_namespace` /
   `pg_attribute`.

---

## 8. Re-measured 2026-11-25 (PostgreSQL 17.12, Python 3.13, loopback)

Environment: Debian trixie, PostgreSQL 17.12 on the same host, Python 3.13.15,
`psycopg` 3.3.9, the real `WebServer`/`App`/`PostgresStore` and the same
stand-in OpenHands/GitHub/LLM servers the test suite uses. Command:

```bash
TEST_SUPABASE_DB_URL="postgresql://…/nexus_bench" \
  python tools/benchmark.py --out bench_after.json
```

`tools/benchmark.py` resets the schema first, so the numbers are not polluted
by earlier data. Every value is a real HTTP round trip.

| Path | p50 (ms) | p99 (ms) | n |
|---|---:|---:|---:|
| `studio_status_ms` (`GET /api/studio`) | 0.76 | 1.14 | 15 |
| `list_projects_ms` | 1.22 | 1.58 | 15 |
| `open_project_ms` | 1.69 | 1.91 | 15 |
| `list_conversations_ms` | 1.64 | 2.00 | 15 |
| `open_conversation_ms` | 1.46 | 2.08 | 15 |
| `fetch_history_ms` | 2.41 | 2.76 | 15 |
| `search_by_content_ms` | 1.86 | 2.27 | 15 |
| `export_conversation_ms` | 2.50 | 2.83 | 15 |
| `chat_turn_ms` (fake provider) | 11.58 | 15.45 | 5 |
| `start_run_ms` (plan + store) | 25.16 | 31.66 | 5 |
| `queue_claim_ms` | 3.41 | 6.24 | 15 |

**Read-path p99 is 1.1–2.8 ms**, well inside the 50 ms target. The two slower
paths are not reads: a chat turn includes the provider round trip, and starting
a run includes planning and storing a plan. Neither is a database problem, and
neither is improved by rewriting the read path.

What this increment changed on the hot paths, and what it did not:

* **Added:** one `perf_counter()` pair and one histogram observation per
  OpenHands HTTP call; one counter increment per claim and per evidence
  verification; one status/duration observation per JSON/HTML response. These
  are in-memory operations behind a lock and are not visible above the noise.
* **Not changed:** the SQL, the indexes, the connection pool, the JSON encoder
  or the event loop. The p99 target was already met; the work that moves
  *dispatch* latency is the LISTEN/NOTIFY wake-up (`docs/RFC-001_EVENT_DRIVEN.md`),
  which removes up to one poll interval (default 5 s) between "a run is
  started" and "a worker looks at it". That is a correctness-preserving latency
  win, not a read-path one.

**Honest limits.** These are loopback numbers on one host. A deployed service
adds network RTT to PostgreSQL and to OpenHands; the read paths remain a small
number of indexed queries, but the absolute values there will be higher. No
claim is made here about a remote database.
