# PR #46 → PR #47 Integration — Final Report

**Date:** 2026-11-16
**Repository:** `Pavel1778/python-dispetcher`
**Task:** integrate PR #46 (already merged to `main`) and PR #47 (restart/duplicate
runs) in that order onto the assigned integration branch — no redesign, no
push, no publish, STOP for owner approval at the end.

**Branch integrated onto:** `arena/71f72168-python-dispetcher`
**Code-complete tip:** `ce21e474a485edb8525f0a9091b5f2bb030c026a` (`ce21e47`) — code-complete; this report and the final doc touch-ups land one docs commit after it (§14).
**Status of this branch:** NOT PUSHED, NOT PUBLISHED, NOT MERGED — awaits owner approval.

---

## 1. Gate summary (read this first)

| Gate | Verdict |
|---|---|
| Integration performed locally on the assigned branch | **DONE** (ec0d553 → ce21e47) |
| Conflicts (exactly 3 known) resolved | **DONE** (§5) |
| Both deadlock protections retained + one structural inversion closed | **DONE** (§7) |
| Route-auth coverage for restart/duplicate | **DONE — added, nothing weakened** (§8) |
| Full Python suite on the integrated tree (local PostgreSQL 16.2) | **1951 passed, 49 skipped, 0 failed (35:16)** — EXIT=0 |
| Lint (`ruff check .`), `git diff --check` | **clean** |
| Studio render test (Node v22.22.3) | **passed** |
| Studio UI tests | **26 passed** |
| Concurrency stress (24-test cancel/reclaim/race/deadlock selection) | **30/30 reps green — 720 runs, 0 failures** |
| Combined CI on the integrated branch | **NOT RUN — BLOCKED** (requires a push; pushing is forbidden by this task) |
| PostgreSQL 17 locally | **BLOCKED** (sandbox has PG 16.2 only; PG 17.11 is exercised by CI) |
| Chromium browser E2E locally | **BLOCKED** (no browser binary; egress to the installer is not reachable) |
| Live OpenHands services | **NOT CONTACTED** (no credentials touched; not claimed) |
| **Overall** | **NOT FULLY VERIFIED — not merge-ready.** The integrated tree is locally green, but the mandatory combined-CI gate has not run. Do not merge until CI is green on the pushed branch. |

Everything below separates what was **verified locally** from what is a **CI
gate**, what is a **live-service** claim (none is made here), and what is
**blocked**. No number in this file is folded into another environment's
number.

---

## 2. Starting state (Step 1 — fresh GitHub verification, 2026-11-16)

- `origin/main` = **`66291614ac2c47b33e825312c9f165f7b38de5cd`** (`6629161`,
  "Merge pull request #46 from Pavel1778/arena/3a132869-python-dispetcher").
  Confirmed via `git fetch` + `git ls-remote`.
- **PR #46: MERGED** 2026-11-16T22:36:31Z, merge commit `6629161` (owner-approved
  merge on 2026-11-16; that permission was spent on #46 and is not reused here).
- **PR #47: OPEN**, head **`f6e77c9682eb7df296a68f6720e85d89de01f5c7`** (`f6e77c9`),
  base `main`. At check time: `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY`
  (expected — it was authored against pre-#46 `main`). Unchanged by this work.
- Current branch `arena/71f72168-python-dispetcher` @ `6629161`, working tree
  clean, based on the actual current `main`. This branch is the integration
  branch; no other branch was created, switched to, force-pushed, or deleted.
  No nested worktrees; no branch-shadowing names (`origin/…`) were used.
- `git fetch origin` was run before any verification; the integration was
  built on the fetched `origin/main` state.

**PR #46 presence re-verified in `6629161` (not re-integrated — it is already
`main`):**

- F-01 `file://` guard: `orchestration/cli.py` — `Client.__init__` refuses any
  `NEXUS_URL` whose scheme is not `http`/`https` (or missing netloc) with
  `CliError(EXIT["config"])`. `tests/test_cli_client.py` (5 tests) all pass
  (`file://`, schemeless, `ftp://` refused; `https://` and loopback accepted).
