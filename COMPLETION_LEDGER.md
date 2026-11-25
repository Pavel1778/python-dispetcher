# NEXUS — Completion Ledger

## 2026-11-25 — event-driven RFC (proposed, not implemented)

- **Status:** awaiting customer approval in [PR #61](https://github.com/Pavel1778/python-dispetcher/pull/61) before implementation;
  phase 1 is not complete. [RFC-001](docs/RFC-001_EVENT_DRIVEN.md) inventories
  dispatcher, lab, planning and external-service polling and describes a
  bounded, recoverable LISTEN/NOTIFY design. No event-driven runtime change or
  migration is claimed by this entry.
- **Verification:** on the working tree, `.venv/bin/python -m pytest -q
  -p no:cacheprovider` completed with 597 passed, 1783 skipped, 0 failed;
  `.venv/bin/ruff check .` passed. These runs include existing uncommitted
  dispatcher changes and are not a PostgreSQL integration or E2E result.
  PostgreSQL-dependent tests could not be verified locally.
- **Acceptance still open:** p99 API read latency <50 ms, 100+ parallel agents,
  production LISTEN/NOTIFY, and all other requested phases require separate
  implementation and measurements. There is no live self-healing example.

Status of every requirement in the "Final Completion Program" against the actual
repository at `main` (current `c0f49fcfb91e5ced07d372c2a8743503d3bfde1e`,
PR #50 reconstruction-merge; before that, PR #49 `6e5c2b4`, PR #46 `6629161`)
plus the reconstructed integration that PR #50 carried.
Legend:

- **VERIFIED** — implemented and proven (test, live run, or both).
- **IMPLEMENTED — NOT LIVE VERIFIED** — real code and tests, proven only against
  the local stand-in servers, not the live service.
- **BLOCKED — EXTERNAL DEPENDENCY** — cannot be exercised here.
- **NOT IMPLEMENTED** — absent.
- **FAILED** — present but broken.

Last updated: 2026-11-18 (PR #50 merged as `c0f49fc`; this branch adds `0041` claim-path indexes, single-VM stack docs, write-gate guard).

---

## Phase 1 — Repository health

| Requirement | Status | Evidence |
|---|---|---|
| `main` = `4282489` (PR #45) | VERIFIED (historical, fix/approval-notification session) | `git log`, `git ls-remote` |
| `main` = `6629161` (PR #46 merged) | VERIFIED | PR #46 state MERGED 2026-11-16T22:36:31Z |
| `main` = `6e5c2b4` (PR #49 merged) | VERIFIED | PR #49 state MERGED 2026-11-17T09:24:41Z |
| `main` = `c0f49fc` (PR #50 merged — reconstruction re-delivery) | VERIFIED | PR #50 state MERGED 2026-11-18T16:46:28Z; combined CI `test` pass 31m20s (run 44151435593) |
| Migrations `0001`–`0041` (42 files, gaps 0012/0013 reserved; `0041` = claim-path indexes) | VERIFIED | `ls migrations/` |
| Full suite on fresh PostgreSQL 17 | VERIFIED (historical, fix/approval-notification session; see `FINAL_RELEASE_REPORT.md` §13 and §18 for the reconstructed tree) | see report |
| `ruff check .` clean | VERIFIED (fix/approval-notification session; re-run under this recovery — see `reports/INTEGRATION_RECOVERY_EVIDENCE.md`) | this branch |
| Browser E2E (Chromium) | VERIFIED (historical, release/acceptance session, 37/37) | `tests/test_browser_e2e.py` |

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
| Features with routes but no screen (File Lab UI, evidence panel, allowlist editor, account management) | IMPLEMENTED — NOT LIVE VERIFIED | reachable via API/CLI/MCP; documented in `docs/KNOWN_LIMITATIONS.md` (account management **is** on screen as of PR #49) |

## Phase 4 — CLI

| Requirement | Status | Evidence |
|---|---|---|
| Auth/config, projects, tasks, runs, status, logs, cancel, results, health, inventory, diagnostics | VERIFIED | `nexus … --help`; `tests/test_cli_mcp.py`; live `doctor`/`status`/`project list` |
| Works against the real API (thin client) | VERIFIED | live run this session |
| No second orchestration engine in the CLI | VERIFIED | `orchestration/cli.py` is HTTP-only |
| `nexus run restart` / `nexus run duplicate` (PR #47) | VERIFIED (integrated on this branch) | `orchestration/cli.py`; `tests/test_cli_mcp.py`; `tests/test_run_history.py` |

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
| Cancel↔reclaim lock-order inversion (`reclaim_expired` run-row-first `FOR UPDATE`) | VERIFIED (PR #47 lock order, retained unchanged) | `orchestration/nexus_lab_store.py`; `tests/test_run_history.py` |
| Transient `DeadlockDetected` safety net (`set_run_state`/`set_run_state_internal`, whole-tx, max 1 retry, rollback first, idempotent ops) | VERIFIED (PR #46, merged as `6629161`; retained unchanged) | `tests/test_deadlock_retry.py` |
| Cancel↔worker-settlement task↔executions ordering inversion (F-06) | **RESOLVED structurally — cancel now blocks unfinished tasks before closing their executions**; pinned by `tests/test_cancel_lock_order.py` (old order → `LockNotAvailable`, fixed order → passes). Not a global deadlock-elimination claim | `orchestration/nexus_lab_store.py`; `tests/test_cancel_lock_order.py` |

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
| Authorization / project isolation | VERIFIED | `tests/test_route_auth.py` (restart/duplicate POST routes added to `PROTECTED_POST` with this integration) |
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

## Phase 9 — PR #46 / PR #47 integration (reconstructed 2026-11-17)

Base for the integration: current `main` = `6e5c2b4` (merge of PR #49; includes
PR #48 approval-notifications, PR #46 F-01 `file://` guard + F-06 bounded
`DeadlockDetected` retry, and PR #49 release acceptance: live-verified run,
Git credential injection, bound-branch dispatch, Self-QA engine, USER_GUIDE).
The integration itself was reconstructed on this branch after the original
integration commits (`ec0d553`…`baa3251`) were lost with a pre-reset sandbox;
it is a **new integration**, not the original history — see
`FINAL_RELEASE_REPORT.md` §18 and `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md`.

| Requirement | Status | Evidence |
|---|---|---|
| PR #46 code present in `main` (F-01 guard, F-06 retry, `_is_deadlock`, `tests/test_deadlock_retry.py`, `tests/test_cli_client.py`) | VERIFIED | `grep -n "_is_deadlock" orchestration/nexus_lab_store.py`; `tests/test_deadlock_retry.py` |
| PR #47 restart_run / duplicate_run integrated without rewrite | VERIFIED (code carried over by merge of `f6e77c9`; only the recorded import hunk needed manual resolution) | `orchestration/nexus_lab_store.py` (`restart_run`, `duplicate_run`, `_plan_from_run`, `_insert_run_with_plan`) |
| Restart/duplicate invariant: plan+DAG preserved; tasks `pending` @ attempt 0; old run kept as history; duplicate creates independent goal+run; idempotent with key; one active run per goal | VERIFIED | `tests/test_run_history.py` (12 tests); `uq_nexus_runs_active_goal` partial unique index (`migrations/0018_nexus_lab_runs.sql`) |
| MCP `restart_run` / `duplicate_run` (`SCOPE_RUN_CONTROL`) + CLI `run restart\|duplicate` + Studio «Перезапустить»/«Дублировать» | VERIFIED | `tests/test_cli_mcp.py`; `orchestration/mcp_server.py`; `orchestration/studio/app.js` |
| Route auth: restart/duplicate POST in `PROTECTED_POST`; unauthenticated 401; member-only (404 for non-member) | VERIFIED | `tests/test_route_auth.py`; `tests/test_run_history.py::test_restart_and_duplicate_are_member_only` |
| Both deadlock protections retained (no revert, no removal) | VERIFIED (code inspected; both present) | `FINAL_RELEASE_REPORT.md` §18 |
| Structural close of the cancel↔worker-settlement task↔executions inversion (F-06) | VERIFIED — pinned by regression test; old order → `LockNotAvailable`, new order → pass | `orchestration/nexus_lab_store.py`; `tests/test_cancel_lock_order.py` |
| Lock-order table from actual SQL (run → tasks → executions) | see `reports/INTEGRATION_CODE_REVIEW.md` and the recovery final report | this branch |
| Full suite on the reconstructed tree | re-run under this recovery; results and environment recorded in `reports/INTEGRATION_RECOVERY_EVIDENCE.md` (never cross-fold with other environments) | this branch |
| Combined CI on the reconstructed branch | NOT RUN at the time of writing (requires push; owner gate) | this branch |
| PostgreSQL 17 locally | environment-dependent — see `reports/INTEGRATION_RECOVERY_EVIDENCE.md` | this branch |
| Browser E2E on the reconstructed tree | NOT RE-RUN unless a Chromium binary is present (see evidence report) | this branch |
| Production deploy | NOT PERFORMED — owner gate | — |

## Known BLOCKED / NOT IMPLEMENTED (documented)

| Item | Status |
|---|---|
| Two-live-account failover | BLOCKED — EXTERNAL DEPENDENCY (one key) |
| Section F live development task on the live service | **VERIFIED (2026-11-16)** — a full autonomous run reached `completed` with both tasks `completed_verified` against live OpenHands (PR #49; see `FINAL_RELEASE_REPORT.md` §16) |
| MCP **client** (NEXUS → external MCP servers) | NOT IMPLEMENTED — and **not needed** by any current flow (NEXUS is the MCP server; a conversation's MCP servers are reached by the agent-server, not NEXUS) |
| Self-QA engine | **IMPLEMENTED (deterministic)** — `orchestration/nexus_selfqa.py`, `nexus qa run` (PR #49); not yet an automatic run gate, and does not judge intent |
| Studio screens for File Lab / evidence / allowlist / accounts | IMPLEMENTED — NOT LIVE VERIFIED (routes exist; no screen) |
| PDF/DOCX/XLSX/PPTX inline preview | NOT IMPLEMENTED (offered as download; UI says so) |
| iOS client | NOT IMPLEMENTED (design only) |

## Reconciliation record — what was retained, updated, discarded, and why

Baseline for this reconciliation: `main` @ `6e5c2b4` (post PR #49). PR #47's
document was read as a source of facts, not as structure. Because the
integration was reconstructed after the original commits were lost, this record
also lists the drift corrections the union required.

**RETAINED (main baseline, incl. PR #48 and PR #49 content):** every phase table
as it existed at `main`; the PR #49 corrections to the blocked/known-issues
tables (Section F VERIFIED via live acceptance; Self-QA IMPLEMENTED
deterministic; MCP client "not needed"); the historical Phase 8 table; the
legend and the ledger's evidence-first tone; the two external blockers that are
still open (two-live-account failover).

**RETAINED (from PR #47):** the restart/duplicate feature semantics (plan+DAG
preserved; tasks `pending` @ attempt 0; old run = history; duplicate = new
goal+run, idempotent with key; refused while the goal has an active run and
while the run is not over; `SCOPE_RUN_CONTROL` MCP tools; member-only); the
deadlock root-cause description (complemented by #46's retry, not replaced);
PR #47's own test evidence (labeled as that branch's environment); the known
limitations and external blockers.

**UPDATED:** header (base `6629161` → current `6e5c2b4`; branch named; date
2026-11-17); Phase 1 (current `main` recorded; older bases kept as history);
Phase 4 (PR #47 CLI rows); Phase 5 (both deadlock protections and the F-06
row); Phase 7 (restart/duplicate in route-auth + member-only rows); Phase 9
rewritten for the reconstruction; evidence rows now point at
`reports/INTEGRATION_RECOVERY_EVIDENCE.md`.

**Drift corrected during the reconstruction (stale even before this branch):**
this file's earlier header chained a stale base SHA (`4282489`) and described
only the fix/approval-notification session; the blocked table now reflects PR
#49 reality instead of the pre-acceptance state (Section F, Self-QA, MCP
client). The integration's earlier "Self-QA NOT IMPLEMENTED" line is discarded
— PR #49 landed a deterministic Self-QA engine.

**DISCARDED (stale or unsupported):** PR #47's banner `Base: main @ 4282489
(PR #45)`; PR #47's `1980 passed` presented as the suite total for the merged
tree (kept only as labeled branch evidence); the implication that the deadlock
was fully eliminated by the lock reorder alone (it is one structural fix plus
one bounded retry — and now one more structural fix in the cancel path;
residual ordering is documented, never claimed eliminated); any framing that
integration verification had already happened at the time of the original
write-up; the assertion that PG 17 had ever run the integrated tree.

**NEVER MARKED COMPLETE:** two-live-account failover; production deploy; MCP
client; Self-QA as an automatic run gate; studio screens without a live pass;
PostgreSQL 17 verification of the reconstructed tree unless actually run (see
evidence report); combined CI; production release.
