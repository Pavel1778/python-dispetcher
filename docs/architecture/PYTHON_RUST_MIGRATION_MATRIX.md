# NEXUS — Python → Rust Migration Matrix

> **STATUS: SPECIFICATION.** Maps every Python module and functional component
> to its target domain, Rust crate/module, API contract, DB objects, UI surface,
> tests and migration phase.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.
>
> **Status vocabulary (no `IMPLEMENTED` without real Rust code):**
> `NOT_ANALYZED · ANALYZED · DESIGNED · IMPLEMENTED · SHADOW_VERIFIED · CANARY ·
> RUST_PRIMARY · PYTHON_FALLBACK · RETIRED`.
> Every row starts at `ANALYZED` or `DESIGNED`; none is `IMPLEMENTED`.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md),
[MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md).

Counts (VERIFIED): 62 modules in `orchestration/`, 70 Python modules excluding
tests, 55 API endpoints, 50 tables, 37 migrations, 1,489 test functions.

---

## 1. Module-level matrix

Columns: **Py path → Domain → Rust crate → API contract → DB objects → UI surface
→ Tests → Phase → Status**.

| Python path | Domain | Rust crate | API contract | DB objects | UI | Tests | Phase | Status |
|---|---|---|---|---|---|---|---|---|
| `web_server.py` | Foundation/API | `nexus-api` | all HTTP routes | — | — | `test_web_server` | 2 | ANALYZED |
| `web_auth.py` | Identity & Access | `nexus-auth` | `/login`,`/logout`,`/api/session` | `sessions`,`login_attempts` | login screen | `test_auth` | 3 | ANALYZED |
| `nexus_projects.py` | Projects | `nexus-projects` | `/api/v1/projects*` | `nexus_projects`,`nexus_project_members`,`nexus_goals`,`nexus_project_events` | projects list | `test_nexus_projects`,`test_project_lifecycle` | 4 | ANALYZED |
| `nexus_conversations.py` | Conversations | `nexus-conversations` | `…/conversations*` | `nexus_conversations`,`nexus_conversation_messages`,`nexus_conversation_attachments`,`nexus_generations` | chat list | `test_conversations` | 4 | ANALYZED |
| `nexus_chat.py` | Conversations/AI | `nexus-conversations`,`nexus-ai` | chat turn | — | composer | `test_chat_workflow` | 4 | ANALYZED |
| `nexus_intent.py` | Conversations | `nexus-conversations` | chat turn | — | mode chips | `test_intent_router` | 4 | ANALYZED |
| `nexus_commands.py` | Conversations | `nexus-conversations` | chat turn | — | commands | `test_commands` | 4 | ANALYZED |
| `nexus_generation.py` | Conversations | `nexus-conversations` | `…/cancel` | `nexus_generations` | stop button | `test_generation_cancel` | 4 | ANALYZED |
| `nexus_specs.py` | Planner & DAG | `nexus-planner` | `…/specs/*` | `nexus_specs`,`_sections`,`_requirements`,`_stages`,`_links` | API only | `test_nexus_planning` | 5 | ANALYZED |
| `nexus_spec_planner.py` | Planner & DAG | `nexus-planner` | chat turn | `nexus_plans*` | API only | `test_plan_draft` | 5 | ANALYZED |
| `nexus_planner.py` | Planner & DAG | `nexus-planner` | chat turn | `nexus_plans*` | API only | `test_planner_worker` | 5 | ANALYZED |
| `nexus_planning.py` | Planner & DAG | `nexus-planner` | API | `nexus_plans*` | API only | `test_nexus_planning` | 5 | ANALYZED |
| `nexus_lab.py` | Execution Engine | `nexus-execution` | `…/runs/*` | `nexus_runs`,`nexus_run_tasks`,`nexus_run_reports` | runs list | `test_durable_runs` | 5 | ANALYZED |
| `nexus_lab_store.py` | Execution Engine | `nexus-execution` | background | many | — | `test_lab_*` | 5 | ANALYZED |
| `lab_worker.py` | Scheduler | `nexus-scheduler` | background | `nexus_lab_workers`,`nexus_conversation_cleanups` | — | `test_lab_worker_process` | 5 | ANALYZED |
| `cloud_client.py` | OpenHands Runtime | `nexus-openhands` | Cloud API | `cloud_sandboxes` | — | `test_cloud_*` | 6 | ANALYZED |
| `cloud_direct.py` | OpenHands Runtime | `nexus-openhands` | Cloud API | `cloud_sandboxes`,`conversation_routes` | — | `test_cloud_direct` | 6 | ANALYZED |
| `openhands_client.py` | OpenHands Runtime | `nexus-openhands` | agent-server | — | — | `test_openhands_*` | 6 | ANALYZED |
| `openhands_router.py` | OpenHands Runtime | `nexus-openhands` | API | `nexus_task_handoffs` | — | `test_openhands_failover` | 6 | ANALYZED |
| `openhands_pool.py` | OpenHands Runtime | `nexus-openhands` | API | `nexus_openhands_pool` | accounts | `test_openhands_pool` | 6 | ANALYZED |
| `openhands_diagnostics.py` | Settings/Admin | `nexus-observability` | `/api/openhands/diagnostics` | — | API only | `test_provider_diagnostics` | 6 | ANALYZED |
| `nexus_openhands_accounts.py` | OpenHands Runtime | `nexus-openhands` | `/api/openhands/accounts*` | `nexus_openhands_accounts`,`_events` | API only | `test_openhands_account_admin` | 6 | ANALYZED |
| `providers.py` | AI Gateway | `nexus-ai` | `/api/providers` | — | API only | `test_providers` | 6 | ANALYZED |
| `github_client.py` | GitHub | `nexus-github` | `/api/github/*` | — | GitHub tab | `test_github_client` | 6 | ANALYZED |
| `nexus_github_accounts.py` | GitHub | `nexus-github` | `/api/github/accounts*` | `nexus_github_accounts` | API only | `test_github_accounts` | 6 | ANALYZED |
| `github_confirmations.py` | GitHub | `nexus-github` | `…/confirm` | `nexus_pending_actions` | API only | `test_github_confirmations` | 6 | ANALYZED |
| `nexus_repo_reader.py` | Context/Memory | `nexus-memory` | chat turn | — | — | `test_repo_reader` | 6 | ANALYZED |
| `nexus_context.py` | Context/Memory | `nexus-memory` | `…/context`,`…/memory` | `nexus_project_context`,`nexus_memory` | API only | `test_context_memory` | 6 | ANALYZED |
| `nexus_binding.py` | Projects | `nexus-projects` | `…/binding*` | `nexus_project_repositories`,`nexus_project_executors` | binding UI | `test_project_binding` | 6 | ANALYZED |
| `file_lab.py` | File Laboratory | `nexus-files` | `…/files`,`…/workspaces` | `nexus_files`,`nexus_workspaces`,`nexus_workspace_files` | API only | `test_file_lab` | 7 | ANALYZED |
| `nexus_files.py` | File Laboratory | `nexus-files` | `…/files` | `nexus_files` | API only | `test_file_lab` | 7 | ANALYZED |
| `nexus_lab_store.py` (files) | File Laboratory | `nexus-files` | `…/files` | `nexus_files` | API only | `test_lab_store` | 7 | ANALYZED |
| `archive_safety.py` | File Laboratory | `nexus-files` | `…/files` | — | — | `test_archive_safety` | 7 | ANALYZED |
| `voice.py` | File Laboratory | `nexus-files` | `…/voice` | `nexus_voice_notes` | API only | — | 7 | ANALYZED |
| `mcp_server.py` | MCP | `nexus-mcp` | `POST /mcp` | — | — | `test_cli_mcp` | 7 | ANALYZED |
| `nexus_evidence.py` | Verification | `nexus-verification` | background | `nexus_evidence` | API only | `test_evidence_trust` | 5 | ANALYZED |
| `evidence_trust.py` | Verification | `nexus-verification` | background | `nexus_evidence` | — | `test_evidence_trust` | 5 | ANALYZED |
| `evidence_keygen.py` | Verification | `nexus-verification` | CLI | — | — | `test_evidence_trust` | 5 | ANALYZED |
| `nexus_reports.py` | Verification | `nexus-verification` | `…/report` | `nexus_run_reports`,`nexus_notifications` | chat report | `test_run_reports` | 5 | ANALYZED |
| `workflow_verifier.py` | Verification | `nexus-verification` | background | `workflows` | — | `test_pipeline_e2e` | 5 | ANALYZED |
| `nexus_actions.py` | Settings/Admin | `nexus-audit` | `…/actions` | `nexus_pending_actions` | API only | `test_workflow_actions` | 7 | ANALYZED |
| `nexus_retry.py` | Execution Engine | `nexus-execution` | background | — | — | `test_reliability` | 5 | ANALYZED |
| `nexus_repo_reader.py` | Context/Memory | `nexus-memory` | chat turn | — | — | `test_repo_reader` | 6 | ANALYZED |
| `nexus_policy.py` | Security | `nexus-core` | — | — | — | `test_security_checks` | 1 | ANALYZED |
| `net_guard.py` | Security | `nexus-core` | — | — | — | `test_security_checks` | 1 | ANALYZED |
| `secrets.py` | Security | `nexus-config` | — | — | — | `test_security_checks` | 1 | ANALYZED |
| `multipart.py` | File Laboratory | `nexus-api` | uploads | — | — | `test_file_lab` | 2 | ANALYZED |
| `runtime_state.py` | Execution Engine | `nexus-execution` | — | — | — | `test_reliability` | 5 | ANALYZED |
| `restore_point.py` | Audit & Lifecycle | `nexus-audit` | CLI | — | — | `test_restore_point` | 7 | ANALYZED |
| `control_plane.py` | Execution Engine | `nexus-execution` | API | `workflows`,`tasks` | legacy UI | `test_control_plane` | 5 | ANALYZED |
| `dispatcher.py` | Scheduler | `nexus-scheduler` | background | `workflows`,`tasks` | — | `test_dispatcher` | 5 | ANALYZED |
| `sr_runtime.py` | Execution Engine | `nexus-execution` | background | `sr_tool_calls` | — | `test_sr_runtime` | 5 | ANALYZED |
| `sr_tools.py` | Execution Engine | `nexus-execution` | background | `sr_tool_calls` | — | `test_sr_runtime` | 5 | ANALYZED |
| `local_runtime.py` | Execution Engine | `nexus-execution` | — | — | — | `test_worker_setup` | 5 | ANALYZED |
| `nexus_workflow.py` | Execution Engine | `nexus-execution` | `…/workflows/*` | `workflows` | legacy UI | `test_workflow_actions` | 5 | ANALYZED |
| `nexus_generation.py` | Conversations | `nexus-conversations` | `…/cancel` | `nexus_generations` | stop button | `test_generation_cancel` | 4 | ANALYZED |
| `web_ui.py` | Foundation/API | `nexus-api` | static serving | — | all | `test_studio_ui` | 2 | ANALYZED |
| `cli.py` | CLI | `nexus-cli` | HTTP client | — | — | `test_cli_mcp` | 8 | ANALYZED |
| `nexus_commands.py` | Conversations | `nexus-conversations` | chat turn | — | commands | `test_commands` | 4 | ANALYZED |
| `nexus_conversations.py` (cleanup) | Audit & Lifecycle | `nexus-audit` | background | `nexus_conversation_cleanups` | — | `test_conversation_cleanup` | 7 | ANALYZED |
| `nexus_spec_delivery.py` | Planner & DAG | `nexus-planner` | background | — | — | `test_nexus_planning` | 5 | ANALYZED |
| `nexus_analysis.py` | Conversations | `nexus-conversations` | chat turn | — | analysis mode | `test_intent_router` | 4 | ANALYZED |
| `tools/benchmark.py` | Observability | `nexus-observability` | CLI | — | — | — | 1 | ANALYZED |

