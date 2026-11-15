# NEXUS — Archive Engine Benchmark

> **STATUS: MEASURED.** The archive/file pipeline before and after the Python
> optimization, and the comparison against a Rust prototype (Phase 4 + Phase 5).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md),
[PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md).
Rust pilot: [RUST_ARCHIVE_ENGINE_PILOT](../architecture/RUST_ARCHIVE_ENGINE_PILOT.md).

---

## 1. Where the time goes (MEASURED)

Extracting a 1000-member zip (Phase 4 baseline):

| Step | p50 |
|---|---:|
| unpack + validate (`open_archive`) | **15.5 ms** |
| 1000 individual INSERTs | **150.3 ms** |
| 1000 INSERTs via `executemany` | **54.8 ms** |
| whole extraction (A) | 165 ms |

The CPU part (unpack) is ~9 % of the operation; the rest is **database round
trips** — one per member. This is the key fact that decides the component: the
bottleneck is not CPU.

## 2. Python optimization (MEASURED, commit `2a34a06`)

`file_lab.extract_into_workspace` now writes every member with a single
`executemany` in the same transaction.

| Members | Before | After | Speedup |
|---|---:|---:|---:|
| 100 | 18.3 ms | **9.8 ms** | 1.9× |
| 1000 | 165.0 ms | **77.8 ms** | 2.1× |

## 3. File upload and inspection (MEASURED, unchanged)

| Operation | p50 |
|---|---:|
| upload 4 KiB / 64 KiB / 512 KiB / 2 MiB | 1.3 / 1.8 / 8.1 / 23.7 ms |
| inspect 100 / 1000 / 2000 members | 2.3 / 20.9 / 39.3 ms |

Uploads are I/O-bound (sha256 is already C); no change made.

## 4. Rust comparison (MEASURED)

`benchmarks/rust-archive` (isolated prototype) unpacks the same zips:

| Archive | Python unpack | Rust unpack | Speedup |
|---|---:|---:|---:|
| 100 members | 1.52 ms | 0.46 ms | 3.3× |
| 1000 members | 15.70 ms | 4.67 ms | 3.4× |
| 1000 × 4 KiB | 24.43 ms | 5.31 ms | 4.6× |

**Verdict:** the Rust unpack is 3.3–4.6× faster as a function, but the unpack is
a small part of the operation, so the end-to-end gain is ~13 % at most — below
the framework's ≥ 20 % p95 threshold. **The component stays Python**
([RUST_ARCHIVE_ENGINE_PILOT](../architecture/RUST_ARCHIVE_ENGINE_PILOT.md) §4).

## 5. Safety (unchanged, VERIFIED)

The archive limits and refusals are untouched: entry count (2000), total
uncompressed (256 MiB), depth (16), compression ratio, symlink/hardlink refusal,
absolute-path refusal, running read cap. The Phase 4 change is only how the
validated members are written to the database, after validation. The archive
safety tests pass unchanged.
