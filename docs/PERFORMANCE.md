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

## A running server under concurrent load (2026-11-20, this session)

The measurements above are one call at a time, in-process. This section is the
other half: a **real** `python -m orchestration.web_server` started the way a
deployment starts it, driven over real sockets by many concurrent clients, with
the **live** provider keys (Gemini and OpenRouter both answered).

The generator is `tools/loadtest.py` (stdlib only — there is no `wrk`/`ab` in
this environment): a pool of threads, no sleep between requests, every latency
recorded with `perf_counter`, every non-2xx counted.

```bash
python -m orchestration.web_server --host 127.0.0.1 --port 12000 --token TOKEN &
python tools/loadtest.py --base http://127.0.0.1:12000 --token TOKEN \
    --clients 16 --duration 10 --mix reads --out reads.json
```

### Read paths, 4 vs 16 concurrent clients (10 s each, 0 errors)

| clients | throughput | p50 | p90 | p95 | p99 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | **1340 req/s** | 2.95 ms | 4.21 ms | 4.75 ms | 6.03 ms |
| 16 | 1069 req/s | 6.53 ms | 10.49 ms | 12.07 ms | 17.01 ms |

The mix is what the studio polls: `/api/studio`, the project list, one project,
the conversation list, one conversation, its messages, `/api/commands`. At 16
clients the machine's 4 cores are shared with the client threads, so the p50
rises while throughput stays near a thousand requests a second; the reads are
database-bound and stay single-digit to low-double-digit milliseconds.

### A real chat turn, end to end (live provider)

Ten turns through the real cascade against `openrouter` (`openai/gpt-4o-mini`
served them after `gemini`):

| n | min | p50 | p90 | max | mean |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 914 ms | **1239 ms** | 1592 ms | 1667 ms | 1254 ms |

This is the number a person actually feels, and it is the **provider's**, not
NEXUS's: the same server answers a read in 3–6 ms. It is recorded so that no one
reads the in-process `chat_turn_ms` (19.6 ms against the stand-in LLM) as a
promise about a real turn.

### Footprint (the same running process)

| metric | idle | under 16-client load |
| --- | ---: | ---: |
| RSS | 65 MB | 67 MB |
| CPU | 0.9 % | 10–15 % |
| threads | 8 | 14 |
| database | 13 MB (3 projects, 30 conversations, 40 messages each) | — |

The process is a single Python interpreter with one PostgreSQL connection; the
thread count grows by one per in-flight request under load and returns to its
idle set afterwards. Memory is flat: nothing accumulates per request.

### The write path when no provider is configured

`--mix writes` against a server with **no** provider key answers every chat turn
`502` with `no orchestrator provider succeeded (no providers configured)`, and
the user turn is still stored. That is correct — a missing provider is said, not
faked — but it means a "write latency" measured without keys is the failure
path, not a turn. Measure a turn only with a working provider (as above).

### Re-measured on the merged build (same session)

After PR #53 was merged into `main` and `main` merged back into this branch, the
same read mix was measured again with the same instrument on the merged code:

| clients | throughput | p50 | p90 | p99 | errors |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 1 224 req/s | 3.16 ms | 4.85 ms | 7.31 ms | 0 |
| 16 | 1 074 req/s | 6.22 ms | 9.47 ms | 14.15 ms | 0 |

That is the same band as the pre-merge run (1 340 / 1 069 req/s) within
run-to-run noise on a shared 4-core box. **The merge did not regress the read
paths**, and this instrument shows no measurable speedup from it either — the
optimisations in section 6 are measured by `benchmarks/`, not by this mix, and
no claim is made here that the two agree.

A **real chat turn** on the merged build (5 turns, live cascade) was faster than
before the merge: min 645 ms, **p50 896 ms**, max 981 ms, mean 829 ms (was p50
1 239 ms). That is consistent with section 6 item 4 (the chat turn's DB
round-trips fell from 7–9 transactions to 2); it is a single 5-sample reading on
a shared box, so treat it as a direction, not a precise figure.

