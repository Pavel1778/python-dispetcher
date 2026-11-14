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
