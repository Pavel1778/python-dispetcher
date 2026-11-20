# PERFORMANCE REPORT — Python vs Rust claim transaction

**Date:** 2026-11-20 · **Verdict: the brief's headline numbers are NOT REPRODUCIBLE**

---

## 1. What was claimed, and what was measured

The brief states:

> Python: 194.2 tasks/s, 55.7 MB RSS. Rust: 170.3 tasks/s, 7.3 MB RSS.

**Those figures have no source in this repository.** A search for a harness that
reports "tasks/s" finds nothing:

```bash
grep -rn "tasks/s\|tasks_per_second" benchmarks/ tools/    # no matches
```

The only claim-comparison harness that exists is
`benchmarks/compare_claims.py`, and it reports **`grants_s`** (committed
`claim_task` grants per second), not `tasks/s`. Its own output labels the
measurement:

> `"dataset": "same cloned UUIDs; independent tasks, empty repo, no global active cap; one warm connection, statement caches off; SQL transaction only."`

So the brief's numbers cannot be confirmed **or** refuted from this repository —
they were produced by something not present here. That is recorded as
**NOT VERIFIED (source absent)**, not as agreement.

---

## 2. The measurement that *was* run

Method: `benchmarks/compare_claims.py` through
`tools/with_disposable_postgres.sh`, which creates a scratch PostgreSQL cluster
with `env -i` (no `.pgpass`, no inherited DSN, socket in a `0700` directory, no
TCP port). Postgres 17.11. Each round runs both drivers against clones of the
same fixture. Median of 4 rounds.

### Workload 200 tasks

| Driver | grants/s (median) | p50 | peak RSS (driver) |
| --- | ---: | ---: | ---: |
| Python | 449.9 | 2.25 ms | 38.6 MiB |
| Rust | **481.4** | 2.01 ms | **3.1 MiB** |

### Workload 500 tasks

| Driver | grants/s (median) | p50 | peak RSS (driver) |
| --- | ---: | ---: | ---: |
| Python | 458.6 | 2.09 ms | 38.6 MiB |
| Rust | 459.8 | 2.13 ms | **3.1 MiB** |

### Earlier run, 400 tasks × 4 rounds (same method)

| Driver | grants/s | p50 | p95 | p99 | RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Python | 465.85 | 2.05 ms | 2.51 ms | 2.82 ms | 39.6 MiB |
| Rust | 465.35 | 2.13 ms | 2.51 ms | 3.07 ms | 3.0 MiB |

---

## 3. Conclusion

**Throughput is a statistical tie.** Across three workloads and twelve rounds
the two drivers are within run-to-run noise of each other (450–481 grants/s,
p50 2.0–2.3 ms). Rust was ahead at 200 tasks and level at 500; neither result is
a repeatable win. **There is no throughput case for switching the scheduler.**

**Memory is a real difference.** The Rust driver uses **~3 MiB** against
**~39 MiB** for Python — about **12×** lighter. This is one transaction's driver
process (startup + teardown included, as GNU `time` measures the whole fresh
process), **not** the resident size of a worker loop, and **not** a server
profile.

### What is not measured

| Metric the brief asks for | Status |
| --- | --- |
| Pure domain computation | **NOT MEASURED** (no harness; `nexus-domain` is pure and could be micro-benchmarked) |
| Complete worker lifecycle | **BLOCKED** — no Rust worker exists |
| Concurrent worker throughput | measured only for *claims* (Phase 4 test), not a worker |
| API latency | **BLOCKED** — no Rust API exists |
| Startup time | included in the driver RSS/CPU figures, not separated |
| Recovery time | **BLOCKED** for Rust; measured for Python in the crash-window tests |
| DB query counts | **9 statements per claim+bind cycle** (4 claim, 5 bind), Python only |

### CPU (from GNU `time`, user seconds, whole driver process)

| Driver | user CPU |
| --- | ---: |
| Python | 0.32 s |
| Rust | 0.03 s |

Rust's driver spends ~10× less user CPU. This is consistent with the RSS
difference and, like it, is a property of the driver process, not of the system
under load.

---

## 4. Reproduce

```bash
cd rust && cargo build --locked --release --example claim_probe && cd ..
NEXUS_CLAIM_PROBE="$PWD/rust/target/release/examples/claim_probe" \
PATH="/usr/lib/postgresql/17/bin:$PATH" \
bash tools/with_disposable_postgres.sh \
  python benchmarks/compare_claims.py --tasks 200 --rounds 4 --out /tmp/c.json
```

`compare_claims.py` refuses any database it did not create itself, so a run
against somebody's real data is impossible by construction.
