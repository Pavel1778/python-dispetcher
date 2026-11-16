# NEXUS — Logic & Reliability Audit (restored 2026-11-16)

Phase A companion. Base: consolidated `main` `42824896` (worker/scheduler
preserved unchanged by the consolidation, per `CONSOLIDATION-LEDGER.md`:
atomic claim, SKIP LOCKED, lease fencing, heartbeats, reclaim, restart
recovery, start hand-over, DAG, parallel tasks, bounded retries,
cancellation, no-progress).

Labels: VERIFIED / TESTED / STATICALLY REVIEWED / BLOCKED.

## Scheduler claim/lease (`nexus_lab_store.claim_task` / pool-based store)

- One transaction: locked run row, `state='running'`, iteration/deadline
  checks, `resume_at` window; `pg_advisory_xact_lock` serialises the
  active-tasks limit and branch acquisition; task taken `FOR UPDATE SKIP
  LOCKED`; lease token fencing renders post-lease writes no-ops.
  **STATICALLY REVIEWED + TESTED (durable-run and concurrency suites).**

## Run lifecycle / cancellation (`_apply_run_state`)

- `cancel` is transactional: `nexus_executor_stops`, executions ended,
  unfinished tasks blocked, `run.cancelled` event, terminal report;
  idempotent. **TESTED** (cancel suite).
- `pause` = dispatch hold only; does not interrupt running conversations.
  Durable waiting shows `resume_at` + `wait_reason` (auto-resume with
  restored budget). UI now says so (F-02, both in base and docs). **TESTED.**
- `resume` requeues retryable tasks, durable ⇒ new cycle with restored
  budget. **TESTED.**

## Recovery

- Lost worker: lease expiry → `reclaim_expired`; `_follow_starts` binds a
  start accepted pre-crash; `_stop_abandoned` prevents two writers on one
  branch. **TESTED vs fakes; live restart drill BLOCKED (needs real worker
  + service).**

## Limits & budgets

- Task/run budgets (`max_attempts`, `max_iterations`, deadline, task time
  limit) bound every retry path; durable cycles carry `cycle`/`attempt`.
  **STATICALLY REVIEWED + TESTED.**

## Not done this pass

- PostgreSQL kill-mid-transaction chaos drill beyond the suite's coverage.
- Live worker crash against a real OpenHands (BLOCKED).

## Settle deadlock on cancel (F-06)

- **Reproduced** (rarely) by this branch's full suite: member-facing
  `set_run_state` cancel vs a worker settling one task of the same run;
  PostgreSQL reported a deadlock while updating `nexus_run_tasks`; the
  test's POST cancel returned HTTP 500.
- **Root cause:** two paths updating the same task rows in the same run
  without a shared lock order (the cancel batch holds the run row and waits
  on task rows; the worker holds task rows and never asks for the run row
  before writing). The global run-row-first rule is honoured everywhere it
  matters, but the batch-update path still collided with per-task writes
  under load.
- **Mitigation (this branch):** one bounded retry on `DeadlockDetected` in
  both `set_run_state` variants. Safe because the aborted transaction
  commits nothing and all its writes are idempotent. Deterministic proof:
  `tests/test_deadlock_retry.py` (injected real driver exception).
- **Honest residual:** the lock-ordering hazard is *mitigated*, not proven
  absent — a systemic lock-order fix would need the exact wait-for graph,
  which PostgreSQL did not log beyond the two PIDs it printed. A second
  deadlock is therefore surfaced, not hidden.

## Defects fixed here / in base

- F-06 (settle deadlock retry) — this branch, TESTED.
- F-01 (CLI scheme) — this branch, TESTED.
- F-02 (pause honesty) — pre-consolidation, merged into base by PR #45;
  verified present at HEAD.
- No new logic defect found in the reviewed surfaces; none is claimed.