- F-06 bounded retry: `orchestration/nexus_lab_store.py` — `_is_deadlock`
  (line 73; matches only `psycopg.errors.DeadlockDetected`); whole-transaction
  retry in `set_run_state` (line 501) and `set_run_state_internal` (line 826);
  **maximum one retry**; rollback happens inside the aborted transaction
  (`self.store._tx()` context) **before** the retry; the retried operations are
  idempotent; no other exception is retried; a second deadlock surfaces as the
  original 500. `tests/test_deadlock_retry.py` (2 tests) pass: retry exactly
  once on an injected single deadlock; a persistent deadlock still raises and
  commits nothing.

## 3. Final branch and commit sequence (order matters)

`arena/71f72168-python-dispetcher`: `6629161` → … → `ce21e47`

| # | SHA | Kind | What and why (in order) |
|---|---|---|---|
| 1 | `ec0d553` | merge commit | `Merge refs/remotes/origin/feat/run-restart-duplicate` — **PR #47 integrated second, on top of PR #46's `main`**. Conflicts resolved in the commit (§5). Commit message is the default merge message (kept deliberately); content of the merge is fully described here and in the two docs it touches. |
| 2 | `2fbdcad` | test | `tests/test_route_auth.py`: `restart`/`duplicate` POST routes added to `PROTECTED_POST` — required by the integration brief; additive only (§8). |
| 3 | `e2c7575` | fix | Structural close of the cancel↔worker-settlement `nexus_run_tasks`→`nexus_task_executions` inversion (§7). Swaps two independent statements in `_apply_run_state`; no behaviour change; adds `tests/test_cancel_lock_order.py`. |
| 4 | `ce21e47` | docs | Records the F-06 structural close and the exact deadlock posture in `FINAL_RELEASE_REPORT.md` / `COMPLETION_LEDGER.md`; both files carry explicit retained/updated/discarded records. |

PR #47-side commits carried by the merge (authored on the PR branch,
unchanged, in order): `b30aba9` (feature + `reclaim_expired` run-row-first
`FOR UPDATE` deadlock fix) → `993ef33` (docs) → `f6e77c9` (docs).

