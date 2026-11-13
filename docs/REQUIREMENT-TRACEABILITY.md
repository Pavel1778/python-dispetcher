# NEXUS — requirement traceability matrix

This is the verification artifact for the request
"NEXUS — OpenHands Complete Integration, Configuration & Autonomous Execution
Acceptance". Its single job is to show that **every piece of the request is
accounted for**: for each requirement it names the code (or document) that
implements it and the test that observes it, or states plainly that the piece is
a report-level requirement, a cross-cutting property, or not reachable in this
environment (BLOCKED / UNVERIFIED).

It was produced by task `t18` of NEXUS run
`a1e46e80-5c2f-401d-8da0-f38dd3d00d66`, whose plan covers the request's
safety rules, the OpenHands inventory and the multi-account pool. Where a piece
lies outside that step's slice it is still listed, so the matrix is complete and
a reader can see the whole request mapped rather than only a part of it.

The source text is the request as its author wrote it (12273 characters,
SHA-256 `a2818fd4747edc7f41f6199073dfdd43c027fc34604fc110a95c0f5952f96f45`).
Piece ids (`R-NNNN`) are the planner's, one per addressable line; the plan named
44 of them for this step. The matrix below walks the request in its own order so
nothing between them can fall through.

**How to read a status**

- **VERIFIED** — an artifact exists and a test (or a real command) observes the
  behaviour.
- **DOC** — the requirement is itself a document/instruction (a rule to follow, a
  report to produce, a list of items to inventory); the artifact is the document
  that states it.
- **BLOCKED** — the artifact exists but the behaviour cannot be exercised here
  (no second OpenHands key, no live GitHub token on the account).
- **UNVERIFIED** — implemented, not exercised in this environment.

---

## 1. Safety rules (section "1. Правила безопасности", R-0002 … R-0018)

| Piece | Requirement (line) | Artifact | Test / evidence |
|---|---|---|---|
| R-0002 | Роль | `AGENTS.md` (repository instructions), the agent's own operating brief | this session's report |
| R-0003 | Role: Senior Infra / OpenHands Integration / Backend Architect / QA | the four roles are exercised across the deliverables below | §15 report in `docs/FINAL_ACCEPTANCE_REPORT.md` |
| — (rule 1) | Determine repo, branch, HEAD, working-tree state | `git rev-parse HEAD`, `git status` | `docs/FINAL_ACCEPTANCE_REPORT.md` §1 baseline table |
| — (rule 2) | Audit architecture and current implementation | `docs/AUDIT-2026-11-12.md`, `docs/OPENHANDS_INVENTORY.md` | this matrix |
| — (rule 3) | Create a safe restore point | `orchestration/restore_point.py` | `tests/test_restore_point.py` (13 cases) |
| R-0012 | Do not switch to another project or repository | `git remote -v` → only `Pavel1778/python-dispetcher` | this session |
| R-0013 | Do not mix NEXUS with a separate "OpenHands Runtime Evolution" | no such module exists (`grep` finds the phrase only in `AGENTS.md`, as the rule itself) | this session |
| R-0014 | Do not overwrite the user's changes | work is additive; working tree was clean at start | `git status` |
| R-0015 | Do not delete working mechanisms for the sake of simplicity | every change additive; no module removed | `git diff` of this task |
| R-0016 | Do not publish API keys or tokens | `orchestration/secrets.py` (Fernet), `api_key_hint` masking, `tests/test_security_checks.py::test_masking_never_reveals_the_middle`, `::test_known_env_secrets_are_scrubbed` | those tests |
| R-0017 | No force-push, destructive migrations, irreversible Git ops | no `DROP`/`TRUNCATE`/`DELETE` in `migrations/*.sql`; push is a plain fast-forward | this session; `git log` |
| R-0018 | Do not change production without separate permission | deployment is out of scope of a code task; no deploy was triggered | this session |
| — (last) | Do not invent the OpenHands API — verify the real version, docs and endpoints | `docs/OPENHANDS_INVENTORY.md` (versions 1.87.1 / agent-server 1.60.1 from `GET /openapi.json` and `GET /server_info`) | live probe, §10 of the inventory |

---

