# NEXUS — Completion Ledger

Status of every requirement in the "Final Completion Program" against the actual
repository at `main` (base `4282489`) plus this session's branch
`fix/approval-notification`. Legend:

- **VERIFIED** — implemented and proven (test, live run, or both).
- **IMPLEMENTED — NOT LIVE VERIFIED** — real code and tests, proven only against
  the local stand-in servers, not the live service.
- **BLOCKED — EXTERNAL DEPENDENCY** — cannot be exercised here.
- **NOT IMPLEMENTED** — absent.
- **FAILED** — present but broken.

Last updated: 2026-11-16.

---

## Phase 1 — Repository health

| Requirement | Status | Evidence |
|---|---|---|
| Git clean, `main` = `4282489` | VERIFIED | `git status`, `git ls-remote` |
| Migrations `0001`–`0040` (41 files, gaps 0012/0013 reserved) | VERIFIED | `ls migrations/` |
| Full suite on fresh PostgreSQL 17 | VERIFIED | see "Test results" in `FINAL_RELEASE_REPORT.md` |
| `ruff check .` clean | VERIFIED | run this session |
| Browser E2E (Chromium) | VERIFIED | 37/37 this session |

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
| Authorization / project isolation | VERIFIED | `tests/test_route_auth.py` |
| Secret handling / no key leak | VERIFIED | `tests/test_github_client.py`, `test_openhands_*` |
| SSRF guard (resolve + re-check, redirects refused) | VERIFIED | `tests/test_security_checks.py` |
| Archive extraction (traversal, bomb, size) | VERIFIED | `tests/test_archive_safety.py` |
| SQL injection (parameterised; column lists are constants) | VERIFIED | audit this session |
| Session handling (sliding TTL, credential-bound) | VERIFIED | `tests/test_auth.py` |
| Secrets forwarded to the sandbox (was dropped) | VERIFIED | **fixed this session**; `test_cloud_direct.py` |

## Phase 8 — This session's changes

| Change | Status | Test |
|---|---|---|
| `approval_required` notification when a plan awaits confirmation | VERIFIED | `test_chat_workflow.py::test_a_manual_order_raises_an_approval_notification` |
| `task_started` notification when a task actually starts | VERIFIED | `test_lab_http.py::test_full_autonomous_loop_completes_over_http` |
| `run_blocked` notification when a run needs a person | VERIFIED | `test_durable_runs.py::test_a_blocked_run_raises_a_notification` |
| Direct transport forwards `secrets` (was silently dropped) | VERIFIED | `test_cloud_direct.py::test_secrets_reach_the_sandbox_create_body` |
| `test_lab_inline` rework-state read made deterministic | VERIFIED | the test, 3× |

## Phase 9 — Restart / Duplicate Run and the cancel/reclaim deadlock (integrated)

| Change | Status | Evidence |
|---|---|---|
| Restart Run (`LabStore.restart_run`) — re-run a finished run's whole plan as a new run under the same goal; every task re-created `pending` at attempt 0; the old run stays as history | VERIFIED | `tests/test_run_history.py`; route `POST .../runs/{run_id}/restart`, CLI `nexus run restart`, MCP `restart_run`, studio «Перезапустить» |
| Duplicate Run (`LabStore.duplicate_run`) — copy a finished run's plan into a new goal and run; idempotent with a key | VERIFIED | `tests/test_run_history.py`; route `POST .../runs/{run_id}/duplicate`, CLI `nexus run duplicate`, MCP `duplicate_run`, studio «Дублировать» |
| Cancel/reclaim **deadlock** (load-sensitive) | VERIFIED — fixed | `reclaim_expired` locked the run row without `FOR UPDATE` then task rows; a cancel locks the run first then tasks, so the orders inverted under load. Reproduced 1/6 on clean main; 0/12 after the fix (`tests/test_deadlock_retry.py`, `test_run_history.py`) |

### The sprint detail, as delivered

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

## Known BLOCKED / NOT IMPLEMENTED (unchanged, documented)

| Item | Status |
|---|---|
| Two-live-account failover | BLOCKED — EXTERNAL DEPENDENCY (one key) |
| Section F live development task on the live service | **VERIFIED (2026-11-16)** — a full autonomous run reached `completed` with both tasks `completed_verified` against live OpenHands (see `FINAL_RELEASE_REPORT.md` §16) |
| MCP **client** (NEXUS → external MCP servers) | NOT IMPLEMENTED — and **not needed** by any current flow (NEXUS is the MCP server; a conversation's MCP servers are reached by the agent-server, not NEXUS) |
| Self-QA engine | **IMPLEMENTED (deterministic)** — `orchestration/nexus_selfqa.py`, `nexus qa run`; not yet an automatic run gate, and does not judge intent |
| Studio screens for File Lab / evidence / allowlist / accounts | IMPLEMENTED — NOT LIVE VERIFIED (routes exist; no screen) |
| PDF/DOCX/XLSX/PPTX inline preview | NOT IMPLEMENTED (offered as download; UI says so) |
