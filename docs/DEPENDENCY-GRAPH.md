# Dependency graph

What references what, and what happens to a dependent when its parent is
removed. Derived from `migrations/*.sql`; the census a permanent project delete
takes (`ProjectService._census`) counts exactly the CASCADE rows.

## The graph

```
project ─┬─ conversations ─┬─ messages ─── attachments
         │                 └─ generations
         ├─ goals ── runs ── run_tasks ── task_executions
         │            │        └─ handoffs
         │            ├─ run_reports
         │            ├─ executor_stops
         │            └─ conversation_cleanups
         ├─ specs ── spec_sections / spec_requirements / spec_links / spec_stages
         ├─ plans ── plan_tasks ── plan_dependencies
         ├─ files / workspaces / workspace_files / voice_notes
         ├─ project_context / memory
         ├─ project_repositories   (→ repositories, global)
         ├─ project_executors      (→ openhands_accounts, global)
         ├─ notifications
         └─ project_members        (→ the principal)

global (not project-scoped): repositories, github_accounts, openhands_accounts,
  openhands_pool, sessions, login_attempts, lab_workers, schema_migrations
```

## Foreign-key actions (the rule for each edge)

| Parent → child | On delete of the parent | Why |
| --- | --- | --- |
| project → conversations, goals, specs, plans, files, workspaces, voice_notes, context, memory, runs, run_tasks, notifications, events, project_repositories, project_executors, project_members | **CASCADE** | The child has no meaning without the project. A permanent project delete is the one place this is intended, and it is guarded (trash first, no active run). |
| conversation → messages, attachments, generations | **CASCADE** | A conversation's transcript is the conversation. |
| conversation → runs.conversation_id | **SET NULL** | A run is a real execution with its own history; deleting the chat that asked for it must not erase the run. |
| conversation → specs.conversation_id | **SET NULL** | The specification is the task text; the chat is where it was typed. |
| conversation → agent_sessions.conversation_id | **SET NULL** | The mapping to an OpenHands session is a side record. |
| run → run_tasks, run_reports, executor_stops, handoffs, task_executions | **CASCADE** | A run's parts are the run. |
| repository → tasks.repository_id / project_repositories | **SET NULL** / CASCADE | The allowlist row is a link; removing it does not delete a task. |
| account (GitHub/OpenHands) → project bindings | **SET NULL** / refuse | An account removal is refused while a project depends on it; the history is kept. |
| project → evidence.project_id | **SET NULL** | Evidence is the durable proof and is kept for audit even after the project is gone. |

## What a permanent project delete removes (the census)

`GET /api/v1/projects/{id}/dependents` returns a count per project-scoped table,
so the confirmation names what is at stake. The set is
`ProjectService._PROJECT_TABLES`: `nexus_conversations`, `nexus_goals`,
`nexus_plans`, `nexus_files`, `nexus_workspaces`, `nexus_voice_notes`,
`nexus_project_context`, `nexus_memory`, `nexus_runs`, `nexus_run_tasks`,
`nexus_evidence`, `nexus_specs`, `nexus_notifications`,
`nexus_project_events`, `nexus_project_repositories`, `nexus_project_executors`,
`nexus_project_members`, plus `active_runs` (the count that blocks the delete).

## What is never cascaded by accident

- **Repositories (GitHub).** `nexus_project_repositories` links a project to a
  repository; deleting the project removes the *link*, never the GitHub
  repository.
- **Accounts.** GitHub and OpenHands accounts are global; a project delete does
  not touch them.
- **The users/principals.** Membership rows cascade; the principal is a string,
  not a row that can be lost.
- **Evidence.** Kept (SET NULL) for audit even after the project is gone.
- **A run's history.** Removing a conversation leaves its runs; removing a run
  leaves its evidence.

## Orphan prevention

Every destructive route is a transaction: the census and the delete commit
together, so the answer cannot describe a different set of rows than the delete
removed. The store's `_tx` rolls back on any error, so a partial delete cannot
persist.
