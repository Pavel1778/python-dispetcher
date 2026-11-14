# Database

PostgreSQL (the suite runs against PostgreSQL 17). The schema is applied by the
process on first connect (`apply_migrations`, recorded in `schema_migrations`)
unless `AUTO_MIGRATE=false`. Every file in `migrations/` is applied in lexical
order; the runner does not require a contiguous sequence, so the reserved gap at
`0012`/`0013` is safe.

## Migrations

`0001`–`0011`, `0014`–`0039`. The three most recent are the ones this increment
added:

- **`0037_task_kind.sql`** — `nexus_run_tasks.kind` (a `CHECK` of the five
  kinds) and the `completed_no_changes` task state (the state `CHECK` is
  widened). Additive: one column with a default, one widened constraint.
- **`0038_lifecycle.sql`** — `nexus_projects.archived_at`,
  `nexus_projects.deleted_at`, `nexus_conversations.deleted_at` (all nullable,
  `NULL` = live) and two partial indexes. Additive.
- **`0039_conversation_pin.sql`** — `nexus_conversations.pinned` (boolean, not
  null, default false) and a partial index for the pinned-first list. Additive.

All three are safe to apply to a live database and to a fresh one; none rewrites
an existing row. Rolling back is dropping the columns and indexes; no data is
lost by applying them.

### Two `0037`s in the repository's history

Two independent lines of work both wrote a `0037` migration. This branch's is
`0037_task_kind.sql`; the other line (`arena/ab0d7665`, PR #38) wrote
`0037_project_conversation_lifecycle.sql` with the same `deleted_at`/`archived_at`
columns this branch has in `0038`. The runner keys on the **filename**, not the
number, and applies `*.sql` in lexical order, so:
- the two files cannot both be in the tree (this branch keeps only its own);
- the column the other line had and this one lacked (`pinned`) was added under a
  fresh number, `0039`;
- an already-migrated database re-runs nothing (`schema_migrations` is keyed on
  the filename).

`tests/test_lifecycle_migrations.py` proves a clean install, an upgrade from a
schema without the three files, and a re-apply that changes nothing.

## Tables and their relationships

- **`nexus_projects`** is the tenant boundary. Everything below is scoped to it
  by `project_id`, and a non-member gets `404`, never `403`.
- **`nexus_project_members`** carries the principal's role (`owner`/`member`).
  `SCOPE_PROJECT_ADMIN` gates rename, archive, delete and purge.
- **`nexus_project_repositories`** is the project↔repository link (many-to-many,
  with a GitHub account, `is_primary`, `working_branch`). `authorize_dispatch`
  and `authorize_run_task` read it; a repository can be shared by several
  projects.
- **`nexus_conversations`** belongs to a project (`ON DELETE CASCADE`) and holds
  the chat, its mode, its workflow state and (nullable) its `run_id`. Its
  messages and attachments cascade.
- **`nexus_runs`** belongs to a project and a goal; **`nexus_run_tasks`** to a
  run. Both cascade from the project. `nexus_task_executions` records one row per
  conversation a task was given.
- **`nexus_evidence`** references the project `ON DELETE SET NULL`, so evidence
  outlives a project (its content stays; only the link goes).
- **`nexus_conversation_cleanups`** references the run, project and account
  `ON DELETE SET NULL`: a cleanup row is the record that a conversation was
  deleted, so it is never removed by a cascade.

### Cascade policy, stated

- `ON DELETE CASCADE` — the project's own data: conversations, messages,
  attachments, runs, run tasks, goals, specs, files, workspaces, memory,
  project executors, repository links.
- `ON DELETE SET NULL` — records that outlive a project: evidence, legacy tasks,
  cleanup rows, `nexus_runs.conversation_id` (a run survives its chat being
  purged).

The external GitHub repository is **never** touched by a delete or a purge: only
the link row goes; the `repositories` allowlist entry stays.

## Indexes that matter

- `uq_tasks_workspace_active` (partial unique on
  `(selected_repository, COALESCE(selected_branch,''))` over the active states) —
  the workspace lock, claimed inside a `SAVEPOINT`.
- `idx_nexus_run_tasks_run`, `idx_nexus_run_tasks_state`,
  `idx_nexus_task_executions_open` (partial, `ended_at IS NULL`) — the claim and
  poll paths.
- `idx_nexus_projects_live`, `idx_nexus_conversations_live` (partial, over
  `deleted_at IS NULL`) — the lifecycle lists added by `0038`.
- `idx_nexus_conversations_pinned` (partial, over `deleted_at IS NULL`) — the
  pinned-first chat list added by `0039`.

## Orphan records

No project-scoped table can be orphaned by a project delete: the ones that
belong to the project cascade, and the ones that outlive it null their
`project_id`. `tests/test_lifecycle.py` asserts that purging one project leaves
another's conversations, runs and repository links intact, and that the external
repository row is still there.

## The schema guard

`PostgresStore.verify_schema()` names any table (or column) the store depends on
that is missing, so a forgotten migration is one clear message instead of a 500
on the first request. `REQUIRED_COLUMNS` in `cloud_store.py` is the list; a new
column the store reads must be added there (a test asserts the lifecycle and
`kind` columns are).
