# NEXUS — Rust Domain Boundaries

> **STATUS: SPECIFICATION.** Target domain boundaries for the Rust modular
> monolith. No Rust code exists; this is design.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Siblings: [RUST_WORKSPACE_DESIGN](./RUST_WORKSPACE_DESIGN.md),
[EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md),
[API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md).

Each domain below states: **Ownership · Public interfaces · Internal modules ·
Data ownership · Dependencies · Events · Failure modes · Security boundary ·
Tests · Migration acceptance criteria.**

A domain is a *boundary*, not necessarily a process. Only
[EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md)'s scheduler/executor is a
candidate for a separate process, and only with evidence.

---

## 1. Foundation

- **Ownership:** shared types, IDs, errors, config, tracing bootstrap.
- **Public interfaces:** `NexusError`, newtype IDs, `Config`, `Clock`, `IdGen`.
- **Internal modules:** `ids`, `error`, `config`, `time`, `telemetry`.
- **Data ownership:** none (no tables).
- **Dependencies:** none (the only crate with no domain dependency).
- **Events:** none.
- **Failure modes:** config validation failure at startup (fail fast).
- **Security boundary:** holds no secret material; secrets are read via
  `nexus-config` handles, never logged.
- **Tests:** unit.
- **Migration acceptance:** type parity with the Python DTOs; config keys match
  `docs/ENVIRONMENT.md`.

## 2. Identity & Access

- **Ownership:** login, sessions, CSRF, machine bearer, RBAC membership.
- **Public interfaces:** `login`, `logout`, `session`, `authorize(principal, project)`, `require_read/write`.
- **Internal modules:** `password` (PBKDF2-HMAC-SHA256, 600k), `session`, `csrf`, `bearer`, `throttle`, `lockout`, `membership`.
- **Data ownership:** `sessions`, `login_attempts`, `nexus_project_members`.
- **Dependencies:** Foundation, Projects (membership reads).
- **Events:** `auth.login`, `auth.logout`, `auth.lockout`.
- **Failure modes:** DB down → login unavailable; clock skew → session TTL wrong.
- **Security boundary:** the single owner of credential verification; never
  returns a stored secret; a non-member resolves to 404 (never 403).
- **Tests:** `test_auth.py`, `test_route_auth.py` parity; concurrency on session
  revoke.
- **Migration acceptance:** identical session semantics (server-side, HMAC of
  token), identical lockout, identical 404 isolation.

## 3. Projects & Workspace

- **Ownership:** project lifecycle, membership, repository binding.
- **Public interfaces:** `create/list/get/rename`, `set_archived`, `delete/restore`, `bind_repository`.
- **Internal modules:** `projects`, `members`, `binding`.
- **Data ownership:** `nexus_projects`, `nexus_project_members`, `nexus_goals`, `nexus_project_events`, `nexus_project_repositories`, `nexus_project_executors`.
- **Dependencies:** Foundation, Identity & Access.
- **Events:** `project.created/archived/deleted/restored`.
- **Failure modes:** delete with an active run is refused (by design).
- **Security boundary:** every project-scoped call authorizes membership first.
- **Tests:** `test_nexus_projects.py`, `test_project_lifecycle.py`.
- **Migration acceptance:** identical archive/trash/restore semantics; new work
  refused in a trashed project.

## 4. Conversations & Messages

- **Ownership:** chats, messages, pins, generations (stop).
- **Public interfaces:** `create/list/get/rename`, `set_mode`, `set_pinned`, `set_archived`, `set_deleted`, `messages`, `cancel`.
- **Internal modules:** `conversations`, `messages`, `generation`.
- **Data ownership:** `nexus_conversations`, `nexus_conversation_messages`, `nexus_conversation_attachments`, `nexus_generations`, `nexus_conversation_cleanups`.
- **Dependencies:** Foundation, Identity & Access, Projects, AI Gateway (for a
  chat turn).
- **Events:** `conversation.created/renamed/pinned/archived/deleted`,
  `generation.started/cancelled`.
- **Failure modes:** one running generation per conversation (partial unique
  index) → a second answer is refused 409.
- **Security boundary:** membership; untrusted text is escaped before render.
- **Tests:** `test_conversations.py`, `test_conversation_management.py`,
  `test_generation_cancel.py`.
- **Migration acceptance:** pins/soft-delete/generation semantics identical.

## 5. Task Management

