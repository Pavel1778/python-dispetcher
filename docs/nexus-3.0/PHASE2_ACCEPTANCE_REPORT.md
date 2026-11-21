# PHASE 2 ACCEPTANCE REPORT — independent audit

**Date:** 2026-11-20 · **Verdict: BLOCKED (candidate absent) / ACCEPTED WITH CONDITIONS (what exists)**

Read `FORENSIC_BASELINE.md` first. The Phase 2 candidate it describes
(`nexus/3.0-foundation` @ `9c11f52`) **is not in this repository**, so the Phase 2
acceptance audit could not be performed against it. This report therefore:

1. states exactly what could not be audited and why, and
2. audits what *does* exist — the Python orchestrator and the four Rust crates —
   independently, with the evidence for each finding.

Nothing below is inferred from the previous report. Where the previous report and
the code disagree, the code is quoted.

---

## 1. Components the brief claims, and whether they exist

| Claimed component | Exists? | Evidence |
| --- | --- | --- |
| `nexus-domain` | **yes** | `rust/crates/nexus-domain` (1 025 lines) |
| `nexus-core` | **yes** | `rust/crates/nexus-core` (387 lines) |
| `nexus-cli` | **yes** | `rust/crates/nexus-cli` (354 lines) |
| `nexus-store` | **NO** | 0 files on all 11 remote branches |
| `nexus-worker` | **NO** | 0 files on all 11 remote branches |
| `nexus-api` | **NO** | 0 files on all 11 remote branches |
| Rust `set_run_state` / `_apply_run_state` | **NO** | only in Python (`orchestration/nexus_lab_store.py`) |
| Rust `reclaim_expired_full` | **NO** | only `reclaim_expired` in `nexus-storage` |
| Rust `release_task` / `postpone_task` / `holds_lease` | **NO** | only in Python |
| Rust `decide()` equivalent to `_decide` | **NO** | `nexus-core` has the claim *decision*, not `_decide` |

Mechanical check used:

```bash
for b in $(git branch -r | grep -v HEAD); do
  git ls-tree -r --name-only "$b" | grep -cE 'rust/crates/(nexus-worker|nexus-api|nexus-store)/'
done
# every branch: 0
```

**Consequence:** audit items 3.1–3.5 of the brief that name Rust modules have
nothing to inspect. They are recorded **BLOCKED — component absent**, not passed.

---

## 2. What was audited instead, and the findings

### 2.1 Run lifecycle (Python — the real implementation)

`_RUN_ENTERED_FROM` (`orchestration/nexus_lab_store.py:159`) defines the legal
transitions. `_apply_run_state` (`:1001`) applies them under a `FOR UPDATE` lock
on the run row.

**Verified:**
- `cancelled` is reachable from `running`, `paused`, `waiting_for_approval`,
  `blocked`; `completed`/`failed` only from `running`. A cancelled run cannot be
  resumed (no entry to `running` from `cancelled`).
- Cancelling inserts stop signals, blocks unfinished tasks, closes open
  executions **and** updates the run row — in **one transaction**, so a cancel
  cannot partially apply.
- Repeating a terminal transition is a no-op (`if current == state: return`), not
  an error.

**Lock ordering — verified programmatically, not by reading comments.** A script
walked every function in `nexus_lab_store.py`, recorded the first lock taken on
`nexus_runs` vs `nexus_run_tasks`, and reported any function that takes the task
lock first:

```
start_run / set_hidden / retry_failed / restart_run / duplicate_run / delete_run
_apply_run_state / wait_task / postpone_task          -> runs only
mark_running / retry_task / switch_task_executor      -> tasks only
claim_task / reclaim_expired                          -> other, runs, tasks
INVERSIONS (task locked before run): none
```

That is the claim the code comment makes ("one order for everyone"), and it
holds across all 14 locking functions.

### 2.2 Concurrency (new evidence — `tests/test_concurrent_claims.py`)

The brief's Phase 4 requires 1–64 workers (and optionally 128). No such test
existed. One was written and run against real PostgreSQL 17:

| Width | Distinct grants | Duplicate lease | Lost task |
| ---: | --- | --- | --- |
| 1, 2, 4, 8, 16, 32, 64 | none repeated | none | none |
| 128 (opt-in) | 256 tasks, each once | none | none |

**Result: 12 passed, 1 skipped (the 128 case, opt-in), then 128 passes when
enabled.** Two failures during development were **the test's fault, not the
product's**, and are recorded because they are instructive:

1. Widths ≥ 16 granted only 8 tasks. Cause: `max_active_tasks()` (default 8)
   refused further claims. That cap is deliberate — it is what stops one
   deployment flooding OpenHands — so the test now disables it
   (`NEXUS_LAB_MAX_ACTIVE_TASKS=0`) and measures claim correctness alone.
2. Widths 32/64 granted 50 of 64. Cause: `max_iterations` defaults to **50**, so
   the run exhausted its iteration budget at exactly 50 claims. Correct
   behaviour; the test now gives each run `max_iterations=count`.

### 2.3 Security (Python boundaries)

See `SECURITY_AUDIT.md`. Summary: every HTTP handler calls `_require_read` or
`_require_write` before serving data; the only pre-gate routes are `/login`,
`/login/state`, `/healthz`, `/readyz`, `/logout`, and each returns a boolean or a
status line and nothing else. SQL injection was **actively probed** through the
API's free-text fields and **refuted** (`tests/test_sql_injection.py`).

### 2.4 Rust code quality (real defects found and fixed)

Running the checks the brief names found two genuine defects on `main`:

| Check | Before | After |
| --- | --- | --- |
| `cargo fmt --all --check` | **failed** (`nexus-cli/src/main.rs`) | clean |
| `cargo clippy --workspace --all-targets -- -D warnings` | **failed**: `manual_clamp` in `nexus-domain/src/retry.rs:112`, `manual_range_contains` in `nexus-domain/tests/domain.rs:166` | clean |
| `cargo test --workspace --locked --release` | 30 passed | 30 passed |

The fixes are the smallest correct ones (`max(0).min(20)` → `clamp(0, 20)`;
`a >= x && a < y` → `(x..y).contains(&a)`), not `#[allow]` suppressions.

---

## 3. Verdicts

| Gate | Verdict | Reason |
| --- | --- | --- |
| **A — Phase 2 correctness** | **BLOCKED** | the candidate commit and branch do not exist; the claimed Rust worker/API/store are absent |
| **B — Rust reliability** | **NOT VERIFIED** | no Rust worker exists to test concurrency, lifecycle or recovery |
| **C — Python/Rust compatibility** | **ACCEPTED WITH CONDITIONS** | 22 existing parity/claim checks pass on the real surface (claim + reclaim only); no worker/API parity is possible |
| **D — Distributed execution safety** | **ACCEPTED WITH CONDITIONS** | verified for the Python dispatch path (9 crash-window cases, see `DISPATCH_RECOVERY_PROTOCOL.md`); no Rust dispatch exists |
| **E — Security** | **ACCEPTED WITH CONDITIONS** | no critical/high defect found in the audited Python scope; the Rust API does not exist, so its security cannot be assessed |
| **F — Phase 3 readiness** | **BLOCKED** | the real OpenHands executor for Rust does not exist |
| **G — Production migration readiness** | **REJECTED** | API parity is impossible: there is no Rust API |

**No gate is passed by inventing a component.** Six of seven are blocked or
conditional on the candidate being supplied.
