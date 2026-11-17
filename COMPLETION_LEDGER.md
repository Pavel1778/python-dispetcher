# NEXUS — Completion Ledger

Status of every requirement in the "Final Completion Program" against the actual
repository at `main` (base `6629161`, PR #46 merge; before that, base `4282489`
plus this session's branch `fix/approval-notification`). Legend:

- **VERIFIED** — implemented and proven (test, live run, or both).
- **IMPLEMENTED — NOT LIVE VERIFIED** — real code and tests, proven only against
  the local stand-in servers, not the live service.
- **BLOCKED — EXTERNAL DEPENDENCY** — cannot be exercised here.
- **NOT IMPLEMENTED** — absent.
- **FAILED** — present but broken.

Last updated: 2026-11-16 (reconciled 2026-11-16 for the PR #46 → PR #47
integration on branch `arena/71f72168-python-dispetcher`).

---

## Phase 1 — Repository health

| Requirement | Status | Evidence |
|---|---|---|
| Git clean, `main` = `4282489` | VERIFIED (historical, fix/approval-notification session) | `git status`, `git ls-remote` |
| `main` = `6629161` (PR #46 merged) | VERIFIED | `git ls-remote origin main`; PR #46 state MERGED 2026-11-16T22:36:31Z |
| Migrations `0001`–`0040` (41 files, gaps 0012/0013 reserved) | VERIFIED | `ls migrations/` |
| Full suite on fresh PostgreSQL 17 | VERIFIED (historical, fix/approval-notification session; see `FINAL_RELEASE_REPORT.md` §13 and `PR46_PR47_FINAL_INTEGRATION_REPORT.md` for the integrated tree) | see report |
| `ruff check .` clean | VERIFIED (fix/approval-notification session; re-checked on this integration branch) | run this session |
| Browser E2E (Chromium) | VERIFIED (historical, fix/approval-notification session, 37/37) | `tests/test_browser_e2e.py` |

## Phase 2 — Core product

| Requirement | Status | Evidence |
|---|---|---|
| Natural-language task intake | VERIFIED | live: goal accepted, planned |
| Task classification (chat vs work) | VERIFIED | `tests/test_intent_router.py` |
| Planning and decomposition | VERIFIED | live: 1 goal → 2-task DAG |
| Dependency-aware execution | VERIFIED | live: t2 gated on t1; `tests/test_lab_*` |
| Parallel execution where safe | VERIFIED | `tests/test_durable_runs.py` |
| Retry and backoff | VERIFIED | live: `task_retry` notification observed |
| Timeouts | VERIFIED | `NEXUS_TASK_TIMEOUT_SECONDS`; `tests/test_durable_runs.py` |
| Cancellation + stop signals | VERIFIED | live: run cancelled; `tests/test_durable_runs.py` |
| Idempotency (one workflow/Sr/run per request) | VERIFIED | `tests/test_duplicate_requests.py` |
| Lease management + fencing tokens | VERIFIED | `tests/test_race_protection.py` |
| Crash recovery, dead-worker recovery | VERIFIED | `tests/test_start_handover.py`, `test_lab_worker_process.py` |
| Result aggregation + final report | VERIFIED | `tests/test_run_reports.py`, `test_pipeline_e2e.py` |
| No silent task loss; durable state/history | VERIFIED | every task row + execution history |
| OpenHands auth, create, dispatch, poll, result, cleanup | VERIFIED | live direct-transport run this session |
| Failure classification | VERIFIED | `classification.retryable` honoured |
| Runtime inventory (engine, CLI, API, tests) | VERIFIED | `tests/test_openhands_inventory.py` (20 cases) |
| Multi-account slots, health, selection, failover | VERIFIED | `tests/test_openhands_pool.py`, `test_openhands_failover.py` |
| Two-**live**-account failover | BLOCKED — EXTERNAL DEPENDENCY | one key only; fakes+PostgreSQL prove the logic |
| Project isolation (404, not 403) | VERIFIED | `tests/test_route_auth.py`, `test_conversations.py` |
| Role enforcement, API boundaries | VERIFIED | `tests/test_security_checks.py`, `test_route_auth.py` |
| File Lab (upload, ZIP safety, browse, download, batch) | VERIFIED | `tests/test_file_lab.py`, `test_archive_safety.py` |
| Uploads not auto-exposed to agents | VERIFIED | files are read through the GitHub API at the bound ref |
| Verification/evidence engine (Ed25519) | VERIFIED | `tests/test_evidence_trust.py`, `test_lab_evidence.py` |
| Never VERIFIED without evidence | VERIFIED | `GitVerifier`; live task `completed_unverified` |
| Notifications: started/completed/failed/blocked/approval | VERIFIED | **this session** added `task_started`, `run_blocked`, `approval_required`; `goal_*`, `task_*`, `task_retry`, `run_report` existed |

## Phase 3 — Interface

| Requirement | Status | Evidence |
|---|---|---|
| Studio: project switcher, chat, workspace, files, agent | VERIFIED | `tests/test_browser_e2e.py` (37) |
| Responsive desktop / tablet / mobile | VERIFIED | Chromium journeys at six widths |
| No dead controls / placeholder buttons | VERIFIED | `tests/test_studio_ui.py` (handler↔control↔icon) |
| Honest pause semantics | VERIFIED | this repo's pause-honesty fix; run card says so |
| Profiler in the run view | VERIFIED | `tests/test_run_profiler.py`; live profile read |
| Notifications centre (read/unread/delete) | VERIFIED | `tests/test_lab_http.py`, browser E2E |
| Features with routes but no screen (File Lab UI, evidence panel, allowlist editor, account management) | IMPLEMENTED — NOT LIVE VERIFIED | reachable via API/CLI/MCP; documented in `docs/KNOWN_LIMITATIONS.md` |

## Phase 4 — CLI

| Requirement | Status | Evidence |
|---|---|---|
| Auth/config, projects, tasks, runs, status, logs, cancel, results, health, inventory, diagnostics | VERIFIED | `nexus … --help`; `tests/test_cli_mcp.py`; live `doctor`/`status`/`project list` |
| Works against the real API (thin client) | VERIFIED | live run this session |
| No second orchestration engine in the CLI | VERIFIED | `orchestration/cli.py` is HTTP-only |
| `nexus run restart` / `nexus run duplicate` (PR #47) | VERIFIED (integration branch, this merge) | `orchestration/cli.py`; `tests/test_cli_mcp.py`; `tests/test_run_history.py` |

## Phase 5 — Resilience and performance

| Requirement | Status | Evidence |
|---|---|---|
| Restart / worker restart / crash / SIGKILL recovery | VERIFIED | `tests/test_start_handover.py`, `test_lab_worker_process.py` |
| DB interruption / reconnect | VERIFIED | `tests/test_store_reconnect.py` |
| Rate limiting (429) → postpone, no attempt spent | VERIFIED | `tests/test_openhands_failover.py` |
| Duplicate submission absorbed | VERIFIED | `tests/test_duplicate_requests.py` |
| Concurrent projects / stale leases | VERIFIED | `tests/test_race_protection.py` |
| No duplicate finalization, no stuck executions, no unbounded retries | VERIFIED | `tests/test_durable_runs.py`, `test_run_reports.py` |
| Performance benchmark | VERIFIED | `tools/perf_bench.py`; see report |
| Cancel↔reclaim lock-order inversion (`reclaim_expired` run-row-first `FOR UPDATE`) | VERIFIED (PR #47 lock order, retained unchanged); integration stress re-run on this branch | `orchestration/nexus_lab_store.py`; `tests/test_run_history.py`; stress iteration counts in `PR46_PR47_FINAL_INTEGRATION_REPORT.md` |
| Transient `DeadlockDetected` safety net (`set_run_state`/`set_run_state_internal`, whole-tx, max 1 retry, rollback first, idempotent ops) | VERIFIED (PR #46, merged as `6629161`; retained unchanged) | `tests/test_deadlock_retry.py`; deadlock injection 2/2 |
| Cancel↔worker-settlement task↔executions ordering inversion (F-06) | **RESOLVED structurally 2026-11-16** — cancel now blocks unfinished tasks **before** closing their executions (commit `e2c7575`); every worker settle takes the same task-before-executions order. Pinned by `tests/test_cancel_lock_order.py` (deterministic: passes with the fix, fails with `LockNotAvailable` on the old order). Not a global deadlock-elimination claim | `orchestration/nexus_lab_store.py`; `tests/test_cancel_lock_order.py` |

## Phase 6 — Deployment readiness

| Requirement | Status | Evidence |
|---|---|---|
| Env vars, secrets, DB, migrations, start, health/readiness | VERIFIED | `DEPLOY.md`, `docs/ENVIRONMENT.md`, live `/healthz`+`/readyz` |
| Worker lifecycle | VERIFIED | `render.yaml`, `docker-compose.yml`, systemd unit; `tests/test_worker_setup.py` |
| Backup/restore drill | VERIFIED | `tests/test_backup_restore.py` (real pg_dump/pg_restore) |
| Upgrade / rollback procedure | VERIFIED | `DEPLOY.md` |
| Wheel builds with all assets | VERIFIED | built this session; studio+pwa present |
| Docker image build | IMPLEMENTED — NOT LIVE VERIFIED | Dockerfile present; daemon unavailable in this sandbox |
| Production deploy | NOT PERFORMED | needs owner authorization |

## Phase 7 — Security

| Area | Status | Evidence |
|---|---|---|
| Authentication (session + bearer) | VERIFIED | `tests/test_auth.py` |
| Authorization / project isolation | VERIFIED | `tests/test_route_auth.py` (restart/duplicate POST routes added to `PROTECTED_POST` 2026-11-16) |
| Secret handling / no key leak | VERIFIED | `tests/test_github_client.py`, `test_openhands_*` |
| SSRF guard (resolve + re-check, redirects refused) | VERIFIED | `tests/test_security_checks.py` |
| Archive extraction (traversal, bomb, size) | VERIFIED | `tests/test_archive_safety.py` |
| SQL injection (parameterised; column lists are constants) | VERIFIED | audit this session |
| Session handling (sliding TTL, credential-bound) | VERIFIED | `tests/test_auth.py` |
| Secrets forwarded to the sandbox (was dropped) | VERIFIED | **fixed this session**; `test_cloud_direct.py` |
| New restart/duplicate endpoints project-scoped, member-only | VERIFIED | `tests/test_run_history.py::test_restart_and_duplicate_are_member_only`; non-member gets 404 |

## Phase 8 — This session's changes

| Change | Status | Test |
|---|---|---|
| `approval_required` notification when a plan awaits confirmation | VERIFIED | `test_chat_workflow.py::test_a_manual_order_raises_an_approval_notification` |
| `task_started` notification when a task actually starts | VERIFIED | `test_lab_http.py::test_full_autonomous_loop_completes_over_http` |
| `run_blocked` notification when a run needs a person | VERIFIED | `test_durable_runs.py::test_a_blocked_run_raises_a_notification` |
| Direct transport forwards `secrets` (was silently dropped) | VERIFIED | `test_cloud_direct.py::test_secrets_reach_the_sandbox_create_body` |
| `test_lab_inline` rework-state read made deterministic | VERIFIED | the test, 3× |

## Phase 9 — PR #46 / PR #47 integration (2026-11-16)

Base for the integration: current `main` = `6629161` (PR #46 merged; includes
PR #48 / approval-notification work and PR #46's F-01 file:// guard + F-06
bounded `DeadlockDetected` retry).

| Requirement | Status | Evidence |
|---|---|---|
| PR #46 code present in `main` before integration (F-01 guard, F-06 retry, `_is_deadlock`, `tests/test_deadlock_retry.py`, `tests/test_cli_client.py`) | VERIFIED (this integration branch) | `grep -n "_is_deadlock" orchestration/nexus_lab_store.py` (lines 73, 523, 843); `tests/test_deadlock_retry.py` 2 passed; `tests/test_cli_client.py` passed; `grep -n "urlsplit" orchestration/cli.py` (scheme guard) |
| PR #47 restart_run / duplicate_run integrated without rewrite | VERIFIED (code merged unchanged except the import-conflict hunk) | `orchestration/nexus_lab_store.py` (`restart_run`, `duplicate_run`, `_plan_from_run`, `_insert_run_with_plan`) |
| Restart/duplicate invariant: plan+DAG preserved; tasks `pending` @ attempt 0; old run kept as history; duplicate creates independent goal+run; idempotent with key; one active run per goal | VERIFIED (this integration branch) | `tests/test_run_history.py` (12 tests); `uq_nexus_runs_active_goal` partial unique index (`migrations/0018_nexus_lab_runs.sql`) |
| MCP `restart_run` / `duplicate_run` (`SCOPE_RUN_CONTROL`) + CLI `run restart|duplicate` + studio «Перезапустить»/«Дублировать» | VERIFIED (integration) | `tests/test_cli_mcp.py`; `orchestration/mcp_server.py`; `orchestration/studio/app.js` |
| Route auth: restart/duplicate POST in `PROTECTED_POST`; unauthenticated 401; member-only (404 for non-member) | VERIFIED (this integration branch) | `tests/test_route_auth.py`; `tests/test_run_history.py::test_restart_and_duplicate_are_member_only` |
| Both deadlock protections retained (no revert, no removal) | VERIFIED (code inspected; both present) | §16 of `FINAL_RELEASE_REPORT.md`; `PR46_PR47_FINAL_INTEGRATION_REPORT.md` |
| Structural close of the cancel↔worker-settlement task↔executions inversion (F-06) | VERIFIED — `e2c7575`; regression test pins the order (old order → `LockNotAvailable`, new order → pass) | `orchestration/nexus_lab_store.py`; `tests/test_cancel_lock_order.py` |
| Lock-order table from actual SQL (run → tasks → executions) | see `PR46_PR47_FINAL_INTEGRATION_REPORT.md` | integration report |
| Full suite on the integrated tree (PG 16.2) | **VERIFIED LOCALLY** — 1951 passed, 49 skipped, 0 failed (35:16) | `pytest tests/ -q`; `/home/user/pytest_integration_final.log`; `PR46_PR47_FINAL_INTEGRATION_REPORT.md` |
| Combined CI on the integrated branch | NOT RUN at the time this ledger was written (requires push; owner gate) | integration report §CI |
| PostgreSQL 17 locally | NOT AVAILABLE (sandbox has PG 16.2) — PG 17 runs in CI | integration report |
| Browser E2E on the integrated tree | NOT RE-RUN here (no Chromium in sandbox) | integration report |
| Production deploy | NOT PERFORMED — owner gate | — |

## Known BLOCKED / NOT IMPLEMENTED (unchanged, documented)

| Item | Status |
|---|---|
| Two-live-account failover | BLOCKED — EXTERNAL DEPENDENCY (one key) |
| Section F live development task on the live service | BLOCKED — needs a live sandbox + GitHub provider token on the account |
| MCP **client** (NEXUS → external MCP servers) | NOT IMPLEMENTED (design only; the MCP **server** is implemented) |
| Self-QA engine | NOT IMPLEMENTED (design only) |
| Studio screens for File Lab / evidence / allowlist / accounts | IMPLEMENTED — NOT LIVE VERIFIED (routes exist; no screen) |
| PDF/DOCX/XLSX/PPTX inline preview | NOT IMPLEMENTED (offered as download; UI says so) |

## Reconciliation record — what was retained, updated, discarded, and why

Baseline for this reconciliation: the `main` (post PR #48) version of this file.
PR #47's version was read as a source of facts, not as structure.

**RETAINED (main baseline, PR #48 content):** every phase table above as it
existed at `main`; all BLOCKED / NOT IMPLEMENTED items exactly as before
(none were promoted); the historical Phase 8 table; the legend and tone of the
ledger; PG 17 full-suite evidence attributed to the fix/approval-notification
session; `fix/approval-notification`-session live-run evidence.

**RETAINED (from PR #47):** the gap-analysis conclusion that restart/duplicate
were the genuinely missing pieces; the restart/duplicate feature semantics
(plan+DAG preserved; tasks `pending` @ attempt 0; old run = history; duplicate
= new goal+run, idempotent with key; refused while the goal has an active run
and while the run is not over; `SCOPE_RUN_CONTROL` MCP tools; member-only);
the deadlock root-cause description (complemented by #46's retry, not replaced);
PR #47's own test evidence (labeled as that branch's environment). PR #47's
branch-session deadlock stress numbers (0/12 after fix; reproduced 1/6 before)
are retained as that branch's evidence, not re-attributed to this integration.

**UPDATED:** header (reconciliation note, `main` SHA `4282489` → `6629161`);
Phase 1 first rows (current `main` recorded, historical kept labeled); Phase 4
(PR #47 CLI rows); Phase 5 (both deadlock protections and the F-06 row —
later closed structurally, see Phase 9);
Phase 7 (restart/duplicate in route-auth + member-only rows); new Phase 9
(integration evidence). Every number that could not be reproduced here is
either attributed to its original environment or explicitly marked not-run.

**DISCARDED (stale or unsupported):** PR #47's banner `Base: main @ 4282489
(PR #45)`; PR #47's `1980 passed` presented as the suite total for the
integrated tree (kept only as labeled branch evidence); PR #47's implication
that the deadlock was eliminated by the lock reorder alone (it is one
structural fix plus one bounded retry — and, in this integration, one more
structural fix in the cancel path; any residual ordering is documented, never
claimed eliminated); any framing that the integration had been fully verified
at the time of writing (it had not); PR #47's `0/12` deadlock stress as if
measured here (the integration's own stress results are reported in
`PR46_PR47_FINAL_INTEGRATION_REPORT.md` with exact iteration counts).

**NEVER MARKED COMPLETE:** two-live-account failover; Section F live
development task; production deploy; MCP client; Self-QA engine; studio
screens without a live pass; Docker image build (not live verified); PG 17
verification of the integrated tree; combined CI; Chromium E2E of the
integrated tree.