**Non-`orchestration/` Python:** `packaging/debian/nexus-client.py` (client),
`tools/*.py` (ops), `conftest.py`, `tests/*`. All ANALYZED.

---

## 2. API endpoints → Rust interfaces (55 VERIFIED endpoints)

Selected groups (all 55 map 1:1 to `nexus-api` handlers):

| Group | Paths | Rust module |
|---|---|---|
| Session/auth | `/login`,`/logout`,`/login/state`,`/api/session` | `nexus-auth` |
| Projects | `/api/v1/projects[/{id}]` | `nexus-projects` |
| Conversations | `…/conversations[/{cid}][/messages|/cancel|/export]` | `nexus-conversations` |
| Runs | `…/runs[/{run_id}][/report]`, `…/runs/{id}/tasks/{tid}/switch` | `nexus-execution` |
| Goals | `…/goals/{gid}/run` | `nexus-execution` |
| Specs | `…/specs/{id}[/content|sections|requirements|stages]` | `nexus-planner` |
| Context/memory | `…/context`,`…/memory`,`…/context/refresh` | `nexus-memory` |
| Files/workspaces | `…/files*`,`…/workspaces*` | `nexus-files` |
| Voice | `…/voice*` | `nexus-files` |
| Actions | `…/actions*` | `nexus-audit` |
| Binding | `…/binding*`,`…/repository`,`…/repositories/*` | `nexus-projects` |
| Executors | `…/executors/*` | `nexus-openhands` |
| GitHub | `/api/github/*`,`…/github/*` | `nexus-github` |
| OpenHands | `/api/openhands/*` | `nexus-openhands` |
| Providers | `/api/providers` | `nexus-ai` |
| System | `/api/system`,`/api/health`,`/healthz`,`/readyz`,`/api/config`,`/api/capabilities`,`/api/studio` | `nexus-observability`/`nexus-api` |
| Security | `/api/security/policy` | `nexus-core` |
| MCP | `POST /mcp` | `nexus-mcp` |
| Legacy | `/api/workflows*`,`/api/tasks*`,`/api/repositories*` | `nexus-execution` (owner-level; see §4) |

