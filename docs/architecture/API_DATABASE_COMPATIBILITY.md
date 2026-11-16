# NEXUS — API & Database Compatibility

> **STATUS: SPECIFICATION.** How Python and Rust coexist on one database and one
> API during the strangler migration, and how a rollback stays real.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md),
[EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md).

---

## 1. The database (VERIFIED)

- PostgreSQL (Supabase), schema applied by the process (`AUTO_MIGRATE=true`).
- 37 migrations, 50 tables, 89 foreign keys, 2 triggers
  (`trg_nexus_tasks_binding`, `trg_nexus_workflows_project`), 30 UUID defaults.
- The migration runner (`cloud_store.apply_migrations`) records a SHA-256
  checksum per file in `schema_migrations` **in the same transaction as the
  file's execution** (VERIFIED — `apply_migrations` runs each file through
  `_tx`, which commits only on success).
- An already-applied file whose body changed is a **hard refusal**
  (`MigrationError`), not a silent re-run.

**Consequence for the 0037 fix (VERIFIED by experiment):** a migration that
fails rolls back and is **not** recorded, so a database cannot hold a bad
checksum for a failed migration. A `MIGRATION_CORRECTIONS` mechanism is
therefore **not required** to fix 0037 (see the fix report).

---

## 2. Object → owner mapping (Python → Rust → compatibility)

| DB object | Python owner | Rust owner (target) | Compatibility requirement | Migration strategy | Verification |
|---|---|---|---|---|---|
| `nexus_projects`, `_members`, `_goals`, `_project_events` | `nexus_projects.py` | `nexus-projects` | same columns, same 404 isolation | additive only | contract tests |
| `nexus_conversations`, `_messages`, `_attachments`, `nexus_generations` | `nexus_conversations.py` | `nexus-conversations` | same pin/delete/generation semantics | additive only | lifecycle tests |
| `nexus_runs`, `nexus_run_tasks`, `nexus_run_reports`, `nexus_task_executions`, `nexus_executor_stops`, `nexus_task_handoffs`, `nexus_lab_workers` | `nexus_lab*.py` | `nexus-execution`/`nexus-scheduler` | same state machine, lease, fencing | additive only | the 15 scenarios |
| `nexus_specs*`, `nexus_plans*` | `nexus_spec*.py` | `nexus-planner` | same plan shape, DAG rules | additive only | planner tests |
| `nexus_openhands_accounts`, `_events`, `nexus_openhands_pool`, `cloud_sandboxes`, `conversation_routes` | `nexus_openhands_accounts.py`, `cloud_*.py` | `nexus-openhands` | keys encrypted; never returned | additive only | security tests |
| `nexus_github_accounts`, `nexus_pending_actions` | `nexus_github_accounts.py`, `github_confirmations.py` | `nexus-github` | guarded-write protocol | additive only | confirm tests |
| `nexus_files`, `nexus_workspaces`, `_workspace_files`, `nexus_voice_notes` | `file_lab.py` | `nexus-files` | archive safety; size cap | additive only | safety tests |
| `nexus_project_context`, `nexus_memory` | `nexus_context.py` | `nexus-memory` | supersede-not-delete | additive only | memory tests |
| `nexus_evidence`, `nexus_run_reports` | `nexus_evidence.py`, `nexus_reports.py` | `nexus-verification` | Ed25519 signatures valid | additive only | evidence tests |
| `sessions`, `login_attempts` | `web_auth.py` | `nexus-auth` | same session semantics | additive only | auth tests |
| `task_events`, `nexus_notifications` | `web_server.py`, `nexus_lab_store.py` | `nexus-events` | per-entity order | additive only | event tests |
| `nexus_conversation_cleanups` | `nexus_lab_store.py` | `nexus-audit` | same retention | additive only | cleanup tests |
| `schema_migrations` | `cloud_store.py` | `nexus-db` | same checksum discipline | shared | migration tests |
| `workflows`, `tasks`, `repositories`, `sr_tool_calls` (legacy) | `control_plane.py`, `sr_*.py` | `nexus-execution` | owner-level (not project-isolated) | retire last | legacy tests |

