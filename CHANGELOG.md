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

All notable changes to NEXUS. The format follows Keep a Changelog; the project
is versioned in `pyproject.toml`.

## Unreleased — master completion, synchronization and release (2026-11-14)

### Fixed

- **Migrations: an edited, already-applied file is refused, not re-run.**
  `PostgresStore.apply_migrations` used `ON CONFLICT DO UPDATE` and executed a
  changed body under its recorded name, so the history could claim one thing
  while the schema was another. It now raises `MigrationError`, naming the file.
- **A second chat answer is refused cleanly.** `_chat_turn` let the
  one-generation-per-conversation `ValueError` surface as a raw 400 and left the
  conversation in `thinking`; it is now a 409 with a clear message and an idle
  conversation (the running answer stays stoppable).
- **Deleting a run clears the goal's `active_run_id`.** The column has no
  foreign key, so removing the run left the goal pointing at a gone row.
- **A bare follow-up question keeps reading the run state** (ported from PR #38):
  a pronoun follow-up after an `analysis` turn stays `analysis` instead of being
  answered with no facts.

### Added

- The §10 regression suite (`tests/test_regression_p0_suite.py`) and the
  concurrent-runs architectural contract
  (`tests/test_concurrent_runs_policy.py`); the runs list route accepts
  `?include_hidden=1` as an alias of `?archived=1`.
- Documentation: `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
  `docs/PRODUCTION_READINESS.md`, `docs/BRANCHES.md`,
  `docs/INTEGRATION_TESTING.md`, `docs/KNOWN_LIMITATIONS.md`,
  `docs/GAP-ANALYSIS.md`, `docs/GROUND-TRUTH-BUILD-LINT-TEST.md`,
  `docs/P0-INCIDENT-FINAL-REPORT.md`, and `tools/verify_build_lint_test.sh`.

## Unreleased — NEXUS Ultimate increment

The four phases below are independent, each with its own migration and tests.
No existing behaviour was removed; every change is additive.

### Added — chat command system (Phase 4)

- `orchestration/nexus_commands.py`: one pure parser and registry for the chat
  command vocabulary — `/chat /plan /autorun /audit /fix /test /review /deploy
  /research /status /help` — the four hashtag groups (technology, quality,
  execution, restriction) and typed parameters (`--repo`, `--branch`).
- Restrictions have priority: `#read-only` downgrades an execute command to a
  read-only analysis and reports the downgrade; a restriction no downgrade can
  satisfy (`/deploy #no-deploy`) is refused (400).
- Only real command and tag tokens are stripped from the request, so a path
  (`/tmp/file`) or an issue number (`#1234`) is left exactly as written.
- The parser runs in `_chat_turn` before the Intent Router: an explicit command
  sets the turn's mode, and an execute command is never turned into a
  clarifying question. A UI button posts `{"action": "/plan"}` and goes through
  the same parser, so buttons and slash commands cannot drift apart.
- The command decision is stored on the user turn (`meta.command`) and carried
  into the task text the planner and the agents read.
- `GET /api/commands[?q=]` serves the vocabulary and completions.
- The studio composer shows a command menu and a stop button.

### Added — conversation management (Phase 3)

- Migration `0037_conversation_management.sql`: `nexus_conversations.pinned`,
  `pinned_at`, `deleted_at`; the `nexus_generations` table.
- Pin/unpin: durable, survives a restart, orders the pinned section by pin time.
  Pinning is not activity, so unpinning restores the old order.
- Soft delete/restore: `deleted_at` is real (the conversation leaves every list)
  and reversible; the project, its repository and the transcript are untouched.
- Search by title and by the text of messages; `GET …/conversations?deleted=1`
  is the trash.
- `GET …/conversations/{id}/export`: a self-contained JSON document with no
  secret in it.
- The studio sidebar has a pinned section, an archive and a trash with restore.

### Added — stop generation (Phase 3)

- `orchestration/nexus_generation.py`: a `GenerationToken` carries the live
  provider response; a cancel trips the signal and closes the response, so a
  blocked read raises at once — the stop is immediate, not "after the model
  finishes".
- One running generation per conversation (unique partial index), so a stop is
  unambiguous.
- The partial text produced is saved with a cancelled mark; the conversation is
  left idle and immediately usable.
- `POST …/conversations/{id}/cancel`; 409 when nothing is running.
- Stopping a text answer never touches an autonomous run, and vice versa.
- The studio's send button becomes a stop button while a text answer generates.

### Added — run history management (Phase 6)

- `LabStore.retry_failed`: requeues *only* the tasks that did not verify and
  puts the finished run back to work; a verified task is never requeued, and a
  run with nothing to retry is refused (409).
- `LabStore.delete_run`: removes a finished run's own record and nothing else.
  The goal, the specification, the chat, the evidence and the repository are all
  untouched; an active run is refused.
- Routes `POST …/runs/{id}/show|retry-failed`, `DELETE …/runs/{id}`.
- The studio's run card offers "retry failed", "hide" and "delete from history"
  on a finished run.

### Added — performance tooling (Phase 2)

- `tools/benchmark.py`: reproducible measurements of the real request paths
  (studio status, list/open project, list/open conversation, history, chat turn,
  run start, store counts, file upload) against a real server and PostgreSQL.
- `tools/benchmark_compare.py`: prints the deltas between two benchmark runs.
- `docs/PERFORMANCE.md`: the method and a baseline.
- Indexes for the new list order (`idx_nexus_conversations_pinned`,
  `idx_nexus_conversations_live`).

### Added — documentation (Phase 15)

- `docs/COMMANDS.md`, `docs/PERFORMANCE.md`, this `CHANGELOG.md`; the README
  gained the command system, conversation management and run history.
