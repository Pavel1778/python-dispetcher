# NEXUS — Rust Migration Assessment (restored 2026-11-16)

Phase C. **Zero components migrate into the NEXUS runtime.** (Restored and
updated to the consolidated base.)

## What exists in this tree (not authored here)

- `benchmarks/rust-archive/` — isolated research benchmark of the CPU-bound
  part of archive handling (“NOT the NEXUS runtime and shares nothing with
  it”, per its own header). Its verdict criterion: is a Rust unpacker
  materially faster than ~15 ms / 1000 members Python. It does **not** carry
  the safety validation (that is a parity concern).
- `benchmarks/rust-db/` — a DB-path benchmark crate, likewise isolated.
- `benchmarks/*.py` — API/DB-concurrency/E2E/events-files/scheduler/soak
  benchmarks; `benchmarks/soak_test.py` present.
- These are **cargo projects with lockfiles**, not runtime code; they are not
  built or run by this branch, and no claim about their output is made.

## Component map (consolidated tree)

| Component | Lang | Why | Verdict |
| --- | --- | --- | --- |
| Archive validation/extraction | Python + cap controls | correctness-critical; already streaming, capped, tested; Rust pilot measured (~15 ms Python) | **NO-GO** without a measured p95 problem or a fuzz win |
| File hashing (`sha256_file`) | Python / hashlib (OpenSSL) | I/O-bound | NO-GO |
| Terminal output (≤30 kB stored) | Python | trivial bytes | NO-GO |
| Scheduler hot path (`claim_task`) | SQL + thin Python | the DB is the work | NO-GO |
| CLI/diagnostics | Python | interactive startup cost only | DEFER — only if one-binary distribution is demanded |
| Report/render paths | Python | small payloads | NO-GO |

## Honest constraints

- `cargo`/`rustc` absent in this sandbox; toolchain cannot be installed
  (network allowlist). **No Rust benchmarks were produced here.** The
  consolidated `benchmarks/rust-*` crates exist for that; their results
  belong to whoever ran them.
- No p50/p95/p99 Rust-vs-Python comparison is invented for this document.
- PyO3/maturin = new build chain for 0 measured ms; IPC sidecar = new failure
  domain to secure for the same reason.

## Re-justifying criteria (pre-registered)

1. Measured p95 > 50 ms on `open_zip` on real uploads, or a fuzz campaign
   showing a structural gap Python cannot close cheaply.
2. One-binary CLI distribution requirement.
3. A controlled prototype showing > 5× end-to-end on a named hot path, with
   identical inputs/machine, reported with p50/p95/p99 + peak RSS.