- **Ownership:** the task entity and its state machine within a run.
- **Public interfaces:** `create_tasks`, `claim`, `retry`, `task_state`.
- **Internal modules:** `task`, `state`, `lease`.
- **Data ownership:** `nexus_run_tasks` (with `nexus_runs`), `nexus_task_executions`, `nexus_task_handoffs`.
- **Dependencies:** Foundation, Execution Engine (state is written there).
- **Events:** `task.state_changed`.
- **Failure modes:** lease conflict; branch exclusivity (`_BRANCH_FREE`).
- **Security boundary:** system-written; not directly user-writable.
- **Tests:** `test_durable_runs.py`, `test_lab_verifier.py`.
- **Migration acceptance:** the state machine and lease/fencing semantics match
  [EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md).

## 6. Execution Engine

- **Ownership:** runs, scheduling, dispatch, verification, evidence.
- **Public interfaces:** `start_run`, `pass`, `cancel`, `retry_failed`, `delete_run`, `report`.
- **Internal modules:** `run`, `scheduler`, `executor`, `verifier`, `evidence`.
- **Data ownership:** `nexus_runs`, `nexus_run_tasks`, `nexus_run_reports`, `nexus_evidence`, `nexus_executor_stops`, `nexus_task_executions`, `nexus_lab_workers`.
- **Dependencies:** Foundation, Task Management, OpenHands Runtime, GitHub
  Integration, AI Gateway, Events.
- **Events:** `run.started/advanced/completed/failed/cancelled`.
- **Failure modes:** stale run detection; in-flight task recovery; branch hold.
- **Security boundary:** the only writer of terminal run/task state.
- **Tests:** `test_durable_runs.py`, `test_reliability.py`, `test_lab_loop.py`.
- **Migration acceptance:** all 15 mandatory scenarios
  ([MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md) §4).

## 7. Planner & DAG

- **Ownership:** spec intake, planning, DAG construction, dependency gating.
- **Public interfaces:** `plan(spec)`, `validate_dag`, `start_run_from_plan`.
- **Internal modules:** `spec`, `planner`, `dag`, `spec_store`.
- **Data ownership:** `nexus_specs`, `nexus_spec_sections`, `nexus_spec_requirements`, `nexus_spec_stages`, `nexus_spec_links`, `nexus_plans`, `nexus_plan_tasks`, `nexus_plan_dependencies`.
- **Dependencies:** Foundation, AI Gateway, Execution Engine.
- **Events:** `plan.created`, `spec.created`.
- **Failure modes:** empty-plan fallback; LLM outage → recorded failure.
- **Security boundary:** planning reads project state under membership.
- **Tests:** `test_nexus_planning.py`, `test_plan_draft.py`.
- **Migration acceptance:** plan shape + DAG validation identical.

## 8. Scheduler & Dispatcher

- **Ownership:** the loop: reclaim → claim → dispatch → poll → verify → record
  → decide; and the stop/cleanup queues.
- **Public interfaces:** `pass`, `claim_stops`, `claim_cleanup`.
- **Internal modules:** `scheduler`, `dispatcher`, `queues`.
- **Data ownership:** writes to `nexus_runs`/`nexus_run_tasks` via Task/Execution
  ownership; owns `nexus_conversation_cleanups` claim.
- **Dependencies:** Execution Engine, OpenHands Runtime, PostgreSQL.
- **Events:** `worker.heartbeat`, `cleanup.queued`.
- **Failure modes:** worker death → lease reclaim; `max_active_tasks` limit
  (`NEXUS_LAB_MAX_ACTIVE_TASKS`, default 8).
- **Security boundary:** system component; no user input.
- **Tests:** `test_lab_worker_process.py`, `test_dispatcher.py`.
- **Migration acceptance:** a candidate for a separate process only with a
  measured need; otherwise in-process.

## 9. OpenHands Runtime

- **Ownership:** Cloud transport (direct/app), account pool, routing, failover.
- **Public interfaces:** `dispatch`, `poll`, `stop`, `remove`, `select_account`, `handoff`.
- **Internal modules:** `client`, `direct`, `pool`, `router`, `diagnostics`.
- **Data ownership:** `cloud_sandboxes`, `conversation_routes`, `nexus_openhands_accounts`, `nexus_openhands_account_events`, `nexus_openhands_pool`, `nexus_task_handoffs`.
- **Dependencies:** Foundation, AI Gateway (models), Identity & Access.
- **Events:** `account.selected/failed`, `handoff`.
- **Failure modes:** 429 with Retry-After; account at concurrency limit; Cloud
  conversation keeps running after a local cancel (documented limitation).
- **Security boundary:** API keys encrypted at rest (Fernet); never returned.
- **Tests:** `test_openhands_*.py`, `test_cloud_direct.py`.
- **Migration acceptance:** identical routing/failover; identical 429 handling.

## 10. AI Gateway

- **Ownership:** the LLM provider cascade (Gemini → Groq → OpenRouter →
  OpenHands) and provider probes.
