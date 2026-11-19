# Feature Matrix — NEXUS (audited 2026-11-18/19 on branch arena/aa1abcbc-python-dispetcher @ eff7cd2 + ab0dc09)

Legend — Status: VERIFIED (executed & green) · IMPLEMENTED_NOT_VERIFIED (code present, exercised only indirectly) · PARTIAL · BROKEN · MISSING · INTENTIONALLY_OUT_OF_SCOPE.
"Tests" column names the suite(s) that execute the behavior; "Live" notes a real-process verification.

| # | Feature | Implementation | Integration | Tests | Status |
|---|---------|----------------|-------------|-------|--------|
| 1 | Authentication (pbkdf2 login, session cookies) | `orchestration/web_auth.py` | web login, CLI bearer | `tests/test_auth.py` (scenarios, 1665 LOC) | VERIFIED (suite + live smoke: wrong pw → 401, right pw → 303) |
| 2 | Session management (renewal, sliding TTL, HttpOnly/SameSite=Strict) | `web_auth.AuthService`, cookie helpers in `web_server.py` | browser | `test_auth.py` session suites | VERIFIED |
| 3 | Authorization (single-owner login + bearer) | `web_server._require_read/_require_write`, `nexus_policy` scopes | all routes | `test_route_auth.py` (100+ probes) | VERIFIED |
| 4 | Write-gate enforcement (order: bearer/session+CSRF → routes) | `web_server.do_POST` | every write route | `tests/test_write_gate.py` (AST gate-order guard, 3 tests) | VERIFIED |
| 5 | Project isolation & RBAC (owner/member; membership-first, NotFound-no-existence) | `nexus_projects.ProjectService`, `nexus_policy.require_scope` | all project services | `tests/test_project_isolation.py` (18 probes, new), `test_nexus_projects.py`, `test_project_binding.py`, `test_run_binding.py` | VERIFIED |
| 6 | Repository isolation (default branch, allowed branches, binding) | `nexus_binding.ProjectRepositoryService` | dispatch refuse | `test_project_repositories.py`, `test_branch_write_lock.py` | VERIFIED |
| 7 | Task dispatch (projects → workflows → Sr tasks) | `control_plane.py`, `dispatcher.py`, `cloud_dispatcher.py` | queue in PG | `test_dispatch_authorization.py`, `test_dispatcher.py`, `test_e2e_dispatcher_loop.py` | VERIFIED (fake cloud/runtime) |
| 8 | Claiming & lease handling (forked-safe claims, `FOR UPDATE`) | `nexus_lab_store.claim_task`, `_BRANCH_FREE` | worker + inline loop | `test_race_protection.py`, `test_connection_concurrency.py`, `test_deadlock_retry.py` | VERIFIED (49 tests passed 2026-11-19) |
| 9 | Worker recovery (dead lease reclaim, restart mid-cycle) | `lab_worker.py`, `nexus_lab_store` leases, spec planner lease (3 s) | lab worker | `test_long_task_cases.py::test_a_restart_while_the_plan_is_being_built…` — **flaky under 2-CPU saturation**, passes in isolation (6/6); documented in KNOWN_LIMITATIONS | PARTIAL (load-sensitive) |
| 10 | Idempotency (goals, tasks, conversation messages, dispatch keys) | `IdempotencyConflict`, unique keys | API, CLI | `test_duplicate_requests.py`, `test_nexus_projects.py` (idempotency suites) | VERIFIED |
| 11 | Retry logic (bounded, exponential, `sr_start_retry`) | `nexus_retry.py`, `control_plane.py` | dispatcher | `test_sr_start_retry.py`, `test_deadlock_retry.py` | VERIFIED |
| 12 | Dead-letter handling | dead-letter tables/events in dispatcher | dispatcher | `test_dispatcher.py` (dead-letter cases) | VERIFIED (via suite) |
| 13 | Evidence generation (signed records) | `nexus_evidence.py`, `evidence_trust.py` | runs, MCP, API | `test_lab_evidence.py`, `test_evidence_trust.py` | VERIFIED |
| 14 | Verification pipeline (re-verify, attest verdict) | `workflow_verifier.py`, `nexus_evidence.reverify` | CI checks, QA | `test_lab_verifier.py`, `test_selfqa.py`, `test_workflow_actions.py` | VERIFIED (fake checks) |
| 15 | File upload & extraction (zip-slip, bombs) | `multipart.py`, `archive_safety.py`, `nexus_files.py`, `file_lab.py` | file lab | `test_archive_safety.py`, `test_file_lab.py`, `test_artifacts.py` | VERIFIED |
| 16 | GitHub operations (accounts, repos, branches, write w/ confirmation) | `github_client.py`, `github_confirmations.py` | PR creation | `test_github_client.py`, `test_github_write_http.py`, `test_github_confirmations.py` | VERIFIED (fake GitHub) |
| 17 | CLI (`nexus`: version/doctor/status/migrations/project/run/task/qa…) | `cli.py` | HTTP | `test_cli_client.py`, `test_cli_mcp.py` | VERIFIED — new: `nexus migrations` (live-verified 2026-11-19) |
| 18 | Web interface (React-less studio, PWA) | `orchestration/studio/` | app.js/markdown.js | `tests/studio_render_test.js` (passes), browser E2E (CI only) | VERIFIED render / PARTIAL browser (no local Chromium) |
| 19 | Notifications (project-scoped) | `nexus_lab_store.notifications`, chat/reports | UI bell | `test_lab_http.py` (notification cases) | IMPLEMENTED_NOT_VERIFIED end-to-end in browser |
| 20 | Logging & observability (`/healthz`, `/readyz`, `/api/observability`, tracing) | `tracing.py`, `web_server.observability()`, `openhands_diagnostics.py` | CLI `doctor`, `/api/observability` | `test_observability.py` | VERIFIED |
| 21 | Database migrations (42, idempotent, checksum-guarded) | `cloud_store.apply_migrations`, `migrations/` | startup bootstrap | `test_lifecycle_migrations.py`, `test_durable_migrations.py`, `test_migration_sql_guards.py`, `test_schema_bootstrap.py` | VERIFIED (fresh + pre-existing DBs; live smoke applied 42/42) |
| 22 | Backup & restoration (pg_dump → pg_restore) | `test_backup_restore.py`, docs | CI + manual drill | `test_backup_restore.py` (CI, populated DB) — **local drill 2026-11-19: pg_dump → restore → 51 tables, 42 migrations, 0041 indexes present; scratch DB dropped afterwards** | VERIFIED (schema-level locally; data-level in CI) |