## 2. Full OpenHands inventory (section "2. Полная инвентаризация", R-0020 … R-0039)

The inventory is `docs/OPENHANDS_INVENTORY.md` (566 lines, 13 sections). Each
listed item maps to a section of it:

| Piece | Item (line) | Inventory section |
|---|---|---|
| R-0020 | Полная инвентаризация OpenHands | the whole document |
| R-0021 | Исследуй (the list follows) | §1–§11 |
| R-0022 | OpenHands Cloud API | §1 "OpenHands Cloud API (V1)" |
| R-0023 | версия API | §0 "Versions in scope (live)" |
| R-0024 | авторизацию | §2 "Authorization" |
| R-0025 | управление conversations | §3 "Conversation management" |
| R-0026 | создание новых conversations | §3.1 "Two-phase creation", §3.2 "direct transport" |
| R-0027 | продолжение существующих conversations | §3.3 "Continuing, renaming, deleting" |
| R-0028 | отправку сообщений | §4 "Sending messages" |
| R-0029 | получение событий | §5 "Events" |
| R-0030 | получение статуса выполнения | §6 "Execution status and completion" |
| R-0031 | остановку и завершение | §7 "Stopping, finishing, runtime and workspace" |
| R-0032 | управление runtime | §7 (sandbox pause/resume/delete) |
| R-0033 | workspace | §7 (workspace read/write, file upload) |
| R-0034 | доступные инструменты | §8 "Available tools" |
| R-0035 | лимиты | §9.1 "Limits (observed / schema)" |
| R-0036 | ошибки | §9.2 "Errors" |
| R-0037 | тайм-ауты | §9.3 "Timeouts" |
| R-0038 | доступность API | §1.5 "Availability probes", §10 live verification |
| R-0039 | совместимость текущего клиента NEXUS с OpenHands | §11 "NEXUS ↔ OpenHands compatibility" + compatibility matrix |

Also required by this section (not separate pieces, but stated lines):

| Requirement | Artifact | Test |
|---|---|---|
| Проверь существующие адаптеры, клиенты, конфигурации и переменные окружения | `orchestration/cloud_client.py`, `cloud_direct.py`, `openhands_client.py`, `local_runtime.py`; `docs/ENVIRONMENT.md` | `tests/test_connectivity.py`, `tests/test_cloud_direct.py` |
| Не создавай второй параллельный клиент, если уже существует подходящий | one client surface, four transports selected by `OPENHANDS_TRANSPORT` (inventory §11) | `tests/test_cloud_direct.py` |

---

## 3. Multiple OpenHands accounts (section "3. Настройка нескольких OpenHands-аккаунтов", R-0042 … R-0056)

### 3.1 The pool exists and is not capped

| Piece | Requirement (line) | Artifact | Test |
|---|---|---|---|
| R-0042 | Настройка нескольких OpenHands-аккаунтов | `orchestration/nexus_openhands_accounts.py`, `orchestration/openhands_pool.py` | `tests/test_openhands_multi_account.py`, `tests/test_openhands_pool.py` |
| R-0043 | Реализуй полноценный OpenHands Account Pool | `AccountPool` (`openhands_pool.py`) | `tests/test_openhands_pool.py`, `tests/test_acceptance_account_pool.py` |
| R-0044 | Система должна поддерживать произвольное количество аккаунтов | no cap; one row per account | `tests/test_acceptance_account_pool.py::test_an_arbitrary_number_of_accounts_is_not_capped` (12 accounts), `::test_one_account_is_selectable`, `::test_two_accounts_are_both_supported_and_independently_verified`, `::test_three_accounts_are_supported` |
| R-0045 | Для каждого аккаунта (the field list follows) | `nexus_openhands_accounts` table + `_VIEW_COLUMNS` | `tests/test_openhands_account_admin.py` |

### 3.2 The per-account fields (each required line, R-0046 … R-0056)

All fields live on the account row and are returned by the key-free
`_VIEW_COLUMNS` shape (`orchestration/nexus_openhands_accounts.py:56`).