- **Public interfaces:** `complete(prompt)`, `probe()`, `estimate_tokens`.
- **Internal modules:** `cascade`, `provider`, `probe`, `limits`.
- **Data ownership:** none (config via env).
- **Dependencies:** Foundation.
- **Events:** `provider.failed`.
- **Failure modes:** all providers fail → `AllProvidersFailed`; too-large
  prompt → `ProviderTooLarge`.
- **Security boundary:** keys only in headers; never logged (`__repr__` hides).
- **Tests:** `test_providers.py`, `test_provider_diagnostics.py`.
- **Migration acceptance:** identical cascade order and error taxonomy.

## 11. Context & Memory

- **Ownership:** project context snapshots and hybrid memory.
- **Public interfaces:** `refresh`, `current`, `history`, `remember`, `retrieve`, `supersede`.
- **Internal modules:** `context`, `memory`, `dedupe`.
- **Data ownership:** `nexus_project_context`, `nexus_memory`.
- **Dependencies:** Foundation, Projects, GitHub (repo analysis).
- **Events:** `context.refreshed`, `memory.remembered`.
- **Failure modes:** supersede race (dedupe key).
- **Security boundary:** memory is project-scoped; cross-project reads refused.
- **Tests:** `test_context_memory.py`.
- **Migration acceptance:** supersede-not-delete semantics identical.

## 12. GitHub Integration

- **Ownership:** GitHub accounts, repository discovery, guarded writes.
- **Public interfaces:** `add/rotate/revoke`, `verify`, `bind_project`, `list_repositories`, `create_repository`, `set_visibility`.
- **Internal modules:** `client`, `accounts`, `confirmations`.
- **Data ownership:** `nexus_github_accounts`, `nexus_pending_actions`.
- **Dependencies:** Foundation, Identity & Access, Projects.
- **Events:** `github.account.added/revoked`.
- **Failure modes:** token without write scope; write refused without a
  confirmation token.
- **Security boundary:** tokens encrypted; writes behind a one-time
  confirmation token with TTL.
- **Tests:** `test_github_*.py`.
- **Migration acceptance:** identical guarded-write protocol.

## 13. File Laboratory

- **Ownership:** uploads, previews, downloads, workspaces, archives, voice.
- **Public interfaces:** `upload`, `list_files`, `preview`, `download`, `delete_file`, `create_workspace`, `put_workspace_file`, `workspace_diff`, `extract`.
- **Internal modules:** `files`, `workspaces`, `archive_safety`, `voice`.
- **Data ownership:** `nexus_files`, `nexus_workspaces`, `nexus_workspace_files`, `nexus_voice_notes`.
- **Dependencies:** Foundation, Projects.
- **Events:** `file.uploaded/deleted`.
- **Failure modes:** size cap (`NEXUS_MAX_UPLOAD_BYTES`); unsafe archive member.
- **Security boundary:** a member name never becomes a path; text/images only
  inline; other types download.
- **Tests:** `test_file_lab.py`, `test_archive_safety.py`, `test_lab_files.py`.
- **Migration acceptance:** identical extraction safety and preview matrix.

## 14. MCP Integration

- **Ownership:** the MCP server (stdio + Streamable HTTP); the client is
  design-only.
- **Public interfaces:** `POST /mcp`, tool surface.
- **Internal modules:** `server`, `tools`, `scopes`.
- **Data ownership:** none (acts through other domains).
- **Dependencies:** all domains it exposes.
- **Events:** none.
- **Failure modes:** a caller has exactly the authority of an authenticated
  NEXUS user (no privilege escalation).
- **Security boundary:** every tool re-checks project membership.
- **Tests:** `test_cli_mcp.py`.
- **Migration acceptance:** identical tool set and scope checks.

## 15. IDE & Terminal Gateway

- **Ownership:** the AI IDE surfaces and any terminal/exec gateway.
- **Public interfaces:** `ide_view`, editor routes.
- **Internal modules:** `ide`, `terminal`.
- **Data ownership:** `nexus_agent_sessions`.
- **Dependencies:** File Laboratory, GitHub, OpenHands Runtime.
- **Events:** `ide.opened`.
- **Failure modes:** **UNKNOWN** — terminal is not a first-class NEXUS
  component today.
- **Security boundary:** must not become an unguarded shell.
- **Tests:** `test_ide_view.py`.
- **Migration acceptance:** IDE view parity; terminal is a **new** capability
  to specify, not port.

## 16. Verification & Evidence

- **Ownership:** `GitVerifier`, the signed evidence record, acceptance decision.
- **Public interfaces:** `verify(task, report)`, `record(evidence)`, `accept`.
- **Internal modules:** `verifier`, `evidence`, `trust`.
- **Data ownership:** `nexus_evidence`, `nexus_run_reports`.
- **Dependencies:** GitHub Integration, Execution Engine.
- **Events:** `evidence.recorded`, `task.accepted/reworked`.
- **Failure modes:** a push that was refused must not be accepted (change
  requirement).
