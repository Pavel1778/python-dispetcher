# NEXUS 3.0 — Compatibility contract and entity map

This is the Phase 1 "mapping of old and new entities" and the frozen contract
that lets the migration proceed without breaking anything a client depends on.
It is a living table: a row moves from `Python` to `Rust (parity)` to
`Rust (authoritative)` only when the parity test is green, and the switch removes
the Python version in the same change.

## 1. Entities (database — unchanged)

Every table below stays exactly as it is. 3.0 adds no rename and no drop; any
new column is additive in a new migration file and named in
`cloud_store.REQUIRED_COLUMNS`.

| Domain | Tables | Owner today |
|---|---|---|
| Projects / members | `nexus_projects`, `nexus_project_members`, `nexus_project_repositories`, `nexus_project_executors`, `nexus_project_context`, `nexus_project_events` | Python |
| Conversations / chat | `nexus_conversations`, `nexus_conversation_messages`, `nexus_conversation_attachments`, `nexus_agent_sessions`, `nexus_generations` | Python |
| Goals / specs | `nexus_goals`, `nexus_specs`, `nexus_spec_sections`, `nexus_spec_requirements`, `nexus_spec_links`, `nexus_spec_stages` | Python |
| Runs / tasks | `nexus_runs`, `nexus_run_tasks`, `nexus_task_executions`, `nexus_task_handoffs`, `nexus_executor_stops` | **Python; Rust claim = parity** |
| Plans | `nexus_plans`, `nexus_plan_tasks`, `nexus_plan_dependencies` | Python |
| Evidence | `nexus_evidence` | Python |
| Reports / notifications | `nexus_run_reports`, `nexus_notifications` | Python |
| Accounts | `nexus_openhands_accounts`, `nexus_openhands_account_events`, `nexus_openhands_pool`, `nexus_github_accounts` | Python |
| Files | `nexus_files`, `nexus_workspaces`, `nexus_workspace_files`, `nexus_voice_notes` | Python |
| Cleanup | `nexus_conversation_cleanups` | Python |
| Memory / context | `nexus_memory` | Python |
| Legacy dispatcher | `workflows`, `tasks`, `conversations`, `repositories`, `task_events`, `sr_tool_calls` | Python |
| Auth / infra | `sessions`, `login_attempts`, `conversation_routes`, `cloud_sandboxes`, `nexus_pending_actions`, `nexus_lab_workers` | Python |

## 2. Modules → Rust target → status

| Python module | Rust target | Status |
|---|---|---|
| `nexus_lab_store._RUN_ENTERED_FROM` (run machine) | `nexus-domain::state` | **parity** |
| `nexus_workflow._ALLOWED` (workflow machine) | `nexus-domain::state` | **parity** |
| `control_plane._ALLOWED` (task machine) | `nexus-domain::state` | **parity** |
| `nexus_planner.validate_tasks` (DAG) | `nexus-domain::dag` | **parity** |
| `nexus_workflow.plan_hash_of` | `nexus-domain::dag` | **parity** |
| `nexus_retry` (cycle/refused wait) | `nexus-domain::retry` | **parity** |
| `LabStore.claim_task` (decision) | `nexus-core::scheduler` | **parity** |
| `LabStore.claim_task` (transaction) | `nexus-store` | **parity (this session)** |
| `LabStore.mark_running` / `reclaim_expired` / `holds_lease` | `nexus-store` | **parity (this session)** |
| `LabStore._apply_run_state` (transitions) | `nexus-store` (next) | Python |
| `nexus_executor_stops`, `abandoned` recovery | `nexus-store` (next) | Python |
| `nexus_lab.LabScheduler` (loop) | `nexus-worker` | Python |
| `cloud_client`, `cloud_direct`, `openhands_pool`, `openhands_router` | `nexus-openhands` | Python |
| `github_client`, `nexus_github_accounts` | `nexus-github` | Python |
| `web_server` routes | `nexus-api` (Axum) | Python |
| `nexus_intent`, `nexus_planner.task_kind` | — (stays Python) | deliberate |

## 3. API contract (frozen)

The `/api` routes in `docs/API.md` answer the same JSON for the same inputs
during the whole transition. A Rust route is added only at parity; the Python
route keeps serving until then. `tests/test_route_auth.py` is the executable
guard: every protected route needs `_require_read`/`_require_write` (Python) or
the equivalent extractor (Rust), or it fails.

## 4. What "parity" means here (the gate)

For a component to move from `Python` to `Rust (parity)`:

1. the Python behaviour is read from the source, not a summary;
2. a test drives **both** implementations over the **same** cases on real
   PostgreSQL (or the same pure inputs);
3. the outputs and the resulting state are compared, not sampled;
4. a disagreement fails the suite, named by case.

Only then is the Rust version a candidate to become authoritative, and that
switch is a separate, reviewable change that deletes the Python version.

## 5. Client contract (unchanged)

Web, APK, DEB and EXE all speak the same HTTP API. No client-specific endpoint is
introduced; a native shell adds only OS functions (secure storage, file dialogs,
notifications, deep links, lifecycle). A client change that requires a new API
shape is a contract change and follows §3.
