# NEXUS — Independent Code Review of the Reconstructed Integration

**Date:** 2026-11-17 · **Branch:** `recovery/nexus-integration-revalidated`
**Base:** `6e5c2b410d531379c3599baa53492c83244551e5` (`main`, PR #49 merge)
**Method:** read the actual transaction code in this tree (SQL statements,
transaction boundaries via `PostgresStore._tx`, lock kinds). No test execution
is claimed in this document; all "VERIFIED" tags here mean *verified by
inspection*, nothing more. Fresh test results live in
`reports/INTEGRATION_RECOVERY_EVIDENCE.md`.

**Scope files:** `orchestration/nexus_lab_store.py`, `orchestration/cloud_store.py`,
`orchestration/nexus_projects.py`, `orchestration/web_server.py`,
`orchestration/mcp_server.py`, `orchestration/nexus_policy.py`,
`orchestration/cli.py`, `orchestration/lab_worker.py`,
`orchestration/studio/app.js`, `tests/test_cancel_lock_order.py`,
`tests/test_run_history.py`, `tests/test_route_auth.py`, `tests/test_cli_mcp.py`.

---

## 1. Derived lock order (from the code, not from a document)

Universal observed order for row locks across write paths:

**run row(s) → task row(s) → execution row(s)**

with new-row-only inserts (`INSERT … RETURNING` of fresh rows, or
`INSERT … SELECT`/`ON CONFLICT DO NOTHING` against rows the path does not need
to lock) and read-only paths elsewhere. Refinements observed:

- `claim_task` (L~1205): `nexus_runs` `FOR UPDATE` → `pg_advisory_xact_lock('nexus-active-tasks')` → count in-flight → `pg_advisory_xact_lock(branch-key)` → re-check branch-free predicate → `UPDATE nexus_runs` → `UPDATE nexus_run_tasks … FOR UPDATE SKIP LOCKED`. Run → tasks. Advisory locks are leaf latches (never held while waiting on another path's row locks except as shown; the branch latch is released with the transaction).
- `_apply_run_state` (L~852, cancel/pause/resume/complete/fail/blocked): `nexus_runs` `FOR UPDATE` → `nexus_executor_stops` `INSERT…SELECT` (new rows) → **`UPDATE nexus_run_tasks`** (L~889) → **`UPDATE nexus_task_executions`** (L~896). Run → tasks → executions. **This order is the reconstructed fix** (tasks before executions); on `main` the executions bulk-update ran first — an inversion against every worker settle.
- `mark_running` / `record_start` (L~1370, L~1367+): task row `FOR UPDATE` (or `UPDATE … RETURNING`) → `_close_execution` (executions) → `INSERT nexus_task_executions` (new row; FK `task_id→nexus_run_tasks.id` takes `FOR KEY SHARE` on the task — a *downgrade-compatible* lock in the same item direction, and re-entrant in the same transaction) → `INSERT nexus_executor_stops`. Task → executions.
- `record_verification` (L~1483+): task `UPDATE…RETURNING` → executions via `_close_execution` → runs plain read (no lock). Task → executions.
- `retry_task` (L~1537): task `FOR UPDATE` → task update → executions via `_close_execution`. Task → executions.
- `wait_task` (L~1588): plain task read → `runs` `FOR UPDATE` → tasks `UPDATE`. Run → tasks.
- `postpone_task` (L~1745): `runs` `FOR UPDATE` → tasks `UPDATE`. Run → tasks.
- `reclaim_expired` (L~2088): `runs` `FOR UPDATE` (policy row) → stale tasks `FOR UPDATE`. Run → tasks. **The PR #47 fix, retained.**
- `restart_run` (L~673) / `duplicate_run` (L~710): `runs` `FOR UPDATE` (source) → plain goals read (duplicate) → `INSERT` new run (+ goal for duplicate) → `UPDATE nexus_goals` (new active run) → `INSERT` new tasks. No pre-existing task/execution rows of the source run are locked. New rows only.
- `start_run` (L~339): goals `FOR UPDATE` → `INSERT` run → `UPDATE nexus_goals` → `INSERT` tasks. Goals is the parent namespace here.
- `_close_execution` (L~1155): callers hold the task; executions only.
- Reports/notifications/events (`reports.write_terminal`, `_notify`, `ProjectService._event`): new-row inserts; `_tell_chat` takes `nexus_conversations` `FOR KEY SHARE` — a leaf.
- Work-queue tables (`nexus_tasks` outbox etc., e.g. L~2393, L~2597): `FOR UPDATE [OF …] SKIP LOCKED` on their **own** rows only; unrelated to the run/task/execution hierarchy.
- `nexus_openhands_accounts.py:541`: reads executions, no lock.
- `nexus_projects.py`: `_project` takes `FOR KEY SHARE OF m` on the membership row (L~103) — compatible with everything except `FOR UPDATE/DELETE` of that row, and no path locks membership rows in the run hierarchy. Project delete (L~343) takes `FOR UPDATE` on the project row; it races nothing in the claim/cancel paths that would form a cycle through run/task rows.

### 1.1 Residual ordering notes (recorded, not claimed away)

1. **Goals lock order differs by path**: `start_run` locks goals first, `restart_run`/`duplicate_run` lock the *source run* first and goals second. No cycle is possible here: `start_run` never locks an existing run row, and `restart`/`duplicate` never locks an existing goal row before the source run; the goals row they update is the same one `start_run` would lock, but `start_run` does not wait on runs. Still, this is a **documented asymmetry**, serialized by the `uq_nexus_runs_active_goal` partial unique index and the source-row lock.
2. **`_DEPS_VERIFIED` / `_BRANCH_FREE` correlated subqueries** in `claim_task` read sibling task rows without locking them (by design — claim is `SKIP LOCKED` and branch ownership is re-checked under the branch advisory latch). A concurrently finishing sibling can satisfy/invalidate the predicate between read and update; the claim is still safe because the *task* row is locked and the *branch* latch closes the relevant race.
3. **Executor-stops / report writing** happen inside the cancel/fail transaction; they insert new rows only and cannot invert with anything.
4. `_requeue_retryable` (inside `_apply_run_state`'s resume path) updates tasks while the run row is held — run → tasks, consistent.
5. **No global deadlock-freedom is claimed.** PostgreSQL's deadlock detector remains the backstop; the two run-settle paths (`set_run_state`, `set_run_state_internal`) carry the PR #46 bounded retry (max 1 retry on `DeadlockDetected` only, whole transaction retried after rollback, all writes idempotent). Nothing else is retried.

### 1.2 Exhaustive executions write-site sweep

Repo-wide search for writes to `nexus_task_executions`: exactly three write sites
(`nexus_lab_store.py` L~896 — the fixed cancel bulk close; `_close_execution` L~1155;
`mark_running`'s `INSERT` L~1423). All three now follow task-before-execution.

**Result: no task-before-run inversion and no execution-before-task inversion remains
in the audited paths. One ordering class (goals vs source-run) is asymmetric but acyclic — see 1.1.1.**

---

## 2. Authorization review

### 2.1 HTTP route surface (`orchestration/web_server.py`)

- Route registration: `POST …/runs/{id}/restart|duplicate` require authentication
  (the whole `/api/v1` surface sits behind session/bearer auth — enforced before
  `rest` dispatch; unauthenticated requests get **401**, asserted by
  `tests/test_route_auth.py` for both routes via `PROTECTED_POST`).
- Project scope: handlers call `lab.restart_run / duplicate_run(principal,
  **project_id**, run_id[, key])`; the store re-checks membership inside the
  transaction (`ProjectService._project` → `NotFound` for non-members, which the
  top-level mapper renders as **404, never 403** — see any existing route's
  mapping; the restart/duplicate handlers only translate `InvalidTransition` → 409
  and `IdempotencyConflict` → 409 and let `NotFound` propagate to the same mapper).
- Run ownership: the source run is read with `WHERE id=%s AND project_id=%s FOR UPDATE` —
  a cross-project run id simply does not exist for the caller's scope.
- Task ownership: not applicable to these two routes (no task ids in path).
- Optional `Idempotency-Key` header or body `idempotency_key` on duplicate;
  a conflicting key → `IdempotencyConflict` → **409**.
- Restart/duplicate while the run is not terminal → `InvalidTransition` → **409**.

### 2.2 Store-level (authoritative)

`LabStore.restart_run` / `duplicate_run` begin with `ProjectService._project(cur,
principal, project_id)` inside `_tx()`; the same membership proof every other
run operation takes. A foreign principal gets `NotFound` — asserted by
`tests/test_run_history.py::test_restart_and_duplicate_are_member_only`
(`user:stranger` → `NotFound` on both operations).

### 2.3 MCP (`mcp_server.py`, `nexus_policy.py`)

- Tools `restart_run` / `duplicate_run` are registered once each.
- Each handler calls `self._member(project_id)` first (membership → `NotFound`).
- Scopes: `MCP_TOOL_SCOPES["restart_run"] == SCOPE_RUN_CONTROL` and
  `["duplicate_run"] == SCOPE_RUN_CONTROL` — the same control scope as
  `cancel_run`; asserted by `tests/test_cli_mcp.py::test_mcp_registers_and_scopes…`
  (the two new tests added by the feature) and exposed in `tools/list`.

### 2.4 CLI (`cli.py`)

- `_client` attaches the bearer token (`Authorization: Bearer …`), never printed.
- F-01 guard retained: the base URL is parsed with `urlsplit`; a scheme other
  than `http`/`https` (e.g. `file://`) or a missing netloc fails loudly at
  construction (`CliError(EXIT["config"])`) — verified present in this tree.
- `nexus run restart|duplicate` are thin HTTP calls; they implement **no**
  authorization of their own and correctly rely on the server.

### 2.5 Studio (`orchestration/studio/app.js`)

- Buttons «Перезапустить» (`run-restart`) «Дублировать» (`run-duplicate`) render
  only on finished-run cards (`!live` branch) and POST to the same project-scoped
  endpoints. Studio is a client, not an authorization boundary.

### 2.6 Negative cases observed

| Case | Expected | Where |
|---|---|---|
| Unauthenticated POST to both routes | 401 | `tests/test_route_auth.py` |
| Non-member (store) | `NotFound` (→ 404) | `tests/test_run_history.py::test_restart_and_duplicate_are_member_only` |
| Run not over | 409 | `tests/test_run_history.py::test_restart_run_refuses_an_active_run` |
| Duplicate with same key, retried | 200, same copy, `created:false` | `test_duplicate_run_is_idempotent_with_a_key` |
| Conflicting idempotency key | 409 | `web_server.py` mapper + `IdempotencyConflict` |

**Gap noted (not fixed here — reconstruction must not smuggle scope):** there is
no HTTP-level cross-project negative test for these two routes specifically
(the store-level one exists and is the authoritative check; every other run
route shares the same project-scoped skeleton). Also untested: duplicate against
a nonexistent run id (expected 404), and restart on a run the caller can see but
that belongs to a *different* project (404 via `project_id` predicate). These are
advisory follow-ups for a later cycle, not defects in this reconstruction.

**No protection was weakened anywhere in this reconstruction.**

---

## 3. Restart / Duplicate behaviour review

- **Original run intact:** yes — restart/duplicate never write to the source
  run's rows; they read its plan (`_plan_from_run`) and create new rows. The old
  run keeps state, tasks, executions, evidence and report.
- **New identity:** restart → new run under the **same goal** (documented
  semantics: re-running the same plan for the same request); duplicate → new
  **goal + run** (a copy cannot share the goal — one active run per goal).
  New `run_id` in both cases; old run remains as history.
- **Source binding enforced:** source run is `SELECT … FOR UPDATE` with
  `project_id` predicate; unfinished source → 409; restart of a run whose goal
  already has an active run → 409 (checked explicitly, and serialized by the
  source-row lock against a concurrent restart; `uq_nexus_runs_active_goal`
  partial unique index is the database backstop).
- **Idempotency:** duplicate uses `INSERT … ON CONFLICT (project_id,
  idempotency_key) DO NOTHING RETURNING …`; on conflict it re-reads the goal and
  its latest run and returns it with `created:false`; a key conflict with no row
  (impossible under the unique index, but coded defensively) raises
  `IdempotencyConflict` (→ 409). Retried requests never double-create.
- **Invalid states rejected:** non-terminal source → 409 (`restart` while
  `running`, `duplicate` while `running`); no plan → 409; goal already has an
  active run (restart) → 409.
- **Cancellation interplay:** cancel touches only unfinished runs; a restart or
  duplicate source must be terminal by definition, so the two operations cannot
  race on a live run. A cancel landing between a restart's source-read and its
  new-run insert commits atomically with the restart transaction — there is no
  interleaving in which a cancelled run is "restarted" half-way.
- **Worker crash / restart resilience:** the new run's tasks start `pending` at
  attempt 0; the scheduler and the normal reclaim path pick them up unchanged —
  no new recovery machinery is required. If the process crashes after the
  restart transaction commits but before dispatch, the run is simply picked up
  on the next pass; if it crashes inside the transaction, nothing of the copy
  exists (single `_tx()` for goal+run+tasks — atomic).
- **Evidence chain:** duplicate/restart create **new** evidence rows going
  forward; the old run's signed evidence is untouched; nothing re-signs history.
- **Goal text copy (duplicate):** the copied goal text is truncated at 19,800
  chars before appending the marker, respecting the length limit — retained as
  delivered.

---

## 4. Implementation duplication / consistency audit

- `restart_run` / `duplicate_run` have exactly one implementation each
  (`LabStore`); MCP, CLI, Studio, and the HTTP routes all call into it — no
  divergent copies.
- MCP tools registered exactly once; one `PROTECTED_POST` line; one Studio
  handler per action; one CLI command pair; one route-auth regression line.
- The integration's own docs commit (lost `ce21e47`+`baa3251`) is reconstructed
  as the rewritten reconciliation sections of `FINAL_RELEASE_REPORT.md` §18/§19
  and `COMPLETION_LEDGER.md` Phase 9 + reconciliation record — updated for the
  `6e5c2b4` base (PR #49 facts retained).
- `PR46_PR47_FINAL_INTEGRATION_REPORT.md` is included **verbatim with a
  SUPERSEDED banner** (it documents the lost integration; its SHAs and evidence
  paths are unresolvable/destroyed). It is not evidence for this branch.

## 5. Findings summary

| # | Severity | Finding | Disposition |
|---|---|---|---|
| F-1 | P1 (latent) | Cancel↔worker-settlement task↔executions inversion (historical) | **Structurally closed** in this reconstruction (`_apply_run_state` tasks→executions); pinned by `tests/test_cancel_lock_order.py` |
| F-2 | P2 | `reclaim_expired` read the run row unlocked (historical, PR #47) | **Closed** (run `FOR UPDATE` first), retained from PR #47 |
| F-3 | Info | Goals-vs-source-run lock asymmetry between `start_run` and restart/duplicate | Acyclic; recorded; no change |
| F-4 | Info | No HTTP-level cross-project negative test for restart/duplicate specifically | Follow-up; store-level check is authoritative and tested |
| F-5 | Info | `_DEPS_VERIFIED`/`_BRANCH_FREE` read siblings unlocked | By design (SKIP LOCKED + branch latch + re-check); recorded |
| F-6 | Risk | Combined CI absent; PG 17 not used locally for this tree unless recorded in the evidence report; Chromium likely unavailable | Owner gate; see evidence + final report |

**Review verdict:** no blocking defect found in the reconstructed code paths.
The two historical lock-order inversions are closed and pinned. Authorization is
layered and additive. Restart/duplicate semantics are sound under retries,
races and crashes. Confidence is bounded by what was inspected; nothing global
is claimed.