Files changed vs `main` (`6629161…ce21e47`, 15 files):
`COMPLETION_LEDGER.md`, `DEPLOYMENT_CHECKLIST.md` (new from #47),
`FINAL_RELEASE_REPORT.md`, `docs/CLI_REFERENCE.md`, `docs/CRUD-MATRIX.md`,
`orchestration/cli.py`, `orchestration/mcp_server.py`,
`orchestration/nexus_lab_store.py`, `orchestration/nexus_policy.py`,
`orchestration/studio/app.js`, `orchestration/web_server.py`,
`tests/test_cancel_lock_order.py` (new), `tests/test_cli_mcp.py`,
`tests/test_route_auth.py`, `tests/test_run_history.py`.

Diff vs the PR #47 head (`f6e77c9..HEAD`) contains **only**: #46's `_is_deadlock`
+ the two retry loops, this integration's task/executions reorder, PR #48's
`run_blocked`/`task_started` `_notify` additions (already on `main`), and the
route-auth test line + new test file + docs. Nothing unrelated; PR #47's
feature code is byte-for-byte what it shipped.

## 4. Integration order and PR #46 re-verification (Step 2)

PR #46 was already on `main` (merge `6629161`); it was **not** re-merged and
its design was **not** redesigned. Re-verified in the merged tree:

- F-01 `file://` `NEXUS_URL` guard present (see §2). 5/5 CLI-client tests pass.
- F-06 bounded retry present at both settle sites; whole-transaction retry;
  max 1 retry; rollback before retry; idempotent operations; no other
  exception retried. 2/2 deadlock-retry tests pass (injected single deadlock
  retried once and settles; persistent deadlock raises, run stays `running`).
- `docs/REPORT-A-OPENHANDS-INTEGRATION.md` / `docs/ARENA_ENGINEERING_AUDIT.md`
  restored by #46 are present.
- PR #46's `test_approval_notification…` flows unaffected (full suite green).

## 5. The exactly-3 known conflicts and their resolutions (Step 3)

| Conflict | Type | Resolution | Why |
|---|---|---|---|
| `orchestration/nexus_lab_store.py` import hunk | content | `from .nexus_projects import IdempotencyConflict, ProjectService` (kept on both sides — union). Inspected usage: `IdempotencyConflict` is raised at `duplicate_run`'s idempotency path (line 768) and re-raised to the web layer (`web_server.py` maps it to 409); `ProjectService` is used across the store. Nothing unrelated removed; no other import touched. | Brief mandated the union import; dropping `ProjectService` would break the store, dropping `IdempotencyConflict` would break duplicate-with-key retries. |
| `FINAL_RELEASE_REPORT.md` | add/add (main = PR #48 content vs #47's Final Delivery Sprint report) | **`main` version kept as the baseline** (it carries the approval-notification session and the post-#48 verdict). Verified #47 facts reconciled in: new §16 (Integration record) and §17 (Reconciliation record: retained/updated/discarded), §2 (features), §5 (route-auth posture), §13 (per-environment number attribution), §14 (commit table), §15 (verdict qualified by integration gates), §4 (limitations). Stale #47 claims discarded (see the file's §17). External blockers kept blocked; nothing invented; no live-verification claimed for this sandbox. | Brief mandated main-as-baseline with verified #47 information only. |
| `COMPLETION_LEDGER.md` | add/add | **`main` version kept as the baseline** (Phase tables 1–8 unchanged except evidence references). Verified #47 information appended as **Phase 9 — PR #46 / PR #47 integration** (restart/duplicate rows, route-auth rows, deadlock rows). Stale #47 banner (`Base: main @ 4282489 (PR #45)`) and its `1980 passed` suite total discarded; its branch-local stress numbers kept only as labeled branch evidence. Blocked/not-implemented items were **not** promoted. | Brief mandated evidence-based reconciliation; never mark incomplete items complete. |

`git status` after `git merge` reported exactly these 3 conflicted paths; all
other files auto-merged (`cli.py`, `mcp_server.py`, `web_server.py`,
`studio/app.js`, `nexus_policy.py`, docs, tests). The merge was committed as
`ec0d553` **before** any later edit; the two other commits are separate,
auditable steps. No conflict markers remain (`grep -rn '<<<<<<<'` clean).

Both reconciled docs now contain an explicit **retained / updated / discarded**
record with reasons (the file itself, e.g. `FINAL_RELEASE_REPORT.md` §17 and
`COMPLETION_LEDGER.md` → "Reconciliation record"). Claims that integration
verification had already happened were removed; CI/PG17/Chromium are stated as
not-run for this sandbox, never as passing.

## 6. PR #47 feature preservation and invariants (Step 3)

Feature code integrated **without redesign**; all present and exercised:

- `LabStore.restart_run` — finished run's plan re-run as a **new run under the same goal**; every task re-created `pending` at attempt 0; old run untouched as history; refused (409 `InvalidTransition`) while the goal has an active run, and while the run is not over; audit event `run.restarted`; chat notified (`goal_started`).
- `LabStore.duplicate_run` — plan copied into a **new goal + new run** (one active run per goal makes same-goal duplication impossible); `ON CONFLICT (project_id, idempotency_key) DO NOTHING` with fall-through to an existing-key lookup; a key that matched nothing raises `IdempotencyConflict` (HTTP 409).
- Plan+DAG preservation via `_plan_from_run`/`_insert_run_with_plan`: every task (with `dependencies`) re-inserted `pending`, attempt 0, same `task_key`s, same order.
- Surfaces: HTTP `POST /api/v1/projects/{id}/runs/{run_id}/restart|duplicate`; CLI `nexus run restart|duplicate` (`orchestration/cli.py`); MCP `restart_run`/`duplicate_run` (`SCOPE_RUN_CONTROL` in `mcp_server.py` + `nexus_policy.py`); Studio «Перезапустить»/«Дублировать» buttons (`orchestration/studio/app.js` with working `run-restart`/`run-duplicate` handlers).
- One active run per goal is enforced by the partial unique index `uq_nexus_runs_active_goal` (`migrations/0018_nexus_lab_runs.sql`, state `running|paused|waiting|blocked`); idempotency uniques for goals `(project_id, idempotency_key)` (`migrations/0009_nexus_projects.sql:33`) and plans/tasks (`0010`, `0018`) unchanged.
- Project isolation: both routes are project-scoped; a non-member gets 404 — `tests/test_run_history.py::test_restart_and_duplicate_are_member_only` passes.

Invariant test results on this branch (all via `tests/test_run_history.py`,
12/12 passed in 29.69 s, real PostgreSQL):

| Invariant | Test | Result |
|---|---|---|
| Restart re-runs the whole plan as a new run; tasks `pending` @ attempt 0; old run kept | `test_restart_run_re_runs_the_whole_plan_as_a_new_run` | PASS |
| Restart refused while the goal has an active run | `test_restart_run_refuses_an_active_run` | PASS |
| Duplicate makes an independent goal and run | `test_duplicate_run_makes_an_independent_goal_and_run` | PASS |
| Duplicate is idempotent with an `idempotency_key` | `test_duplicate_run_is_idempotent_with_a_key` | PASS |
| Restart/duplicate are member-only (non-member → 404) | `test_restart_and_duplicate_are_member_only` | PASS |
| (Pre-existing history tests retained) | `test_hide_and_show_a_finished_run`, `test_removing_a_finished_run_keeps_the_goal_chat_and_repository`, `test_an_active_run_cannot_be_removed_from_history`, `test_removing_a_run_keeps_the_evidence`, `test_retry_failed_*` (3) | PASS |

## 7. Both deadlock protections — preserved, plus one structural inversion closed (Steps 5–6)

**Protection 1 (PR #47, retained unchanged):** `LabStore.reclaim_expired` locks
the **run row `FOR UPDATE` first**, then the lease-expired `dispatched|running`
task rows with no bound conversation (`FOR UPDATE`), the order every other
site already uses. The old order (tasks first, then the run) inverted against
cancel and deadlocked under load (`psycopg.errors.DeadlockDetected`).
Confirmed present and unmodified by this integration (`git diff ec0d553 --`
shows no change to that block).

**Protection 2 (PR #46, retained unchanged):** bounded whole-transaction
`DeadlockDetected` retry in `set_run_state` / `set_run_state_internal` —
`_is_deadlock` matches only `psycopg.errors.DeadlockDetected`; maximum one
retry; the transaction is retried as a whole; rollback occurs inside the
transaction before the retry; the writes are idempotent; no other exception is
retried. Confirmed present and unmodified.

**Newly closed in this integration (`e2c7575`):** the cancel↔worker-settlement
inversion.

The pre-integration review noted that neither protected path covered the
cancel-side **task → executions** ordering. Inspection of the actual SQL found:

- **Cancel** (`_apply_run_state`, `state in ("cancelled","failed")`) updated
  `nexus_task_executions` (bulk, by `run_id`) **before** `nexus_run_tasks`
  (bulk, by `run_id`).
- **Every worker-side settle** (`mark_running`, `record_verification`,
  `retry_task`, and the `_close_execution` call sites) locks the **task** row
  first and closes/inserts execution rows **after** — the canonical order.

So cancel held the run's execution rows and waited on a task row the worker
held, while the worker waited on those executions: a row-level cycle. The two
statements in the cancel block are mutually independent (same transaction, no
cross-reads), so the reorder is semantics-preserving and atomic. After
`e2c7575`, cancel blocks unfinished tasks **first**, then closes executions —
the worker's order. The run row remains the outermost lock, as in
`claim_task`, `postpone_task` and `reclaim_expired`.

**Deterministic regression test** — `tests/test_cancel_lock_order.py`:
a worker holds `nexus_run_tasks` (uncommitted); the cancel starts; the test
waits until the cancel transaction is blocked on the task lock; then the
worker closes its executions with a 200 ms `lock_timeout` (deliberately far
below the default 1 s `deadlock_timeout`, so the outcome never depends on
which transaction the deadlock detector picks as victim).

- With the fix (`e2c7575`): worker's executions UPDATE succeeds; cancel
  completes; the execution is closed by the cancel with outcome `cancelled`. — **PASS**
- With the old order: worker's UPDATE fails deterministically with
  `LockNotAvailable` (verified by temporarily restoring the old order;
  restored afterwards). — **FAIL** (test correctly catches the regression)

**Lock-order table** (write paths, from the actual SQL; `nexus_lab_store.py`
unless noted; "—" = no lock/new rows only; all statements in one tx):

| Path | 1st | 2nd | 3rd | Consistent with global order? |
|---|---|---|---|---|
| `start_run` | `nexus_goals` FOR UPDATE | INSERT `nexus_runs`, UPDATE `nexus_goals` | INSERT tasks (new rows) | ✔ (goals is the run's parent namespace; no inversion vs tasks/executions) |
| `claim_task` | `nexus_runs` FOR UPDATE | advisory(`nexus-active-tasks`) → `nexus_run_tasks` FOR UPDATE SKIP LOCKED | advisory(branch) → UPDATE `nexus_runs`, UPDATE `nexus_run_tasks` | ✔ run → tasks |
| `set_run_state` / `set_run_state_internal` (cancel/pause/resume/complete/fail) | `nexus_runs` FOR UPDATE | `executor_stops` INSERT…SELECT (no row locks) → `nexus_run_tasks` UPDATE (after fix) | `nexus_task_executions` UPDATE | ✔ run → tasks → executions |
| `retry_failed` | `nexus_runs` FOR UPDATE | `nexus_run_tasks` UPDATE (requeue) | — | ✔ run → tasks |
| `restart_run` | `nexus_runs` (source) FOR UPDATE | `nexus_goals` (read) → INSERT `nexus_runs`, UPDATE `nexus_goals`, INSERT tasks | — | ✔ run → goals/tasks (new rows) |
| `duplicate_run` | `nexus_runs` (source) FOR UPDATE | `nexus_goals` (read) → INSERT goal (ON CONFLICT) → INSERT `nexus_runs`, tasks | — | ✔ |
| `mark_running` | `nexus_run_tasks` (FOR UPDATE / UPDATE…RETURNING) | `_close_execution` → UPDATE `nexus_task_executions` (+ INSERT execution; FK `task_id → nexus_run_tasks.id ON DELETE CASCADE` takes FOR KEY SHARE on the task — same direction) | `nexus_executor_stops` INSERT (ON CONFLICT) | ✔ tasks → executions |
| `record_verification` | `nexus_run_tasks` UPDATE…RETURNING | `_close_execution` → executions | `nexus_runs` read (project_id only, no lock) | ✔ tasks → executions |
| `retry_task` | `nexus_run_tasks` FOR UPDATE | `_close_execution` → executions | — | ✔ tasks → executions |
| `wait_task` | (plain read of task) → `nexus_runs` FOR UPDATE | `nexus_run_tasks` UPDATE | (optional) `nexus_runs` UPDATE | ✔ run → tasks |
| `postpone_task` | `nexus_runs` FOR UPDATE | `nexus_run_tasks` UPDATE | — | ✔ run → tasks |
| `release_task` / `hold_task` / `note_progress` / `reopen_task` / `switch_task_executor` (task-scoped) | `nexus_run_tasks` UPDATE (or FOR UPDATE) | — | — | ✔ (prefix of global order) |
| `record_start` / `take_start` | `nexus_run_tasks` UPDATE | — | — | ✔ |
| `reclaim_expired` (PR #47) | `nexus_runs` FOR UPDATE | `nexus_run_tasks` FOR UPDATE | — | ✔ run → tasks |
| `_close_execution` (shared helper) | (callers hold the task row) → executions | — | — | ✔ |
| `delete_run` | `nexus_runs` FOR UPDATE | goals UPDATE → DELETE run (FK cascades tasks→executions) | — | ✔ (DB-enforced order matches) |
| `_tell_chat` (blocked notice) | `nexus_conversations` FOR KEY SHARE | INSERT conversation message | — | ✔ (never holds tasks/executions) |
| Reports (`nexus_reports.write_terminal` on cancel/fail) | goals/specs/tasks **reads, no locks** | INSERT outbox/notifications (new rows) | — | ✔ (read-only) |
| `nexus_notifications` inserts (`task_started`, `run_blocked`, …) | — (new rows) | — | — | ✔ |

Exhaustive sweep for `nexus_task_executions` writes in the codebase found
exactly three write sites (cancel bulk close — fixed; `_close_execution`;
`mark_running`'s INSERT), plus two read-only sites (`nexus_openhands_accounts`,
`nexus_reports`). After `e2c7575`, **every** write path respects
run → tasks → executions or is a new-rows-only insert.

**Remaining risks — stated plainly:**

- **No global deadlock-freedom proof is claimed.** The table covers the
  `nexus_lab_store` settlement paths, the scheduler, and the report/notification
  writers; it is not a proof over every table/statement in the system.
- An unobserved ordering can still surface `DeadlockDetected`; PostgreSQL will
  abort one transaction (nothing is half-committed), and on the two settle
  paths PR #46's retry will absorb a single occurrence. A persistent one
  surfaces as the pre-existing 500.
- `mark_running`'s FK share on the parent task row is consistent with the
  fixed order; no inversion remains on that interaction.
- PR #46's bounded retry is a backstop, **not** a license to add order-
  violating paths in future work.
- Deadlock stress re-verified on this branch: **30/30 reps green** across a
  24-test cancel/reclaim/race/deadlock selection (exact list in §10).

## 8. Route-authorization (Step 6)

`restart`/`duplicate` POST routes were **missing** from
`tests/test_route_auth.py::PROTECTED_POST` (neither `main` nor PR #47 had
added them — a gap in #47's coverage, not a product defect: the routes were
already project-scoped and member-only in effect). Fix `2fbdcad` adds two
entries to the list — nothing else touched:

```python
f"/api/v1/projects/{UUID}/runs/{UUID}/restart",
f"/api/v1/projects/{UUID}/runs/{UUID}/duplicate",
```

Results after the addition (route-auth suite, 11/11 passed in 16.40 s):

- Unauthenticated `POST …/runs/{id}/restart|duplicate` → **401** (via
  `test_protected_post_endpoints_require_auth`).
- Authenticated requests reach the handler (suite's authenticated cases) and
  membership is enforced — a non-member gets **404**
  (`tests/test_run_history.py::test_restart_and_duplicate_are_member_only`,
  12/12 pass).
- `SCOPE_RUN_CONTROL` still covers both new MCP tools
  (`tests/test_cli_mcp.py::test_mcp_…` — restart/duplicate asserted in the
  21/21-passing suite; scopes in `nexus_policy.py` unchanged by this task).
- No protections weakened; the change is additive coverage only.

## 9. Full verification battery (Step 7) — what ran where

**Everything in this section ran on HEAD = `ce21e47`** (the code-complete tree:
`6629161` + PR #47 merge + route-auth + cancel-settlement fix; the docs commit
on top changed no code). Environment: Python 3.11.2, pytest 9.0.3, ruff
0.13.1, Node v22.22.3, **PostgreSQL 16.2** (pgserver, socket DSN
`/home/user/pgdata-nexus`) — **not PG 17**. Fresh database reset per test
module (DROP SCHEMA + all 41 migrations).

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check .` | **All checks passed** |
| Diff hygiene | `git diff --check` | **clean** |
| F-01 / F-06 / deadlock injection | `pytest tests/test_cli_client.py tests/test_deadlock_retry.py tests/test_cancel_lock_order.py` | **8 passed** |
| Restart/duplicate invariants | `pytest tests/test_run_history.py` | **12 passed** |
| Route authorization | `pytest tests/test_route_auth.py` | **11 passed** |
| CLI/MCP surfaces | `pytest tests/test_cli_mcp.py` (with `TEST_SUPABASE_DB_URL`) | **21 passed** |
| Studio render (Node) | `node tests/studio_render_test.js` | **studio render tests passed** |
| Studio UI | `pytest tests/test_studio_ui.py` | **26 passed** |
| Full Python suite | `pytest tests/ -q` (fresh DB) | **1951 passed, 49 skipped, 0 failed — 0:35:16 — EXIT=0** (log: `/home/user/pytest_integration_final.log`) |
| Concurrency stress | 30 reps × selection of 24 (`test_race_protection.py` ×13, `test_deadlock_retry.py` ×2, `test_cancel_lock_order.py` ×1, reclaim tests ×6, cancel-twice + cancel-before-start ×2) | **30/30 passed; 720 test-runs; 0 failures** (logs `/tmp/stress_rep_*.log`; selection in §10) |
| Migration test on PG 17 | — | **BLOCKED** — no PG 17 locally; CI provides `postgres:17` service |
| Chromium browser E2E | `pytest tests/test_browser_e2e.py` | **BLOCKED locally** — Playwright present, browser binary absent; 37 journeys skip (`37 skipped`); **not run, not claimed** |
| Studio / Chrome devtools checks | — | **BLOCKED locally** (same reason); studio render + UI unit tests do pass |
| Live OpenHands acceptance | — | **NOT RUN** — no credentials touched; not claimed |

**Skips, not misrepresented:** 49 skipped in the full suite = 37 browser-E2E
journeys (Chromium unavailable locally; `tests/test_browser_e2e.py` reports
`37 skipped` when run) + 4 Android-SDK build-tools cases + 8 other
environment-gated skips (missing `TEST_POOLER_DSN`, Windows-only mingw-w64
cases, live-service variants) — the same environmental classes CI lists. No
skip is a failure; no skip was counted as a pass; the exact list is one
`pytest -q -rs` away. PG 16.2 vs CI's PG 17.11 means migration behavior on PG
17 is **not** verified here — CI's `postgres:17` leg is the gate for that.

**Suite-number provenance across this session (never sum, never mix):** this
branch **1951/49/0** (PG 16.2) · trial merge tree earlier
**1950/49/0** (PG 16.2, 35:39) · PR #47 branch **1980/9/0** and its CI
**1983/6/0 on PG 17.11 + Chromium** (that branch's environment) · `main`
(PR #48 session) **1976/9/0 on PG 17** · clean-main reference **1973/9/0**.

## 10. Concurrency stress — exact definition

Selection (24 tests, all DB-backed): `tests/test_race_protection.py` (13),
`tests/test_deadlock_retry.py` (2), `tests/test_cancel_lock_order.py` (1),
`tests/test_comprehensive_suite.py::test_an_expired_lease_is_reclaimed_and_re_claimed`,
`::test_a_started_conversation_is_never_reclaimed_into_a_second_start` (2),
`tests/test_acceptance_recovery.py::test_an_expired_lease_with_no_conversation_is_reclaimed`,
`::test_lease_reclaim_is_fenced_so_the_dead_worker_cannot_write` (2),
`tests/test_lab_store.py::test_expired_lease_is_reclaimed` (1),
`tests/test_control_plane.py::test_expired_lease_can_be_reclaimed` (1),
`tests/test_durable_runs.py::test_cancel_twice_is_the_same_as_once`,
`::test_a_cancel_between_the_claim_and_the_start_still_stops_the_conversation` (2).

Protocol: 30 sequential reps; each rep re-runs the 24 tests against the
PG 16.2 cluster, which resets the schema per module (DROP SCHEMA public
CASCADE + migrations) — reps are independent. Result: **30 passed reps, 0
failed reps; 720 test-runs; 0 failures, 0 retries used to pass, 0 skips
injected**. The one earlier observed real-world failure this campaign responds
to (pre-`#46` cancel deadlock, `test_long_task_cases`) is additionally covered
by `tests/test_deadlock_retry.py` (deterministic injection) and by the new
`test_cancel_lock_order.py` (order pinning).

## 11. What is NOT verified — blocked or owner-gated (do not read around this)

1. **Combined CI on the integrated branch — NOT RUN.** CI runs in GitHub
   Actions on pushed branches; this task forbids pushing/publishing. The
   mandatory combined gate (full suite + PG 17.11 service + Chromium 159 E2E +
   `ruff` on the merged tree) therefore **has not executed**. The branch is
   **NOT FULLY VERIFIED and must not be described as merge-ready.** PR #47's
   own CI (`f6e77c9`, 1983/6/0, PG 17.11 + Chromium) and `main`'s CI are prior
   signals only — this is not a substitute for a combined-CI run on the
   integrated tree.
2. **PostgreSQL 17 locally** — unavailable (16.2 only). PG-17-specific
   behavior (e.g. lock/visibility edge cases) is untested here; CI's PG 17
   service is the intended gate.
3. **Chromium / Studio browser E2E** — unavailable locally; 37 journeys
   skipped locally; CI installs Chromium and runs them. Not run, not claimed.
4. **Live OpenHands acceptance** — not run; no credentials were used or
   requested; PR #47's live evidence remains attributed to its branch session.
5. **Production deploy, production data, publish of any kind** — not performed;
   requires owner authorization.
6. **Two-live-account failover; Section F live dev task** — still BLOCKED
   (external credentials), unchanged, not marked complete anywhere.
7. **Global deadlock freedom** — not claimed (§7); PostgreSQL remains the
   backstop; an unobserved ordering would surface, not vanish.

## 12. Exact recommended GitHub merge sequence (NOT executed — owner gate)

1. **Owner decision:** approve pushing `arena/71f72168-python-dispetcher` and
   opening a PR into `main` (suggested title: *PR #46 → PR #47 integration:
   restart/duplicate runs, deadlock fixes*). Until then: stop here.
2. On push, GitHub Actions (`tests` workflow, PG 17.11 + Chromium) runs the
   **combined CI** on the integrated tree. This is the mandatory gate: it must
   be **green** (full suite, browser E2E 37/37, lint) before any merge. Do not
   merge on queued/cancelled/failing runs; do not merge on PG-16-only evidence.
3. **Merge with a merge commit** (`gh pr merge <n> --merge`). Do **not**
   squash or rebase — preserves PR #46/#47 commit identity and makes PR #47's
   head (`f6e77c9`) an ancestor of `main`.
4. After the merge: confirm `main` advanced; **PR #47 flips to MERGED
   automatically** (GitHub computes merge state by commits); verify it closed
   as merged and that its checks stay green; **do not** merge PR #47
   separately (its branch is `DIRTY` vs current `main` by construction — the
   conflicts are exactly what this branch resolves); do not delete any branch,
   tag, or worktree.
5. Rollback, if needed: `git revert -m 1 <merge-sha>` (reopens the same code
   paths; nothing else depends on the merge).
6. Post-merge (owner's call): run the live acceptance pack from
   `docs/NEXUS_ACCEPTANCE.md` with owner credentials; then close the external
   blockers (second OpenHands key; GitHub provider token) if available.
7. Report the CI outcome back; this document's "NOT FULLY VERIFIED" verdict
   upgrades only when the combined CI is green.

## 13. Remaining limitations and external blockers

- Combined CI: **not run** (blocked, §11). PG 17: **not run locally** (§11).
  Chromium E2E: **not run locally** (§11).
- F-06 residual class: closed structurally for the documented inversion; no
  global proof (§7).
- MCP **client**, **Self-QA engine**, **iOS client**: NOT IMPLEMENTED (design
  only) — unchanged, out of scope.
- Studio screens for File Lab / evidence / allowlist / account management:
  IMPLEMENTED — NOT LIVE VERIFIED (routes exist; no screens) — unchanged.
- Office-document preview: NOT IMPLEMENTED (download-only; UI says so).
- Docker image build: IMPLEMENTED — NOT LIVE VERIFIED (no daemon here).
- Two-live-account failover; Section F live dev task: BLOCKED (external
  credentials) — unchanged, not marked complete.
- Render free-tier sleeps without traffic (documented in `docs/`).
- PG-17-specific semantics unverified locally; see §11.

## 14. Appendix — provenance and evidence paths

**SHAs.** Starting `main`: `66291614ac2c47b33e825312c9f165f7b38de5cd`.
PR #46 head (merged): `397e1de0caf6…` (now `main`); merge commit `6629161`.
PR #47 head (unchanged): `f6e77c9682eb7df296a68f6720e85d89de01f5c7`.
Integration commits: `ec0d553` (merge) → `2fbdcad` (route-auth) → `e2c7575`
(cancel settlement order + test) → `ce21e47` (docs). **Code tip: `ce21e47`**
(`ce21e474a485edb8525f0a9091b5f2bb030c026a`); this report and the final
doc touch-ups land together in one docs commit directly after it — see
`git log -1` for its SHA. No code changes follow `ce21e47`.

**Logs / artifacts.** Full suite: `/home/user/pytest_integration_final.log`
(1951/49/0, 2116.03 s). Stress: `/tmp/stress_rep_1..30.log` (+ `/home/user/
stress_failure_rep_*.log` if ever created — none exist). Compatibility
review: `/home/user/python-dispetcher/PR46_PR47_COMPATIBILITY_REVIEW.md`
(verdict COMPATIBLE WITH CONDITIONS, order #46→#47 — executed as written).
PG cluster: `/home/user/pgdata-nexus` (PG 16.2, pgserver). Not committed to the
repository.

**Reproduction.** `TEST_SUPABASE_DB_URL='postgresql://postgres:@/postgres?host=/home/user/pgdata-nexus' /home/user/venv-nexus/bin/python -m pytest tests/ -q`
then `/tmp/run_stress.sh` (30 reps of the §10 selection). CI evidence
(PG 17.11 + Chromium) is produced only after the owner authorizes the push.

**Explicit non-claims.** This report does not claim: combined-CI green, PG 17
verification, Chromium E2E, live-service acceptance, production deploy,
global deadlock freedom, or that PR #47 is mergeable as-is. All are either
owner-gated or environment-blocked, and are listed as such in §11.

**Owner authorization requested:** (1) push permission for the combined CI;
(2) approval to execute the §12 sequence; (3) confirmation of docs wording for
release communication; (4) explicit go/no-go for any deploy (none performed).
Until (1)–(2) are granted and green, this branch stays local and unpublished.
