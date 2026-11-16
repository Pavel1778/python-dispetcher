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

## Known BLOCKED / NOT IMPLEMENTED (unchanged, documented)

| Item | Status |
|---|---|
| Two-live-account failover | BLOCKED — EXTERNAL DEPENDENCY (one key) |
| Section F live development task on the live service | BLOCKED — needs a live sandbox + GitHub provider token on the account |
| MCP **client** (NEXUS → external MCP servers) | NOT IMPLEMENTED (design only; the MCP **server** is implemented) |
| Self-QA engine | NOT IMPLEMENTED (design only) |
| Studio screens for File Lab / evidence / allowlist / accounts | IMPLEMENTED — NOT LIVE VERIFIED (routes exist; no screen) |
| PDF/DOCX/XLSX/PPTX inline preview | NOT IMPLEMENTED (offered as download; UI says so) |