- **Security boundary:** evidence is Ed25519-signed.
- **Tests:** `test_lab_verifier.py`, `test_evidence_trust.py`, `test_run_reports.py`.
- **Migration acceptance:** identical accept/rework rules.

## 17. Self-QA

- **Ownership:** autonomous verification levels (L1/L2/L3).
- **Public interfaces:** see [SELF_QA_ARCHITECTURE](./SELF_QA_ARCHITECTURE.md).
- **Internal modules:** `runner`, `policy`, `evidence`.
- **Data ownership:** `nexus_pending_actions` (proposals), audit tables.
- **Dependencies:** Execution Engine, Verification & Evidence.
- **Events:** `qa.run.*`.
- **Failure modes:** must never gain unbounded production permissions.
- **Security boundary:** L3 production writes require separate authorization.
- **Tests:** design-only today (`docs/SELF_QA_ARCHITECTURE.md`).
- **Migration acceptance:** safety model implemented and enforced.

## 18. Events & Notifications

- **Ownership:** the event stream and user notifications.
- **Public interfaces:** `emit`, `list`, `mark`.
- **Internal modules:** `bus`, `notifications`.
- **Data ownership:** `task_events`, `nexus_notifications`, `nexus_project_events`.
- **Dependencies:** Foundation.
- **Events:** all (the bus).
- **Failure modes:** ordering; duplicate delivery.
- **Security boundary:** events are project-scoped.
- **Tests:** `test_run_history.py` (notifications), event tests.
- **Migration acceptance:** at-least-once with idempotent consumers.

## 19. Settings & Administration

- **Ownership:** `/api/config`, `/api/capabilities`, `/api/system`,
  diagnostics, allowlist.
- **Public interfaces:** `config`, `capabilities`, `system`, `diagnostics`.
- **Internal modules:** `config`, `diagnostics`, `allowlist`.
- **Data ownership:** none (env-derived); `nexus_lab_workers` for heartbeat.
- **Dependencies:** Foundation, OpenHands Runtime (diagnostics).
- **Events:** none.
- **Failure modes:** misconfigured env → startup refusal.
- **Security boundary:** names/booleans only; no secret values returned.
- **Tests:** `test_studio_api.py`, `test_provider_diagnostics.py`.
- **Migration acceptance:** identical config surface.

## 20. Audit & Lifecycle

- **Ownership:** audit trail, retention, trash/restore windows, permanent-delete
  policy.
- **Public interfaces:** `audit`, `retention`, `purge`.
- **Internal modules:** `audit`, `retention`, `lifecycle`.
- **Data ownership:** `nexus_conversation_cleanups`, `task_events` (audit),
  `schema_migrations`.
- **Dependencies:** Foundation, all domains (as event source).
- **Events:** `cleanup.queued/done`.
- **Failure modes:** purge before the restore window; orphan records.
- **Security boundary:** retention is policy; destructive ops are audited.
- **Tests:** `test_conversation_cleanup.py`, `test_durable_migrations.py`.
- **Migration acceptance:** identical retention and audit behaviour.

---

## Allowed dependency direction (acyclic)

```
Foundation
  └─ Identity & Access ── Projects & Workspace
        ├─ Conversations & Messages ── AI Gateway
        ├─ Task Management ── Execution Engine ── Scheduler & Dispatcher
        │         ├─ Planner & DAG ── AI Gateway
        │         ├─ OpenHands Runtime
        │         └─ Verification & Evidence ── GitHub Integration
        ├─ Context & Memory ── GitHub Integration
        ├─ File Laboratory
        └─ MCP Integration (leaf, depends on all)
Events & Notifications, Settings & Administration, Audit & Lifecycle: cross-cutting
```

## Forbidden cycles (VERIFIED as rules; must be enforced by the crate graph)

- **AI Gateway ⇏ Execution Engine** (the gateway must not know about runs).
- **OpenHands Runtime ⇏ Execution Engine** (the runtime reports; the engine
  decides — mirrors the Python rule that the executor "never decides
  completion").
- **Verification & Evidence ⇏ Scheduler** (verification is called by the
  engine; it must not drive scheduling).
- **Projects ⇏ any run/execution domain** (projects must not depend on runs;
  the Python code keeps runs in `nexus_lab*`, not in `nexus_projects`).
- **Foundation depends on nothing.**

Enforcement: a crate dependency check in CI (e.g. `cargo deny`/`cargo tree`
rules) once the workspace exists.
