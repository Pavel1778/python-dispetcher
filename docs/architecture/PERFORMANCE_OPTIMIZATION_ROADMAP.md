# NEXUS — Performance Optimization Roadmap

> **STATUS: MEASURED PLAN.** Built on this session's benchmarks (PostgreSQL
> 17.11, Python 3.13.15, 4 vCPU / 16 GB, isolated DB). No production change.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.
>
> Matrix: [PERFORMANCE_DRIVEN_RUST_MATRIX](./PERFORMANCE_DRIVEN_RUST_MATRIX.md).
> Related: [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md),
> [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md),
> [RUST_MIGRATION_RISK_REGISTER](./RUST_MIGRATION_RISK_REGISTER.md).
>
> **Hybrid refinement (2026-11-15):** the P1 DB-pool work in this document is
> Phase 1 of the hybrid plan; the first Rust candidate is the Archive Engine,
> not the DB layer. See
> [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md) and
> [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md).

---

## 1. Current performance baseline (MEASURED, this session)

Read paths (real server, real PostgreSQL, p50/p90/p99 ms):

| Path | p50 | p90 | p99 |
|---|---:|---:|---:|
| studio_status | 0.83 | 0.92 | 1.39 |
| list_projects | 1.23 | 1.32 | 1.43 |
| open_project | 1.69 | 2.32 | 3.08 |
| list_conversations | 1.38 | 1.46 | 1.65 |
| commands | 0.60 | 0.69 | 0.72 |
| fetch_history | 2.38 | 3.54 | 4.76 |
| search_by_content | 2.05 | 2.25 | 2.41 |
| export_conversation | 3.33 | 3.78 | 6.08 |
| chat_turn (stand-in LLM) | 18.0 | 25.6 | 27.5 |
| start_run (stand-in) | 48.4 | 51.5 | 51.5 |
| store_dashboard_counts | 0.37 | 0.62 | 2.14 |
| file_upload 4 KiB | 2.73 | 4.00 | 4.36 |

Control-plane (real stores): `start_run` 40 tasks 17.5 ms; `claim_task` 2.4 ms;
`reclaim_expired` 0.49 ms; `queue_cleanups` 2.5 ms; event write 0.84 ms.

Concurrency (the headline): single-conn + RLock p99 **706 ms** @100c vs
connection pool **37 ms** @100c (Python) vs **35 ms** (Rust/SQLx).
Scheduler claim throughput is **flat ~250–300 claims/s** from 1→25 workers.

Infrastructure (prior audit, MEASURED): Render Free CPU ≈ 0.05 core, RAM
≈ 197–217 MB, spin-down ~15 min.

**Confirmed vs revised (no history rewritten):**
- *Confirmed:* read paths are single-digit ms (`docs/PERFORMANCE.md`).
- *Confirmed:* OpenHands/LLM paths are provider-bound.
- *Revised:* the earlier framing "there is no NEXUS-side hotspot" is **too
  strong under concurrency**. At ≥10 concurrent operations the single
  connection + `RLock` is a real hotspot (p99 64 ms @10c → 706 ms @100c).
  This was not measured before; it is measured now.

## 2. Proven bottlenecks

| # | Bottleneck | Evidence | Affects |
|---|---|---|---|
| B1 | Single DB connection behind `RLock` | p99 706 ms @100c; flat tput | all concurrent DB work, API, scheduler |
| B2 | Thread-per-request HTTP + serialized DB | API p99 1264 ms @50c | API under load |
| B3 | Scheduler claim throughput flat | 250–300 claims/s at 1→25 workers | multi-run scheduling |
| B4 | Archive extract/inspect CPU cost | 216 ms/1000 members; 39 ms inspect/2000 | File Lab |
| B5 | Frontend: 198 KB unminified JS, fixed 1 s/5 s polling | measured asset + code | first load, request volume |
| B6 | Fixed 2 s OpenHands poll interval | `nexus_lab.py` poll_interval=2.0 | run latency (external-bound) |

## 3. Python optimization plan (no Rust required)

| # | Change | Target | Expected effect | Risk | Effort |
|---|---|---|---|---|---|
| P1 | Replace the single connection + `RLock` with a `psycopg_pool` connection pool | B1 | **p99 706→37 ms @100c (19×)**; tput 2.5k→4.7k | low (same SQL, same semantics; keep single-writer per domain) | low |
| P2 | Bound the request handler concurrency (thread pool) so the DB pool is not oversubscribed | B2 | API p99 under load | low | low |
| P3 | Batch/queue event writes; keep one INSERT per event but behind the pool | B1 | event write unchanged, contention removed | low | low |
| P4 | Move archive extract off the request thread (worker) + stream instead of holding bytes | B4 | request latency decoupled from 216 ms | medium | medium |
| P5 | Minify/split/gzip the studio assets; lazy-load highlight/markdown | B5 | first-load bytes ~4× smaller | low | low |
| P6 | Adaptive polling: backoff when idle, visibility-gated (partly present), event-driven where possible | B5/B6 | fewer requests, faster status | low | medium |
| P7 | Index review for the hot queries (list order, content search) | B1 | lower DB time under load | low | low |
| P8 | Cap and measure memory growth over a long run | UNKNOWN | avoid unbounded growth | low | medium |