---

## 3. UI functions → backend/API/UI matrix

VERIFIED UI surfaces (studio): project list, chat list + search, conversation
view, composer with 7 modes (auto, ask, analysis, plan, code, autonomous,
manual), Agent tab (runs), Files tab (workspace tree), accounts (GitHub,
OpenHands), theme (light/dark), login.

| Feature | Backend | API | UI | Full user scenario | Tests | Gap |
|---|---|---|---|---|---|---|
| Projects CRUD | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Project archive/trash/restore | ✅ | ✅ | ⚠️ | partial | ✅ | trash view UI |
| Conversations CRUD/pin/archive/trash | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Chat turn (7 modes) | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Stop generation | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Runs: start/view/cancel/retry/hide/remove | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Run report in chat | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| File upload | ✅ | ✅ | ❌ | ❌ | ✅ | **no screen** |
| Workspaces | ✅ | ✅ | ⚠️ | partial | ✅ | partial tree |
| Voice notes | ✅ | ✅ | ❌ | ❌ | ❌ | **no screen, no test** |
| Evidence | ✅ | ✅ | ❌ | ❌ | ✅ | **no screen** |
| Plan approval | ✅ | ✅ | ⚠️ | partial | ✅ | approval UI |
| GitHub create/visibility | ✅ | ✅ | ❌ | ❌ | ✅ | **no screen** |
| OpenHands accounts | ✅ | ✅ | ⚠️ | partial | ✅ | full CRUD screen |
| Diagnostics | ✅ | ✅ | ❌ | ❌ | ✅ | **no screen** |
| Allowlist editor | ✅ | ✅ | ❌ | ❌ | ✅ | **no screen** |
| MCP tools | ✅ | ✅ | n/a | n/a | ✅ | machine surface |
| Terminal | ❌ | ❌ | ❌ | ❌ | ❌ | **not implemented** |
| Collaboration | ❌ | ❌ | ❌ | ❌ | ❌ | **not implemented** |