## Live verifications actually executed (2026-11-18/19)

- Fresh PG 16.2: full suite (1974 passed / 49 skipped / 1 load-sensitive flake; CI at eff7cd2 green incl. Chromium E2E + PG 17).
- Smoke boot (`nexus_smoke2`, empty DB, port 8124): self-applied 42/42 migrations; `/healthz` 200; `/readyz` → `{"status":"ready","database":true}`; anon `/api/migrations` → 401; bearer → 200; `nexus migrations` (human+JSON) printed 42/42; `nexus doctor` all components reported; idle VmRSS ≈ 52 MB; scratch DB dropped.
- Backup/restore drill (local, suite DB): pg_dump 16.2 → fresh DB → restore exit 0 → 51 tables, 42 migrations, indexes `idx_nexus_*_repo_branch` present; scratch DB dropped.

## Known defects / limits (carry forward)

1. Flake in `test_long_task_cases.py::test_a_restart_while_the_plan_is_being_built…` under 2-CPU saturation (attempt counter assertion); passes isolated; documented.
2. Local verification asymmetry: PG 16.2 vs CI PG 17; no local Chromium → browser E2E and Studio browser flows are CI-verified only.
3. Single-owner web auth model: HTTP principals derive from the one configured login; cross-human isolation exists at the service layer (this matrix #5), not as separate web accounts.
4. Rust: no component exists (no toolchain/registry in this environment); see `docs/RUST_MIGRATION_ASSESSMENT.md` addendum 2026-11-18.
