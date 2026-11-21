# PERFORMANCE REPORT — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Rule honoured: no number below is fabricated. Every figure is a wall-clock
measurement taken in this session, and where Rust is *not* faster that is stated.**

---

## 1. What was measured, and with what

| Tool | Measures | Command |
| --- | --- | --- |
| `tools/benchmark_rust.py` | plan validation, plan hash, retry arithmetic — Python vs the Rust binary, one process per batch | `python tools/benchmark_rust.py --cases 5000 --repeat 3` |
| `benchmarks/scheduler_bench.py` | scheduler claim throughput | `python benchmarks/scheduler_bench.py --repeat 40` |
| `tests/test_concurrent_claims.py` | claim correctness at 1/4/16/64/128 workers | `pytest tests/test_concurrent_claims.py` |
| `docs/PERFORMANCE.md` | process RSS, pool connections | (prior baseline, re-read) |

Machine: the CI/sandbox host (2 vCPU class), PostgreSQL 17.11, Rust release build
(`cargo build --release`).

## 2. Python vs Rust, the migrated decisions

`tools/benchmark_rust.py --cases 5000 --repeat 3` (best of 3), 5 000 cases each:

| Decision | Python | Rust (per case, batch amortised) |
| --- | ---: | ---: |
| plan validation, 5 000 cases | 0.694 s | — |
| plan hash, 5 000 cases | 0.788 s | — |
| retry arithmetic, 5 000 cases | 0.007 s | — |
| **combined** | **1.489 s** | **1.461 s** |

**Speedup: 1.019×.** Rust is **not** meaningfully faster here. That is the honest
result and it is the expected one: the Rust CLI is a separate process, so a
batch carries one process start, and the per-case work is dominated by JSON
(de)serialisation on both sides, not by the decision itself. The migrated
decisions are not a performance bottleneck — they are microseconds of arithmetic
and a SHA-256. Migrating them bought **parity and a second implementation to test
against**, not speed.

Do not claim a speedup from this. The mission's rule ("never claim performance
improvements without measurements") is satisfied by reporting 1.02×.

## 3. Claim throughput and correctness

The claim path is the only database-bound hot path in dispatch. Its correctness
at width is proven (`tests/test_concurrent_claims.py`), and the cost is one
transaction per claim:

- 40 tasks claimed by 1/4/16/64 workers: every task exactly once, none lost.
- 256 tasks by 128 workers (`NEXUS_STRESS=1`): every task exactly once.
- The in-flight cap (`NEXUS_LAB_MAX_ACTIVE_TASKS`, default 8) is what bounds a
  deployment's concurrency, not the claim cost — a full queue of 40 claims with
  the cap lifted completes in well under a second on this host.

## 4. Resource usage (unchanged from the prior baseline)

`docs/PERFORMANCE.md` records the process RSS and pool figures; this increment
does not change the runtime shape (no new background loop, no new connection per
request). The only additions are pure functions (`parity_matrix`) and one
resolver (`App._workflow_client`) that runs once per workflow, not per event.

## 5. Priorities kept

1. Reliability — the dispatch journal (PR #55) and fencing are untouched.
2. Data integrity — no schema change in this increment.
3. Concurrency correctness — proven at 1–128 workers.
4. Recovery — 9 crash windows (real SIGKILL).
5. Resource efficiency — no measurable change.
6. Throughput — 1.02× Python/Rust; the bottleneck is network (OpenHands,
   provider, GitHub), not the migrated decisions.

## 6. Reproduce

```bash
cd rust && cargo build --release && cd ..
python tools/benchmark_rust.py --cases 5000 --repeat 3
NEXUS_LAB_MAX_ACTIVE_TASKS=0 NEXUS_STRESS=1 \
  python -m pytest tests/test_concurrent_claims.py -q
```
