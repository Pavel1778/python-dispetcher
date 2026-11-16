# NEXUS — Scheduler Performance

> **STATUS: MEASURED.** Task-claim throughput and what limits it (Phase 3 of the
> performance program).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md),
[PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md).
Related: [EXECUTION_ENGINE_SPEC](../architecture/EXECUTION_ENGINE_SPEC.md).

---

## 1. Baseline (MEASURED)

An earlier audit saw claim throughput flat at ~250–300 claims/s from 1 to 25
workers. After the connection pool (Phase 1) the same instrument
(`benchmarks/scheduler_bench.py`) gives ~326–364 claims/s — the pool removed the
connection serialization, but the throughput is still **flat** in the number of
workers.

| Workers | Claims/s (before pool) | Claims/s (after pool) |
|---:|---:|---:|
| 1 | ~284 | 364 |
| 5 | ~272 | 354 |
| 10 | ~245 | 349 |
| 25 | ~305 | 327 |

## 2. Why it is flat on one run (VERIFIED)

`claim_task` (`orchestration/nexus_lab_store.py`) takes the **run row**
`FOR UPDATE` at the start of the transaction. The reason is a real invariant:
each claim consumes one **iteration** of the run (`iteration = iteration + 1`,
checked against `max_iterations`), and that increment must be atomic against
every other claim of the same run — otherwise two workers could exceed the
run's iteration budget. Locking the run row makes the budget exact, and it also
makes a concurrent pause/cancel that committed first always win.

So two workers on the **same** run are serialized by design. This is not a
defect to remove; removing it would weaken a guarantee the domain states.

## 3. Claims scale across runs (MEASURED)

The right axis is the number of **runs**, not workers on one run. Measured:

| Case | Claims/s |
|---|---:|
| 1 run, 1 worker | 380 |
| 1 run, 10 workers | 345 |
| **10 runs, 10 workers (one each)** | **1044** |

Ten independent runs claim in parallel at ~1044 claims/s — the scheduler scales
with the work in flight, which is what the deployment has (many runs across
projects). A single run is intentionally one-at-a-time.

## 4. Per-operation costs (MEASURED, unchanged by Phase 1–2)

| Operation | p50 | p99 |
|---|---:|---:|
| `start_run` (40-task plan) | 16.6 ms | 21.2 ms |
| `claim_task` | 2.3 ms | 3.4 ms |
| `reclaim_expired` scan | 0.7 ms | 0.8 ms |
| `queue_cleanups` | 4.2 ms | 7.9 ms |

These are small; the scheduler is not a bottleneck for the deployment's load.

## 5. Guarantees kept (VERIFIED)

None of the following was changed:

- one active run per goal; `iteration` budget exact;
- lease ownership, lease expiry, safe reclaim (`reclaim_expired`);
- duplicate prevention (`FOR UPDATE SKIP LOCKED` one row at a time);
- branch exclusivity (`_BRANCH_FREE`) and the per-branch advisory lock;
- cancellation and pause/cancel-wins ordering;
- project isolation; crash recovery.

## 6. What was considered and NOT done (INFERRED)

- **Narrowing the run-row lock:** rejected — the `iteration` increment and the
  active-task-limit check both need the row stable within the transaction;
  splitting them would either exceed the budget or re-introduce the race the
  concurrency test caught.
- **Batching claims:** rejected — the claim returns the task the worker will
  dispatch; batching would hold several task rows' locks longer for no measured
  gain.
- **Schema/index change:** not needed — `claim_task` is 2.3 ms and the run-row
  lock, not an index, is the limit.

## 7. Conclusion

The scheduler is **not** a bottleneck for the deployment's load: it scales with
the number of runs (~1044 claims/s across 10 runs) and its per-operation costs
are single-digit milliseconds. The flat per-run throughput is a deliberate
guarantee, documented in [EXECUTION_ENGINE_SPEC](../architecture/EXECUTION_ENGINE_SPEC.md),
and left as it is.
