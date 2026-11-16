# NEXUS — Completion Ledger (Final Delivery Sprint)

Living record of what this sprint completed. Facts and evidence, not intentions.
Base: `main` @ `42824896e388c97e218a8915cab56a8498a28473` (PR #45).
Read `CONSOLIDATION-LEDGER.md` for how `main` was assembled; this file records
what was finished **on top of** it.

## Verdict: RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS

The product is usable end to end against the local stand-in servers and against
a live OpenHands account (see `FINAL_RELEASE_REPORT.md`). The only blockers are
external credentials this sandbox does not have (a second OpenHands key for the
two-account failover; a GitHub provider token on the live account for a live
push). No code change can remove those.

## Gap analysis (what was checked, and what was actually missing)

Most of the workflow the directive names already exists and is verified. Checked
against the **actual** `main`, not the (stale) Phase-0 status board:

| Capability | Status on `main` @ 4282489 | Action this sprint |
|---|---|---|
| Task submission, planning, DAG, dependencies | VERIFIED (`test_pipeline_e2e.py`, `test_planner_worker.py`) | none |
| OpenHands dispatch, monitoring, result retrieval | VERIFIED (`test_openhands_failover.py`, live probe) | none |
| Retry / backoff / cancellation / idempotency | VERIFIED (`test_durable_runs.py`, `test_lab_inline.py`) | none |
| Worker crash recovery, stale-lease recovery | VERIFIED (`test_lab_worker_process.py`, `test_race_protection.py`) | none |
| Final report generation, execution history | VERIFIED (`test_run_reports.py`, `test_run_history.py`) | none |
| Per-run metrics / profiler | VERIFIED (migration 0040, `nexus_profiler.py`) | none |
| Notifications (read/unread, navigation) | VERIFIED (`test_lab_http.py`) | none |
| Project isolation, RBAC, MCP scopes, archive safety | VERIFIED (`test_route_auth.py`, `test_security_checks.py`) | none |
| CLI (project/task/run/status/logs/diagnostics/inventory) | VERIFIED (`test_cli_mcp.py`) | added `run restart|duplicate` |
| **Restart Run** | **NOT IMPLEMENTED** | **implemented + tested** |
| **Duplicate Run** | **NOT IMPLEMENTED** | **implemented + tested** |
| Cancel/reclaim **deadlock** (load-sensitive) | **DEFECT (reproduced 1/6)** | **fixed + stress-verified 0/12** |
| MCP *client* / Self-QA engine / iOS client | NOT IMPLEMENTED (design only) | out of scope (no feature gap in the running product) |

## Completed this sprint

### 1. Restart Run (`LabStore.restart_run`)
- Re-runs a finished run's whole plan as a **new run under the same goal**;
  every task re-created `pending` at attempt 0. The old run is untouched history.
- Refused (409) while the goal already has an active run; refused (409) when the
  run is not over.
- Surface: `POST /api/v1/projects/{id}/runs/{run_id}/restart`,
  `nexus run restart --project <p> <run>`, MCP `restart_run`, studio
  «Перезапустить» on a finished run card.

### 2. Duplicate Run (`LabStore.duplicate_run`)
- Copies a finished run's plan into a **new goal and run** (a copy cannot share
  the goal: one active run per goal). Idempotent with an `idempotency_key`.
- Surface: `POST .../runs/{run_id}/duplicate`, `nexus run duplicate`,
  MCP `duplicate_run`, studio «Дублировать».

### 3. Deadlock fix (`LabStore.reclaim_expired`)
- `reclaim_expired` read the run row **without** `FOR UPDATE` and then locked
  task rows; a cancel locks the run row first and then tasks. The two orders
  inverted and deadlocked under load (`psycopg.errors.DeadlockDetected`).
- Fixed by locking the run row first, the order every other site already uses.
- Evidence: reproduced **1/6** on clean `main`; **0/12** after the fix.

### 4. Tests (7 new)
- `tests/test_run_history.py`: restart re-runs every task; restart refuses an
  active run; duplicate makes an independent goal and run; duplicate idempotency;
  restart/duplicate are member-only.
- `tests/test_cli_mcp.py`: CLI registers `run restart`/`run duplicate`; MCP
  exposes `restart_run`/`duplicate_run` scoped `run:control`.

## Backlog (unchanged, still out of scope)
- MCP **client** (NEXUS → external MCP servers): design only
  (`docs/MCP_ARCHITECTURE.md`).
- Self-QA engine: design only (`docs/SELF_QA_ARCHITECTURE.md`).
- iOS client: not implemented.
- Unified terminal-output architecture: deliberately **two** implementations
  (run-level and task-level routes); not to be collapsed.

## Verification log
See `FINAL_RELEASE_REPORT.md` for the full gate table. Summary:
- `pytest tests/ -q` → see report (clean-main reference: 1973 passed / 9 skipped
  / 0 failed).
- `ruff check .` → All checks passed.
- `node tests/studio_render_test.js` → passed.
- Browser E2E (Chromium) → see report.
- Deadlock stress → 0/12 (was 1/6).
