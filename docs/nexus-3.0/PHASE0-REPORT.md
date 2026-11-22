# NEXUS 3.0 — Phase 0 report

Branch: `nexus/3.0-foundation` (isolated; `main` untouched).
Base: `main` @ `2edf3aa` (merge of PR #53).

This report follows the required 11-point format. Nothing here is carried over
from an earlier report without a fresh measurement; the exact commands and
numbers are in `PHASE0-AUDIT.md` and `PHASE0-BASELINE.md`.

## 1. What was checked

- **Repository state:** `main` HEAD `2edf3aa`; branches on `origin` (`main`, four
  `arena/*`, `feat/run-restart-duplicate`, `release/acceptance`,
  `release/nexus-2.0-pr-ready`); tags `v2.0.0` + four `preservation/*`; working
  copy clean at start. (`git log`, `git branch -r`, `git tag`, `git status`.)
- **Scale:** 68 Python modules / 42,797 lines; 136 test modules; 41 migrations
  (`0001`–`0042`, gaps `0012`/`0013` reserved) over **50 tables**; studio 5,549
  lines; existing Rust workspace 3 crates / 1,763 lines (pure domain only).
- **Toolchain:** Python 3.13.15, PostgreSQL 17.11 (live, loopback), Node 24.22.1,
  ruff 0.13.1, pytest 9.0.3, Playwright + Chromium present. **Rust was absent**
  and was installed (1.100.0); `crates.io` reachability was proven with a scratch
  crate before relying on it.
- **Backend:** stdlib `ThreadingHTTPServer`; `PostgresStore` over `psycopg_pool`;
  `LabStore` claim via `FOR UPDATE SKIP LOCKED` + advisory branch lock + fenced
  `lease_token`; `LabScheduler`; two OpenHands transports + account pool/router;
  MCP stdio + Streamable HTTP; a thin HTTP CLI.
- **API contract:** the `/api` surface in `docs/API.md` and `web_server.py`, with
  `tests/test_route_auth.py` as the guard that every protected route needs auth.
- **Studio identity:** chat-first shell, strict CSP, content-hashed assets; the
  structural/contrast/packaging guards in `tests/test_studio_ui.py`, the hostile
  corpus in `tests/studio_render_test.js`, and the Chromium journeys.
- **Packaging/CI:** DEB/APK/EXE are WebView/launcher shells (not yet Tauri 2);
  one CI workflow (`tests.yml`) with a `rust` job and a `test` job; **no
  per-platform package build job** — a Phase 5 gap, recorded, not hidden.

## 2. What was implemented (Phase 0 deliverables)

Phase 0 is audit + a proven baseline, but it also produced the first real slice
of the Rust-first target, so the migration does not start from a claim:

- **`rust/crates/nexus-store`** — the NEXUS claim transaction in Rust (SQLx,
  Tokio), a faithful port of `LabStore.claim_task`: the run gate on the locked
  row, the global backpressure cap under an advisory lock, candidate selection
  with `_TASK_DUE`/`_DEPS_VERIFIED`/`_BRANCH_FREE` one row at a time
  (`FOR UPDATE SKIP LOCKED`), the per-branch advisory lock and re-check, and the
  fenced take. Plus `mark_running`, `holds_lease`, `reclaim_expired`.
- **`nexus-rs claim` / `nexus-rs claim-bench`** — the CLI can perform a real
  claim against a database and measure in-process claim latency, which is what
  the parity test and the benchmark drive.
- **`rust/crates/nexus-store/tests/claim_parity.rs`** — 5 real-PostgreSQL tests,
  including **10 concurrent workers taking 10 distinct tasks**.
- **`tests/test_rust_claim_parity.py`** — Python and Rust agree on the same
  database: same first task, same row shape, same busy-branch refusal, and
  **5 Python + 5 Rust workers taking 10 distinct tasks**.
- **`tools/capture_studio_baseline.py`** — reproducible screenshot baseline of
  the studio (login, empty, phone, light).
- **Docs:** `PHASE0-AUDIT.md`, `PHASE0-BASELINE.md`, `PHASE0-RECOVERY-PLAN.md`,
  this report, and `baseline/*.png`.

## 3. Files changed

| File | Change |
|---|---|
| `rust/Cargo.toml` | add `crates/nexus-store` to the workspace |
| `rust/crates/nexus-store/Cargo.toml` | new crate (sqlx, tokio, uuid, chrono) |
| `rust/crates/nexus-store/src/lib.rs` | the claim transaction + fences |
| `rust/crates/nexus-store/tests/claim_parity.rs` | 5 real-PG tests |
| `rust/crates/nexus-cli/Cargo.toml` | depend on `nexus-store`, `tokio`, `uuid`, `sqlx` |
| `rust/crates/nexus-cli/src/main.rs` | `claim`, `claim-bench`, DSN→URL helper |
| `tests/test_rust_claim_parity.py` | Python↔Rust claim parity (4 tests) |
| `tools/capture_studio_baseline.py` | screenshot baseline tool |
| `docs/nexus-3.0/*` | audit, baseline, recovery plan, this report, screenshots |

`main` is not touched; no existing table, column, test or route is changed.

## 4. Architecture decisions

- **Incremental, parity-gated migration, not a big-bang rewrite** — exactly what
  the requirement's Phase 0–7 and "one authoritative implementation" demand. The
  Python backend stays authoritative; the Rust claim is a *verified candidate*
  until it is switched deliberately, and the parity test is what keeps the two
  honest in the meantime.
- **The claim is the first I/O component to move, because it is the worker's
  hot path and the one with the hardest invariants** (atomicity, fencing,
  branch exclusion). Moving a pure function first would prove less.
- **Latency parity is reported honestly.** The claim is DB-bound; Rust and
  Python are the same speed. The measured win is **memory (~9× less)** and a
  tighter p99 — which is what matters on a 4-core/8 GB box. No end-to-end
  speedup is claimed, because none was measured.

## 5. Tests run (exact)

```text
pytest tests/ -q  → 2094 baseline; 2098 with new parity tests, 9 skipped, 0 failed
ruff check .                            → All checks passed
cargo test (workspace, existing)        → 29 passed
cargo test -p nexus-store               → 5 passed  (real PostgreSQL)
pytest tests/test_rust_claim_parity.py  → 4 passed  (Python↔Rust, real PG)
pytest tests/test_backup_restore.py \
       tests/test_restore_point.py      → 15 passed
```

## 6. Exact results

- Baseline suite: **2094 passed, 9 skipped, 0 failed.** The 9 skips are
  environmental (Android SDK, mingw-w64, pooler DSN, live-OpenHands section) —
  reported as skips, never passes.
- Rust-side: **38 cases** pass (29 existing + 5 store + 4 parity).
- Lint clean; backup/restore drill green; restore point created and `VERIFIED`.

## 7. Performance baseline and results

Full table in `PHASE0-BASELINE.md`. Headline:

- Read API paths p50 **0.6–3.2 ms**, p99 **< 5 ms**. `chat_turn` 12.7 ms p50,
  `start_run` 31.1 ms p50 (network-bound).
- Claim path, in-process: Python p50 1.22 ms / p99 4.31 ms; Rust p50 1.39 ms /
  p99 **2.31 ms**. Same speed (DB-bound), tighter tail in Rust.

## 8. RAM / CPU change

- Claim workload peak RSS: **Python 43.1 MB vs Rust 4.7 MB (~9× less).**
- No CPU regression: latency parity on the same workload.
- This is the operating-budget change that matters for the 8 GB target; it is
  measured, not projected.

## 9. Problems found

- The NEXUS studio's WebView packaging (DEB/APK/EXE) is **not Tauri 2** — a
  Phase 5 gap; not a Phase 0 blocker but it must not be reported as done.
- CI has **no per-platform package build job**.
- `restore-point verify` needs the bundle **path** (or the `.json`), not the
  bare id — the doc string suggested an id; the working invocation is recorded in
  `PHASE0-RECOVERY-PLAN.md`.
- (Environment) leftover `nexus_rs_test_*` databases from an early test harness
  polluted the shared DB; fixed by giving the Rust tests their own database. No
  product defect.

## 10. Residual risks

- The Rust claim is a **candidate**, not authoritative; a divergence would be
  caught by the parity test but the Python path is what runs in production.
- Live OpenHands still has no GitHub provider token here, so a live task that
  must push ends `completed_unverified` (correct, not a defect) — any "verified
  end to end" claim about pushing stays BLOCKED.
- Android/Windows packages cannot be built in this sandbox (no SDK, no
  mingw-w64); those stay NOT VERIFIED until a runner with the toolchains exists.

## 11. What the next phase needs

- **Phase 1 (design):** the Rust workspace boundary map, the API compatibility
  contract as a machine-checkable list, and ADRs — building on the parity gate
  already in place.
- **Phase 2 (core):** migrate the run-lifecycle transitions next, behind the
  same parity gate, then the reclaim/stop paths.
- Owner authorisation is required before any merge, production deploy, or switch
  of an authoritative implementation from Python to Rust.