**Parity gaps counted (VERIFIED):** 9 code-only surfaces with no screen
(uploads, workspaces-partial, voice, evidence, plan-approval-partial,
GitHub-write, OpenHands-account-partial, diagnostics, allowlist) + 2
not-implemented features (terminal, collaboration).

---

## 4. Component classes called out by the task

- **API-only functions (no UI):** lab file uploads, workspaces, voice notes,
  evidence, plan approval, GitHub create/visibility, diagnostics, allowlist,
  OpenHands account management (VERIFIED, `docs/KNOWN_LIMITATIONS.md`).
- **UI-only functions:** none found (VERIFIED) — every UI action calls an API.
- **Incomplete functions:** device sync (polling only), voice transcription
  (browser-provided only), PDF/DOCX/XLSX/PPTX preview (download only).
- **Duplicate implementations:** the **legacy Sr/Jr control plane**
  (`control_plane.py`, `dispatcher.py`, `sr_runtime.py`, `sr_tools.py`,
  `workflow_verifier.py`, `cloud_dispatcher.py`) coexists with the newer
  **autonomous lab** (`nexus_lab*.py`). Two orchestration lines
  (**VERIFIED** — the repo itself records this as "the two lifecycle lines").
- **Deprecated components:** legacy `/api/workflows`, `/api/tasks` are
  owner-level (not project-isolated) — flagged as a security note in
  `docs/NEXUS-AUDIT.md`.
- **Unused modules:** **UNKNOWN** — needs a static import-graph pass (not run
  here).
- **Unknown dependencies:** **UNKNOWN** — needs the same pass.

---

## 5. Full CRUD lifecycle & UI parity matrix

Legend: **C/R/U/D** create/read/update/delete; **A** archive; **T** trash;
**R*** restore; **PD** permanent delete; **RBAC**; **iso** project isolation.