## The audit's own finding: a 140x slowdown from PostgreSQL's JIT (2026-11-20)

Auditing the running system turned up a real performance defect that no test
covered. It is fixed, and the fix is pinned by `tests/test_jit_settings.py`.

**What was wrong.** `LabStore.queue_cleanups` (the worker's retention scan, run
on every pass) unions five sources with `jsonb_array_elements` lateral joins.
The planner prices that in the millions, so PostgreSQL's **JIT compiled the
plan**: on a database with ~6 000 tasks the compile took **500–670 ms**, and
the query then returned zero rows in microseconds.

**Measured, with the project's own benchmark** (`benchmarks/scheduler_bench.py`,
`--repeat 10`):

| metric | before | after |
| --- | ---: | ---: |
| `queue_cleanups_ms` p50 | **635 ms** | **4.5 ms** |
| p90 | 705 ms | 6.4 ms |

`EXPLAIN (ANALYZE)` showed `JIT: Timing … Total 557 ms` against
`Execution Time: 558 ms` — the whole cost was compilation. Setting `jit = off`
on the same query took it to **3–6 ms** (a ~170× difference).

**What was tried and did not work** (recorded so nobody repeats it):

- **An index** on `nexus_run_tasks(conversation_id, resume_conversation_id)`:
  581 ms → 579 ms. The predicate is not the cost.
- **Raising `jit_above_cost`** to 10 000 000: still 600 ms. The plan's estimate
  exceeds even that, so JIT still engaged.
- **`ANALYZE`** made it *worse* in one direction (8 ms → 604 ms): accurate
  statistics made the planner more confident the plan was expensive, so JIT
  engaged where stale statistics had left it off. The benchmark still runs
  `ANALYZE` before this measurement, so it reports the real plan.

**The fix.** `PostgresStore` opens every connection with `jit = off`
(`cloud_store._jit_options`). This is a control plane — short OLTP statements,
no analytical scans — so JIT has nothing to win here. `NEXUS_DB_JIT=on` restores
the server's own behaviour for a deployment that runs heavy queries through the
same database.

**Also fixed in the same audit:** `benchmarks/scheduler_bench.py` reset the
schema on the way in but left its runs behind. A run left `running` with tasks
still `dispatched` holds the global in-flight budget until their leases expire,
so the next thing to use the database was refused every claim. It now resets the
schema on the way out too, and a run leaves **0 runs, 0 tasks** behind.

## What was not done, and why

- **No language migration.** A Go/Rust rewrite of the API would not move these
  numbers: the read paths are already sub-millisecond of database time and the
  slow paths are network-bound (provider, OpenHands, GitHub). The cost — a
  second implementation, a second test suite, the loss of the stdlib-only
  adapters and the entire existing test corpus — is not justified by the
  measured profile. A hybrid (a Rust streaming engine, a Go worker) would only
  pay off under a load this deployment does not have, and would split the
  invariants the AGENTS.md documents in one place. The evidence is the table
  above.
- **No `orjson` (Rust-backed JSON).** Measured ~2.8–2.9× faster than
  `json` on a 1.3 MB trajectory-like payload (9.2 → 3.2 ms). Not adopted: the
  repository is deliberately stdlib-only (+psycopg, +cryptography); the hot
  paths are database and network, not JSON tokenising; and a drop-in risks
  byte-format drift exactly where byte fidelity is load-bearing (the
  canonical evidence encoding) — orjson would never be applied there, and a
  partial adoption is a trap. Recorded here so the measurement is not lost.
  Re-evaluate per release with `tools/benchmark.py`; adopt only with the
  signing path schema-gated.
- **No caching layer.** The read paths are cheap and correctness (project
  isolation, a message appearing at once) matters more than a cache that could
  serve one project's data to another. Nothing is cached between projects.

## Adding a measurement

Add one entry to `tools/benchmark.py`'s `results["measurements"]` and it appears
in both the JSON and the comparison table. Keep the method: a real server, a
real database, a real round trip — never a simulated one.