| Piece | Field (line) | Column / source | Test |
|---|---|---|---|
| R-0046 | уникальный ID | `id uuid` (PK) | `tests/test_openhands_account_admin.py` |
| R-0047 | имя | `label` | `tests/test_openhands_account_admin.py` |
| R-0048 | API endpoint | `base_url` | `tests/test_openhands_account_admin.py` |
| R-0049 | API key | `api_key_encrypted` (Fernet) + `api_key_hint` (masked) | `tests/test_security_checks.py::test_a_key_is_encrypted_not_plaintext`, `::test_masking_never_reveals_the_middle` |
| R-0050 | статус авторизации | `state` (`active`/`invalid`) | `tests/test_openhands_account_admin.py::` verify cases |
| R-0051 | доступность | `health` (`unknown`/`healthy`/`degraded`/`unavailable`), `connection_ok` | `tests/test_openhands_pool_http.py` |
| R-0052 | время последней проверки | `last_checked_at`, `health_checked_at`, `last_connection_at` | `tests/test_openhands_account_admin.py` |
| R-0053 | текущая нагрузка | `AccountPool.live_load` (derived from in-flight tasks) | `tests/test_openhands_pool_http.py` (observed-load case) |
| R-0054 | активные задачи | `active_tasks` (column) + live count | `tests/test_openhands_pool.py` |
| R-0055 | ошибки | `last_error`, `error_count`, `nexus_openhands_account_events` | `tests/test_openhands_account_admin.py` (error-history cases), `tests/test_openhands_pool_http.py` |
| R-0056 | ограничения | `max_concurrent_tasks`, `daily_conversation_limit` | `tests/test_openhands_account_admin.py`, `tests/test_acceptance_account_pool.py::test_load_balancing_ignores_an_account_at_its_limit` |
| — | приоритет | `priority` | `tests/test_acceptance_account_pool.py::test_priority_orders_automatic_selection` |
| — | состояние включения | `enabled` | `tests/test_acceptance_account_pool.py::test_a_disabled_account_is_never_selected` |

### 3.3 Secret handling and masking (stated lines)

| Requirement | Artifact | Test |
|---|---|---|
| Секреты хранить безопасно | `orchestration/secrets.py` (Fernet, `NEXUS_SECRET_KEY`; refuses without a key) | `tests/test_security_checks.py::test_a_key_is_encrypted_not_plaintext`, `::test_encryption_refuses_without_a_key` |
| В интерфейсе и логах показывать только маскированные значения | `api_key_hint`; log redaction filter | `tests/test_security_checks.py::test_masking_never_reveals_the_middle`, `::test_the_filter_cleans_a_log_record` |

### 3.4 The supported operations (stated lines)

| Operation | Artifact (service / route / CLI) | Test |
|---|---|---|
| добавление аккаунтов | `OpenHandsAccountService.add` / `POST /api/openhands/accounts` / `nexus openhands add` | `tests/test_openhands_account_admin.py` |
| редактирование | `.edit` / `POST …/accounts/{id}/edit` / `nexus openhands edit` | `tests/test_openhands_account_admin.py` |
| удаление с проверкой активных задач | `.remove(force=)` / `POST …/remove` | `tests/test_acceptance_account_pool.py::test_removing_an_account_with_active_tasks_is_refused_then_forced` |
| включение и отключение | pool `enable`/`disable` / `POST …/enable`, `…/disable` | `tests/test_openhands_pool_http.py` |
| проверку соединения | `.check_connection` / `POST …/connection` / `nexus openhands connection` | `tests/test_openhands_account_admin.py` |
| проверку авторизации | `.verify` / `POST …/verify` / `nexus openhands account verify` | `tests/test_openhands_account_admin.py` |
| назначение основного аккаунта | `.set_default` / `POST …/default` | `tests/test_openhands_account_admin.py` |
| назначение резервных аккаунтов | pool fallback order / `POST /api/openhands/pool` | `tests/test_acceptance_account_pool.py::test_fallback_order_is_stored_and_reported` |
| ручной выбор | pool mode `manual`; `AccountRouter` preference | `tests/test_acceptance_account_pool.py::test_manual_mode_refuses_rather_than_switching`, `::test_a_manual_switch_honours_the_named_account` |
| автоматический выбор | pool modes `auto`/`failover`/`load_balancing` | `tests/test_acceptance_account_pool.py::test_automatic_distribution_spreads_across_least_busy`, `::test_a_failed_primary_falls_over_to_the_backup` |
| Не привязывай систему к одному API-ключу или одному аккаунту | no single-key coupling; `client_for_project` never falls back | `tests/test_security_checks.py::test_a_service_never_falls_back_to_another_accounts_key` |