| Entity | C | R | U | A | T | R* | PD | Bulk | RBAC | iso | API | UI | Audit | Retention |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Projects | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ design | ❌ | member | ✅ | ✅ | ✅ | ✅ | — |
| Conversations | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ worker | ❌ | member | ✅ | ✅ | ✅ | ✅ | window |
| Messages | ✅ | ✅ | ❌ append-only | n/a | n/a | n/a | ✅ cascade | ❌ | member | ✅ | ✅ | ✅ | — | with conv |
| Tasks | ✅ | ✅ | ✅ state | n/a | n/a | n/a | ❌ | ❌ | system | ✅ | ✅ | ✅ | ✅ | with run |
| Runs | ✅ | ✅ | ✅ | n/a | hide | ✅ show | ✅ terminal | ❌ | member | ✅ | ✅ | ✅ | ✅ | — |
| OpenHands Accounts | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ revoke | ❌ | owner | n/a | ✅ | ⚠️ | ✅ | — |
| LLM Providers | env | ✅ | env | n/a | n/a | n/a | env | n/a | owner | n/a | ✅ | ❌ | — | — |
| Models | env | ✅ | env | n/a | n/a | n/a | n/a | n/a | owner | n/a | ✅ | ❌ | — | — |
| GitHub accounts | ✅ | ✅ | ✅ rotate | n/a | n/a | n/a | ✅ revoke | ❌ | owner | n/a | ✅ | ⚠️ | ✅ | — |
| Repositories | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ remove | ❌ | member | ✅ | ✅ | ✅ | ✅ | — |
| Files | ✅ | ✅ | ⚠️ ws-put | n/a | n/a | n/a | ✅ | ❌ | member | ✅ | ✅ | ❌ | — | — |
| Workspaces | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ | ❌ | member | ✅ | ✅ | ⚠️ | — | — |
| MCP Servers | n/a | ✅ | n/a | n/a | n/a | n/a | n/a | n/a | caller | ✅ | ✅ | n/a | — | — |
| MCP Tools | n/a | ✅ | n/a | n/a | n/a | n/a | n/a | n/a | caller | ✅ | ✅ | n/a | — | — |
| Notifications | ✅ | ✅ | ⚠️ read | n/a | n/a | n/a | ⚠️ | n/a | member | ✅ | ✅ | ⚠️ | — | — |
| Memory | ✅ | ✅ | ✅ supersede | n/a | n/a | n/a | ❌ | ❌ | member | ✅ | ✅ | ❌ | — | — |
| Settings | n/a | ✅ | env | n/a | n/a | n/a | n/a | n/a | owner | n/a | ✅ | ❌ | — | — |
| Users | ❌ | ✅ self | ✅ pwd | n/a | n/a | n/a | n/a | n/a | owner | n/a | ✅ | ✅ | ✅ | — |
| Access/RBAC | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ remove | ❌ | owner | ✅ | ⚠️ | ⚠️ | ✅ | — |
| Audit records | ✅ sys | ✅ | ❌ | n/a | n/a | n/a | policy | n/a | owner | ✅ | ⚠️ | ❌ | ✅ | policy |

**Destructive-operation requirements (mandatory for the Rust port):**
confirmation, dependency checks, an audit trail, a restore window where
applicable, and an explicit permanent-delete policy. Today: conversations and
projects are soft-deleted with a retention window; files and accounts are
hard-removed with dependency checks; nothing is hard-deleted without a check.

**"Not applicable" reasons:** messages are an append-only history; tasks have
no independent lifecycle (they belong to a run); providers/models/settings are
configuration, not rows; MCP is a transport.

---

## 6. Status rule

No row may move to `IMPLEMENTED` without real Rust code and a passing test.
This document keeps every row at `ANALYZED`/`DESIGNED` until then.

---

## 7. Update 2026-11-15 — performance-driven status (added, history preserved)

A measured performance audit (see
[PERFORMANCE_DRIVEN_RUST_MATRIX](./PERFORMANCE_DRIVEN_RUST_MATRIX.md)) found
that the one structural bottleneck is the **DB access layer** (single
connection + `RLock`), and that it is **solvable in Python** (a connection
pool removes ~19× of the p99 at 100 concurrency). Therefore:

- The `web_server.py` → `nexus-api` and `cloud_store.py` → `nexus-db` rows
  remain **ANALYZED**; their performance decision is **PYTHON_OPTIMIZE** first,
  not RUST_FIRST.
- No row is promoted to RUST_FIRST on the basis of language alone; a promotion
  needs a measured gain above the threshold in
  [PERFORMANCE_OPTIMIZATION_ROADMAP](./PERFORMANCE_OPTIMIZATION_ROADMAP.md) §7.
- Archive extract/inspect (`file_lab.py`, `archive_safety.py`) are marked
  **RUST_LATER** (CPU-bound but low volume).

This is an *added* section; the module and CRUD matrices above are unchanged.
