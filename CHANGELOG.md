# Changelog

## Unreleased — performance, execution reliability, project/conversation lifecycle

### Fixed

- **A task could be accepted with no new change** (evidence-based acceptance).
  `GitVerifier` accepted a task whose report named the head of the branch, which
  is true of an agent whose push was refused and that named the head it found.
  The head SHA alone does not prove a new change appeared. A change-requiring
  task (`implementation`, `bugfix`, `documentation`) whose branch did not move
  is now `completed_unverified` and is reworked; it is never reported as
  verified. A task whose kind does not change the code (`verification_only`,
  `research_audit`) is accepted as `completed_no_changes` - an honest no-change
  result, not a claim of a change. (`orchestration/nexus_lab.py`,
  `migrations/0037_task_kind.sql`.)
- **A project or conversation could not be deleted.** Only rename and archive
  existed, and `DELETE …/conversations/{id}` merely archived. Projects and
  conversations can now be archived, moved to the trash, restored and purged,
  with an active-run guard. (See [docs/PROJECT-LIFECYCLE.md](docs/PROJECT-LIFECYCLE.md),
  `migrations/0038_lifecycle.sql`.)

### Added

- **Stage tracing** (`orchestration/tracing.py`): one secret-free span shape for
  the dispatch, poll and verify stages, with the ids of the attempt. Off unless
  `NEXUS_TRACE=1`.
- **`GET /api/observability`**: queue depth, stuck tasks, worker heartbeat and
  per-stage latency. The studio shows it under **Настройки → Диагностика**.
  (`LabStore.queue_snapshot`, `LabStore.stuck_tasks`.) See
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
- **`tools/benchmark.py`**: measures the queue/claim path over real PostgreSQL
  and is honest about what it cannot measure here.
- **Project and conversation context menus** in the studio: archive, delete,
  restore, purge, with a confirmation that names what is inside and states that
  the external GitHub repository is not touched.
- Tests: `tests/test_run_binding.py` (the ForgeCRM incident regression),
  `tests/test_lifecycle.py` (project/conversation lifecycle and isolation),
  `tests/test_observability.py` (tracing and the observability endpoint).

### Documentation

- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) — where the time goes and how to
  measure it.
- [docs/PROJECT-LIFECYCLE.md](docs/PROJECT-LIFECYCLE.md) — the lifecycle
  contract.
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) — the diagnostics surface.

### Migrations

- `0037_task_kind.sql` — `nexus_run_tasks.kind` and the `completed_no_changes`
  state. Additive.
- `0038_lifecycle.sql` — `archived_at`/`deleted_at`. Additive.
- `0039_conversation_pin.sql` — `nexus_conversations.pinned`. Additive.

## PR #39 finalization — history reconciliation and integration

The previous stage's report named a line of work (`5c4f08c`, `nexus_commands.py`,
`nexus_generation.py`, `LabStore.retry_failed`/`delete_run`,
`benchmark_compare.py`) that **does not exist in this repository** (checked
across every ref). The real parallel line is **PR #38** (`arena/ab0d7665`),
which implemented the same project/conversation lifecycle independently and
both lines claimed migration `0037`. This branch keeps its lifecycle as the
superset and ports the two things PR #38 had that it lacked:

- **Conversation pin** (`0039_conversation_pin.sql`, `set_pinned`, `PATCH
  pinned`, the studio menu + `i-pin` icon). A fresh number keeps both `0037`s
  distinct (the runner keys on the filename).
- **Analysis-turn continuity** (`web_server._continues_analysis`): a bare
  follow-up question after an analysis turn stays on the run it found.
- Ported `tests/test_concurrent_runs_policy.py` and an analysis-continuity
  regression; new `tests/test_lifecycle_migrations.py`.

Bugs found and fixed while verifying the new menus over Chromium:

- `ProjectService.stats` passed one parameter to a two-placeholder query (the
  active-runs count) — a 500 on the delete confirmation.
- `GET …/stats` was POST-only, so the studio's confirmation never opened; a
  read is now served on GET too.
- `loadChats` now always fetches the trash so the fold shows a real count.