---

## 4. Automatic switching (section "4. Автоматическое переключение OpenHands") — out of this step's slice, mapped for completeness

The four modes and the ten failover steps are implemented in
`orchestration/openhands_pool.py`, `orchestration/openhands_router.py` and
`orchestration/nexus_lab.py`; the hand-off context is built by
`AccountRouter.handoff_context` / `handoff_message`.

| Requirement | Artifact | Test |
|---|---|---|
| Modes MANUAL / AUTO / FAILOVER / LOAD_BALANCING | `openhands_pool.MODES` | `tests/test_openhands_pool.py`, `tests/test_acceptance_account_pool.py` |
| Detect → record → decide → save → pick → authorise → (re)create conversation → hand context → continue → confirm the new executor works | `AccountRouter`, `OpenHandsExecutor.dispatch`, `AccountRouter.ensure_authorized` | `tests/test_openhands_failover.py`, `tests/test_acceptance_handoff.py`, `tests/test_acceptance_e2e_failover.py` |
| The transferred context must include project/task/goal/plan/subtasks/completed/remaining/decisions/branch/commit/uncommitted/test results/last errors/constraints/acceptance | `AccountRouter.handoff_context` (project, task, goal, plan, completed, remaining, repository, branch, last_commit, **decisions**, **constraints**, **test_results**, **uncommitted**, acceptance, previous report, last error) | `tests/test_acceptance_handoff.py::test_the_handoff_context_carries_everything_the_task_text_lists` |
| Do not assume conversations move between accounts; recover via a new conversation with context + workspace | documented and implemented (a switch is a new conversation) | `tests/test_openhands_failover.py`, inventory §3 |
| **Closed (this step)** | The four items previously carried only implicitly are now named fields: `decisions` and `constraints` are read from the project memory (`nexus_context.MemoryService` / `nexus_memory`, kinds `decision`/`constraint`); `test_results` is the run's durable per-task QA verdict (verdict, reason, commit) read from `verification`/`evidence`; `uncommitted` states honestly that a fresh sandbox inherits no working tree, with the last attempt's `base_sha` and whether the branch moved (`changed`). `handoff_message` prints all four, and rule 4 tells the new executor to keep to the decisions and constraints. `previous_report` now falls back to the last attempt's recorded `final_text` when the caller passes none. | `tests/test_acceptance_handoff.py` (6 cases: the context shape, the message shape, the recorded-report fallback, and the four load-bearing hand-off cases) |

All fourteen named context items are now carried as structured fields; the
hand-off is built from the run's own rows (never from the old conversation), so
it is reproducible after a crash, and it tells the new executor to clone the
branch and continue from the recorded commit.

---

## 5–15. The remaining sections — mapped for completeness

