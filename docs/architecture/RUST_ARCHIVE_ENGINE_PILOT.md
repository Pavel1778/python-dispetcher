# NEXUS — Rust Archive Engine Pilot

> **STATUS: MEASURED PILOT — NOT ENABLED.** An isolated experiment to decide
> whether a Rust archive component is worth adopting. The measurement says it is
> not, today. No Rust code is wired into the runtime; the Python path is
> unchanged and remains the only one.
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `a7442632bb05492f65f72b1bf2e3ec94436625d5`
- **Parent:** [PERFORMANCE_RESULTS](../performance/PERFORMANCE_RESULTS.md),
  [ARCHIVE_ENGINE_BENCHMARK](../performance/ARCHIVE_ENGINE_BENCHMARK.md)
- **Prototype:** `benchmarks/rust-archive/` (isolated; `target/` gitignored)

---

## 1. Question

The archive pipeline (`orchestration/archive_safety.py`,
`orchestration/file_lab.py`) is CPU-bound in principle (zip/tar parse and
decompress). Is a Rust unpacker materially faster than the **optimized** Python
one, in a way that improves a real NEXUS user scenario?

## 2. Method (MEASURED)

- The same zips are generated for both implementations (100 / 1000 / 5000
  members; small and larger members).
- **Variant A (Python, before optimization):** `archive_safety.open_archive`
  plus a per-member INSERT loop.
- **Variant B (Python, optimized):** `open_archive` plus `executemany` (the
  Phase 4 change, commit `2a34a06`).
- **Variant C (Rust):** `benchmarks/rust-archive` decompresses every member with
  the same size cap and does the same CPU work a PyO3 extension would do. It
  does not do the safety validation (a parity concern) and does not touch
  PostgreSQL.
- Same machine, PostgreSQL 17.11, warm-up, 20 repeats, p50.

## 3. Results (MEASURED)

**Unpack only (the CPU part), p50 ms:**

| Archive | Python `open_archive` | Rust `zip` unpack | Rust speedup |
|---|---:|---:|---:|
| 100 members | 1.52 | 0.46 | 3.3× |
| 1000 members | 15.70 | 4.67 | 3.4× |
| 1000 members × 4 KiB | 24.43 | 5.31 | 4.6× |

**Whole extraction (the user operation), p50 ms:**

| Members | A: Python + per-member INSERT | B: Python + executemany | C: hypothetical (Rust unpack + B's insert) |
|---|---:|---:|---:|
| 100 | 18.3 | **9.8** | ~9.1 |
| 1000 | 165.0 | **77.8** | ~67 |

## 4. Decision (INFERRED, per the framework)

The Rust unpack is genuinely 3.3–4.6× faster **as a function**, but:

- The unpack is only ~15 ms of a ~78 ms extraction; the rest is database work.
- Replacing the unpack with Rust would save ~11 ms on a ~78 ms operation
  (~13 %), and far less on the user-visible path (which includes HTTP, DB and
  the surrounding work).
- The framework's latency threshold is **≥ 20 % p95 on the user scenario**; this
  does not reach it.
- **The dominant cost was database round trips, and Python `executemany` removed
  most of it (2.1×).** A Rust unpacker would not have moved that.

**Decision: PYTHON_KEEP for the archive pipeline.** The pilot proves the Rust
mechanism is feasible and faster on the isolated function, but it does not
justify a second implementation, a wheel per platform, a parity/fuzz suite, and
the maintenance that come with it — for a component whose whole operation is
tens of milliseconds.

## 5. What would change the decision

- A workload of **large** archives (hundreds of MB) or many concurrent
  extractions where the CPU part dominates and the absolute saving is
  user-visible.
- A measurement showing the extraction on the user path above the threshold.

Until then the pipeline stays Python.

## 6. Safety parity (design, for a future port)

If a port is ever done, it must keep every guarantee of
`archive_safety.py`: path traversal, symlink and hardlink refusal, absolute
paths, entry count / total size / depth / compression-ratio limits, a running
read cap (`file_size` is attacker-controlled), Unicode path normalization, and
a shared fixture set with property-based and fuzz tests for both
implementations. None of this is built, because the port is not taken.

## 7. Status

**NOT ENABLED.** No Rust code is in the runtime; no feature flag exists; the
Python implementation is the only one and is unchanged. The prototype lives
under `benchmarks/rust-archive/` as an experiment only.