**P1 is the single highest-value change and it is pure Python.** It must be
done first and re-benchmarked before any Rust decision on the DB layer.

## 4. Rust-first candidates

**None required today.** The measured structural bottleneck (B1) is solved by
P1 in Python. A component becomes RUST_FIRST only if, after P1–P8, it still
misses the threshold (§7) **and** Rust shows a real gain in a NEXUS scenario.

The closest candidate is the **Execution Engine** (B3 + long-term type safety),
but that is a **RUST_LATER** architectural choice, not a measured necessity:
after P1 the claim path scales with the pool.

## 5. Rust-later candidates

- **Archive extract/inspect (B4):** CPU-bound; Rust plausibly 2–5×, but the
  volume is low. Revisit if File Lab becomes a hot path.
- **Execution Engine / Scheduler:** async + typed state machines are
  attractive for correctness; migrate when the platform needs it, not for
  measured speed.
- **Full backend:** only after every domain passes parity and the thresholds
  below — and only if the non-performance benefits (types, single binary,
  Tauri stack) justify it.

## 6. Components to keep in Python

HTTP layer, event writes, dashboard counts, run creation, state transitions,
cleanup queueing, OpenHands/LLM/GitHub-bound paths (external-bound), startup/
migrations. All measured cheap or provider-bound.

## 7. Benchmark methodology (fixed)

- Same dataset, same PostgreSQL version, same infrastructure, same load, same
  functional result for every variant.
- Warm-up before measurement; ≥2 runs; report p50/p90/p95/p99 + throughput +
  error rate + CPU/RAM + duration.
- Record Benchmark ID, commit SHA, runtime version, config, iterations,
  concurrency.
- The instruments are committed: `benchmarks/db_concurrency_bench.py`,
  `benchmarks/api_bench.py`, `benchmarks/scheduler_bench.py`,
  `benchmarks/events_files_bench.py`, `benchmarks/rust-db/` (Rust prototype).
- **Acceptance thresholds (preliminary):** latency-sensitive ≥20% p95
  improvement; throughput-bound ≥30% throughput; memory-bound ≥20% RAM
  reduction. A migration below threshold needs a separate written justification.

## 8. Migration sequence (performance-driven)

1. **P1 (DB pool)** → re-benchmark A vs B. If p99 meets the threshold, **stop**:
   the DB layer stays Python.
2. P2–P8 (API concurrency, files, frontend).
3. Re-measure the end-to-end journeys; identify what still misses thresholds.
4. Only then consider RUST_FIRST for a specific component, with a Rust
   prototype measured on the same scenario.
5. Keep Python as the fallback ([API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) §5, one writer per domain).

## 9. Acceptance criteria

- P1: DB p99 @100c ≤ 50 ms (from 706 ms); no functional regression; the full
  test suite green.
- P2: API p99 @50c within 2× of the single-request latency.
- P3–P8: each measured before/after with the same instrument.
- No user-facing journey regresses.
- Every claim is a benchmark artefact, not an estimate.

## 10. Risk register (performance)

| ID | Risk | Prob | Impact | Mitigation |
|---|---|---|---|---|
| PF1 | Pool change breaks single-writer invariants | Low | High | keep per-domain single writer; the pool is transport, not ownership |
| PF2 | Pool oversubscription (pool=server max) | Medium | Medium | cap pool < `max_connections`; the conc=100 artifact proves it |
| PF3 | Minification breaks the studio (no build step today) | Medium | Medium | keep a no-build fallback; test in CI |
| PF4 | Longer poll interval delays status | Medium | Low | adaptive: fast while a run is active, slow when idle |
| PF5 | Benchmark conditions drift between runs | Medium | High | fixed methodology (§7); commit the artefacts |

## 11. Rollback

- P1–P8 are ordinary code changes behind the same API; revert the commit and
  redeploy. No schema change is required by any P-item (all are additive or
  in-process), so a rollback stays readable by the previous code.

## 12. End-to-end targets

| Journey | Today | Target | How |
|---|---|---|---|
| Concurrent reads (p99) | 706 ms @100c | ≤ 50 ms | P1, P2 |
| API under load (p99) | 1264 ms @50c | ≤ 100 ms | P1, P2 |
| Scheduler claim throughput | ~250–300/s | ≥ 500/s | P1 |
| First load (studio) | 198 KB JS | ≤ 60 KB (gzip) | P5 |
| File upload 2 MiB | 23.7 ms | unchanged (already fast) | — |
| Archive extract 1000 | 216 ms | ≤ 120 ms (or off-thread) | P4 |
| OpenHands/LLM journeys | provider-bound | **unchanged** (honest) | — |