---

## 3. API versioning & compatibility

- **Contract:** the Rust API reproduces the current paths and response shapes
  for a cut-over domain. `docs/API.md` (in PR #39) is the reference shape.
- **Versioning:** the existing `/api/v1/projects*` prefix is the stable
  surface. New Rust-only endpoints, if any, get a new prefix; existing shapes
  do not change in place.
- **Error mapping:** `NotFound → 404` (this is how isolation is expressed),
  `Conflict → 409`, `Validation → 400`, `Forbidden → 403` only where Python
  already uses it.

---

## 4. Expand/contract migrations

Every schema change during the migration window is **expand/contract**:

1. **Expand:** add the new column/table (nullable, defaulted, additive). Both
   implementations can run.
2. **Migrate:** backfill in a separate, idempotent step.
3. **Switch:** the new owner starts using the new shape.
4. **Contract:** drop the old shape **only after** the previous owner is
   retired and the observation window has passed.

**Rule:** no destructive change (`DROP`, `DELETE`, `TRUNCATE`, a `NOT NULL`
without a default) while two implementations coexist. This preserves the
ability to roll back to Python (the old code must still be able to read the
data).

---

## 5. Writer ownership (the no-dual-writers rule)

**VERIFIED constraint (task):** Python and Rust must not write the same entity
concurrently without a proven consistency protocol. The design:

- Each domain has **exactly one writer** at any time: the process that owns the
  domain's route at the reverse proxy.
- The non-owner may **read** the domain but must **not write** it.
- Ownership is recorded in a **cutover ledger** (a table) and enforced by the
  proxy routing (only the owner receives write methods for the domain).
- This preserves the store's single-writer discipline (per domain), rather than
  breaking it by having two writers.

**Cutover ledger (proposed table, additive):**

```
nexus_domain_ownership(domain text primary key, owner text, since timestamptz, by text)
```

Read by both processes; written only by the cutover procedure (a human-gated
step), never by a request path.

---

## 6. Cutover procedure (per domain)

1. Parity tests green; canary at 5%.
2. Error rate ≤ the Python baseline and p50 within ±10% for the canary window.
3. Set `nexus_domain_ownership(domain, 'rust')` (human-gated).
4. Flip the proxy route for write methods to Rust.
5. Watch for divergence; if any, flip back (§7).

## 7. Rollback procedure

- **Route rollback:** the proxy switches the domain back to Python in one step.
- **Data rollback:** possible **only** because §4 kept the schema readable by
  Python. If a change was not expand/contract, the route switch is **not** a
  sufficient rollback (VERIFIED rule from the task) — the change is rejected at
  review.
- **Deploy rollback:** Render "Deploy previous" to the last proven Python build.
- **Database rollback:** forward-only new migrations; no destructive rollback.

## 8. In-flight task handling at cutover

- A run in an active state is owned by exactly one scheduler (the current
  writer). At cutover, the outgoing scheduler is drained (no new claims) and
  the incoming one takes over claims after the leases of the outgoing one
  expire (default 900 s) — or a lease hand-off is performed explicitly.
- The `nexus_domain_ownership` row is set **before** the new scheduler starts
  claiming, and the old scheduler stops claiming **before** that.

## 9. Backup & restore (VERIFIED infrastructure exists)

`pg_dump`/`pg_restore` (drill in `tests/test_backup_restore.py`), plus a
point-in-time snapshot before each cutover. The snapshot is the data rollback
floor.

## 10. Compatibility windows

- A domain stays in the "both can read, one can write" state only for the
  canary + observation window, then either advances to Rust-primary or rolls
  back. A domain must not stay in the dual-read state indefinitely.
