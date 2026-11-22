# NEXUS 3.0 — Phase 2 report (Rust Core)

Branch: `nexus/3.0-foundation` (isolated; `main` untouched).
Base: `main` @ `2edf3aa`. Phase 2 builds on `5a9989b` (Phase 0+1).
This report follows the 11-point format the authorization asks for.

**Status: complete, awaiting owner acceptance.** Nothing was merged, deployed,
or switched to authoritative; Python remains the implementation that runs.

## 1. What was checked

- The environment was reset between sessions; the toolchain (Rust 1.100,
  PostgreSQL 17.11), the `nexus` role and database were re-installed and the
  existing suite re-run before any change, so the baseline is the same as
  Phase 0 (2094 passed / 9 skipped at that commit).
- The Python lifecycle code was read from source (`_apply_run_state`,
  `_requeue_retryable`, `release_task`, `postpone_task`, `reclaim_expired`,
  `record_verification`, `run_progress`, `_decide`) and mirrored statement for
  statement, not from a summary.

## 2. What was implemented

Five Rust components, each parity-gated:

1. **Run lifecycle transitions** — `nexus-store::lifecycle::Store::set_run_state`
   mirrors `_apply_run_state`: the state table, the run-row lock first, the
   stop-signal insert, blocking unfinished tasks, closing executions, the
   per-state UPDATE, the goal mirror, the `run.cancelled` event, the blocked-run
   chat note + notification.
2. **Stop / cancellation** — part of (1): a cancel/fail records a stop for the
   conversation at work (`nexus_executor_stops`), blocks every unfinished task,
   hides the run; `_tell_chat` is mirrored.
3. **Lease reclaim & crash recovery** — `reclaim_expired_full` (run row first,
   unbound-only, durable-cycle reset), `release_task` and `postpone_task`
   (both fenced on `lease_token`), plus `holds_lease`.
4. **Worker loop** — `nexus-worker`: an `Executor` trait (the honest seam to
   OpenHands, which is Phase 3), a pure `decide()` mirroring `_decide`, and
   `Worker::tick()` (reclaim → claim → execute → settle → decide).
5. **Axum API layer** — `nexus-api`: `/healthz`, `/readyz`,
   `/api/v1/projects`, `/api/v1/projects/{id}`, `/api/v1/projects/{id}/runs`,
   returning the same JSON wrappers as the Python server.

Plus the supporting store reads (`list_projects`, `project_for_member`,
`list_runs_for_member`, `run_progress`, `active_run_ids`, `run_policy`,
`run_project`) and a `to_sqlx_url` helper shared by the CLI and the API.

## 3. Python↔Rust parity results

All green, on real PostgreSQL:

| Suite | Cases | Result |
|---|---|---|
| `tests/test_rust_parity.py` (domain: state machines, DAG, hash, retry, claim decision) | 9 | pass |
| `tests/test_rust_claim_parity.py` (claim transaction) | 5 | pass |
| `tests/test_rust_lifecycle_parity.py` (cancel, fail, illegal, resume, reclaim, fencing) | 7 | pass |
| `tests/test_rust_api_parity.py` (healthz, readyz, projects, project, runs) | 5 | pass |
| **Total** | **26** | **all pass** |

Each compares the **resulting database state** (not just a return value) over
identical starting states. The API parity test caught a real contract mismatch
— the Python server wraps responses (`{"projects": …}`, `{"project": …}`,
`{"runs": …}`) and the first Rust version returned bare arrays; fixed to match.

## 4. Full test results

- **Rust workspace** (`cargo test --workspace`, real PostgreSQL):
  - nexus-domain 14, nexus-core 15, nexus-store claim 5, nexus-store lifecycle 8,
    nexus-worker loop 4, nexus-worker concurrency/recovery 10 = **56 passed, 0 failed**.
- **Python parity suites:** 26 passed (above).
- **Full Python suite:** see §"Full suite" at the end of this file (run after
  the Rust work; counts below).

## 5. Concurrency and recovery results

`rust/crates/nexus-worker/tests/concurrency.rs` (10 cases, real PostgreSQL):

- **1, 2, 4, 8, 16 and 32 workers** claim together; every worker takes a
  **distinct** task — no duplicate claim, no lost task, at every level.
- **Cancellation race:** claim a few tasks, then cancel; no task is left
  unfinished and nothing is claimable afterwards.
- **Stale fencing token:** a reclaimed task's old worker cannot settle it; the
  verdict is a no-op and the new owner is unchanged.
- **Worker crash recovery:** a task whose lease expired with no conversation
  bound is reclaimed by a new worker and completed.
- **Simultaneous claim/stop/reclaim:** run a cancel and a reclaim concurrently;
  the end state is legal (run `cancelled`, no task unfinished) whichever order
  they take.

Each test uses its **own fresh database** (unique name, no leftover-drop), so a
test's `Worker::tick()` (which correctly scans all active runs) cannot claim a
sibling test's tasks — the earlier shared-DB interference was a test artifact,
fixed by isolation, not a code change.