| Section | Artifact | Test |
|---|---|---|
| 5. Защита от одновременного выполнения | `nexus_lab_store.py` (leases, fencing tokens), `migrations/0028_task_lease_fencing.sql`, `0015_task_workspace_lock.sql`, `openhands_pool`, `nexus_retry.py` | `tests/test_race_protection.py` (13 cases), `tests/test_branch_write_lock.py`, `tests/test_lab_store.py` |
| 6. Настройка автономного выполнения Lab | `orchestration/lab_worker.py`, `NEXUS_LAB_INLINE` (`web_server.lab_inline_default`), `nexus_lab_workers` heartbeat | `tests/test_lab_worker_process.py`, `tests/test_planner_worker.py`, `tests/test_worker_setup.py`, `tests/test_acceptance_worker.py` |
| 7. Полный цикл автономной работы | `nexus_planner.py`, `nexus_lab.py` (SCHEDULER/EXECUTION/QA), `nexus_evidence.py` (EVIDENCE/ACCEPTANCE), reports (NOTIFICATION) | `tests/test_pipeline_e2e.py`, `tests/test_lab_evidence.py`, `tests/test_planner_worker.py` |
| 8. Проверка AI Provider Fallback | `orchestration/providers.py` (Gemini, Groq, OpenRouter, OpenHands orchestrator) | `tests/test_providers.py`, `tests/test_provider_diagnostics.py` (25 cases) |
| 9. OpenHands Tools и MCP | `orchestration/openhands_tools.py`, `mcp_server.py` | `tests/test_openhands_tools.py` (20 cases), `tests/test_cli_mcp.py` |
| 10. Безопасность | `secrets.py`, `net_guard.py`, `nexus_policy.py`, `archive_safety.py`, RBAC/project isolation | `tests/test_security_checks.py`, `tests/test_route_auth.py`, `tests/test_archive_safety.py` |
| 11. Набор тестов A–G | `tests/test_acceptance_*.py` + `tests/acceptance_kit.py` | 50 tests, all pass (see §Evidence below) |
| 12. Интеграция с интерфейсом NEXUS | `orchestration/studio/app.js` (`openhands-*`, `pool-mode`, `switch-executor`) | `tests/test_studio_ui.py`, `tests/test_lab_http.py`, `tests/test_browser_e2e.py` |
| 13. Диагностика | `orchestration/openhands_diagnostics.py` (VERIFIED/FAILED/BLOCKED/UNVERIFIED) | `tests/test_openhands_pool_http.py`, `tests/test_provider_diagnostics.py` |
| 14. Документация | README, DEPLOY, ENVIRONMENT, OPENHANDS_SETUP, OPENHANDS_ACCOUNTS, WORKER_SETUP, FAILOVER, TROUBLESHOOTING, TESTING | `tests/test_worker_setup.py` (launch docs), all present on disk |
| 15. Финальная приёмка | `docs/FINAL_ACCEPTANCE_REPORT.md` (ISSUED) | that report |
| Главное требование | no mocks of the Cloud client in integration tests; BLOCKED stated honestly | `tests/test_acceptance_dev_task.py::test_the_live_openhands_path_is_blocked_without_a_key` |

---

## Evidence run for this verification (task `t18`)

All commands run in this sandbox against PostgreSQL 17 on `127.0.0.1:5433`
(`TEST_SUPABASE_DB_URL=postgresql://nexus:nexus@127.0.0.1:5433/nexus_test`).

| Command | Result |
|---|---|
| `python -m pytest tests/test_acceptance_*.py -q` | **50 passed** in 60.65 s |
| `python -m pytest <15 OpenHands/pool/security/evidence suites> -q` | **296 passed** in 164.38 s |
| `python -m pytest tests/ -q` (full suite) | **1366 passed, 9 skipped, 0 failed** in 1239.68 s |
| `python -m ruff check .` | **All checks passed!** |

The acceptance suite (section 11 A–G) is the direct evidence that the pool,
the hand-off, the worker, recovery and the end-to-end failover are real and not
claims; the 296-test run is the evidence for the account pool, the account
admin surface, race protection, evidence and the security checks.

---

## Verdict

- **Sections 1–3 (this step's slice, pieces R-0002 … R-0056): complete.** Every
  safety rule has an artifact or is a followed instruction; every one of the 18
  inventory items has an inventory section; every one of the 13 per-account
  fields, every secret-handling rule, and every one of the 10 supported
  operations has an implementation and a test. Nothing in this slice is omitted.
- **Sections 4–15: mapped, and present.** The one gap this matrix previously
  recorded — four of the fourteen hand-off context items (*decisions*,
  *constraints*, *test results*, *uncommitted changes*) carried only implicitly
  — was **closed** in the continuation step that followed (see §4 above): all
  fourteen items are now named fields of `AccountRouter.handoff_context`, they
  appear in `handoff_message`, and `tests/test_acceptance_handoff.py` asserts
  them.
- **BLOCKED / UNVERIFIED** items are the environment-level ones already stated
  in `docs/FINAL_ACCEPTANCE_REPORT.md` §12: a two-live-account failover (one
  key), a live task that must clone and push (the account has no GitHub provider
  token), the Android/Windows packaging runtimes, and the opt-in live probes not
  requested in a given run.
