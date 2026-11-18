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

## Re-validation (2026-11-18)

Requested again ("write everything you can and need in Rust"). The finding
stands, re-measured from the sandbox, and the one real hot-path fix that fell
out of the investigation shipped as migration `0041` (see
`docs/PERFORMANCE.md` — the claim path went from an 11.5 ms sequential scan
to 0.008 ms).

Fresh toolchain evidence, 2026-11-18, this sandbox:

- `rustc` / `cargo`: **not installed** (`command not found`);
- crates.io: **unreachable** (TLS EOF on `https://crates.io/api/...` and on
  `https://static.crates.io/...` — egress allowlist is github / pypi / npm
  only);
- `apt-get install rustc cargo`: **refused** — `Could not open lock file
  /var/lib/dpkg/lock-frontend (13: Permission denied)` (not root);
- `gcc`/`g++` are present, but a C or Rust hot path would still have nothing
  crates.io-grade to link against here, and CPython's own hot primitives
  (`hashlib`, `bytes.find`, the C `json` accelerator) are already C.

Zero-dependency pure-std Rust is strictly *not* faster than `hashlib`
(OpenSSL) for the hash/chain work and cannot verify Ed25519 at all, so a
std-only crate would add build and review surface without moving a single
measured number. The pre-registered re-justify criteria above stand
unchanged: a py-spy/pg_stat_statements p95 problem on the planner or the
canonical evidence encoder in a live workload, then a measured Rust pilot.
Nothing in tonight's profiling shows that; the bottleneck was SQL, and it is
fixed.

No "optional accelerator" was wired into the runtime. A component that
cannot be built and exercised in the environment it ships from is not an
optimisation; it is an unverified claim. If the owner wants the Rust path
exercised for real, it needs a build host with crates.io access (CI already
has Rust) — say so and it will be done there, behind the existing
`orchestration/` seam, with the same test corpus.