## 6. Performance comparison — full task lifecycle (not just the claim)

Same workload (20 runs × 10 independent tasks, durable), same machine, same
PostgreSQL; one claim + one settle + the run decision per task, measured
in-process:

Release build (what a deployment runs), 20 runs x 10 tasks:

| | tasks/s | p50 | p95 | p99 | peak RSS |
|---|---|---|---|---|---|
| Python `LabStore` loop | 198.7 | 4.72 ms | 6.53 ms | 6.53 ms | **55.9 MB** |
| Rust `nexus-worker` loop (release) | **254.1** | **3.71 ms** | **5.05 ms** | **5.05 ms** | **4.2 MB** |

**Honest reading.** With the release build the Rust loop is **~1.28x the
throughput, ~1.27x lower p50 and p99**, and **~13x less memory** (4.2 MB vs
55.9 MB) for the same work. The lifecycle is still largely database-bound (the
absolute latencies are single-digit milliseconds in both), so the gain is
modest and reproducible rather than dramatic; the memory reduction is the
larger, structural win for the 4-core / 8 GB target. Earlier in this phase a
*debug* build measured ~170 tasks/s — the release profile is what is reported
here and what a deployment uses. Every number is from one machine, one
PostgreSQL, in-process, and is reproduced by the command below.

Reproduce:
```bash
NEXUS_DB_URL=postgresql://… cargo run -p nexus-worker --release \
  --example lifecycle_bench -- --runs 20 --tasks 10
```

## 7. Resource consumption

- Rust worker process peak RSS on the lifecycle benchmark (release): **4.2 MB**
  vs Python 55.9 MB — about **13x less**.
- The claim path (Phase 0) remains ~4.7 MB vs Python 43.1 MB.
- CPU: the release loop does ~1.28x the tasks per second at ~1.27x lower p50,
  so CPU per task is lower, not higher.

## 8. Remaining blockers

- **OpenHands execution is not migrated.** The `Executor` trait is the seam; a
  production executor (start conversation, poll, verify the commit on GitHub,
  account pool, failover, SelfQA) is Phase 3. The loop is real and tested with a
  deterministic executor; it does **not** claim to run OpenHands yet.
- **The Rust API serves read routes and health only.** Auth (session/CSRF and
  bearer) and the write surface are the Python server's until migrated route by
  route (Phase 4).
- **A two-live-account failover stays BLOCKED** (one key here); unchanged.
- **`to_sqlx_url` is a local adapter** for libpq `key=value` DSNs; SQLx only
  parses URLs. It is tested by every Rust DB test that uses the isolated DSN.

## 9. Files changed (this phase)

New crates:
- `rust/crates/nexus-store/src/lifecycle.rs` — lifecycle, reclaim, release,
  postpone, record_verification, run reads, `row_to_json`/`run_view_json`.
- `rust/crates/nexus-store/tests/lifecycle_parity.rs` — 8 real-PG tests.
- `rust/crates/nexus-worker/` — `Cargo.toml`, `src/lib.rs` (Executor, decide,
  Worker), `tests/worker.rs` (4), `tests/concurrency.rs` (10),
  `examples/lifecycle_bench.rs`.
- `rust/crates/nexus-api/` — `Cargo.toml`, `src/lib.rs` (Axum router),
  `src/main.rs` (binary).

Changed:
- `rust/Cargo.toml` — add `nexus-worker`, `nexus-api`.
- `rust/crates/nexus-store/src/lib.rs` — `to_sqlx_url`, `TaskRow`, wire lifecycle,
  `reclaim_expired` delegates to the faithful version.
- `rust/crates/nexus-cli/src/main.rs` — `run-state`, `reclaim`, `release`,
  `postpone` subcommands; reuse `nexus_store::to_sqlx_url`.
- `tests/test_rust_lifecycle_parity.py`, `tests/test_rust_api_parity.py` — new.
- `.github/workflows/tests.yml` — the parity step runs the new suites.

No Python module was deleted or changed; no migration was added or edited.

## 10. Recommendation for the next phase

- **Phase 3 (Integrations):** implement the production `Executor` against the
  real OpenHands Cloud (both transports, account pool, failover, SelfQA, GitHub
  verification, evidence, reports) behind the trait already in place, parity-
  tested against the Python executor over the same transport fakes
  (`tests/fake_cloud.py`, `tests/fake_runtime.py`).
- **Phase 4 (API):** migrate the auth layer and the write routes, then put the
  unchanged studio in front of the Rust API and run the visual-regression
  baseline.
- Owner authorisation is required before any merge, deploy, or switch of an
  authoritative implementation from Python to Rust.

## Full suite (run after the Rust work)

**2111 passed, 9 skipped, 0 failed** in 548 s (the whole suite including browser
E2E). The baseline was 2094 passed / 9 skipped; the +17 are the new parity test
files. No existing test was removed or weakened. Lint: `ruff check .` clean.
