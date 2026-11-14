# Changelog

All notable changes to NEXUS. The format follows Keep a Changelog; the project
is versioned in `pyproject.toml`.

## Unreleased — production release verification + complete CRUD (2026-11-14)

### Fixed

- **`0037` failed on any server with the Supabase roles (production-blocking).**
  Its revoke loop variable was `record`, not `text`, so the statement became
  `REVOKE ... FROM "(authenticated)"` and the migration failed with
  `role "(authenticated)" does not exist`. Production has that role, so `0037`
  never applied there and every deploy that included it crashed at startup —
  which is why the live service stayed on the old build. Fixed; a regression
  test applies every migration on a database that has the roles.

### Added — complete CRUD

- **Permanent delete** for conversations (`DELETE …/conversations/{id}?permanent=1`)
  and projects (`DELETE /api/v1/projects/{id}`), both two-step (trash first),
  the project delete refused while a run is active.
- **Dependents census** (`GET /api/v1/projects/{id}/dependents`) so a project
  delete names what it removes.
- **Notification lifecycle**: list with an unread filter, mark read/unread,
  bulk mark-all-read, delete.
- **Session management**: `GET /api/sessions`, `DELETE /api/sessions/{id}`,
  `DELETE /api/sessions` (revoke all others) — never the token.
- Studio: notification centre, sessions dialog, trash "delete for good", project
  archive/trash/purge with the census.
- Docs: `docs/CRUD-MATRIX.md`, `docs/ENTITY-LIFECYCLE.md`,
  `docs/DEPENDENCY-GRAPH.md`, `docs/SECURITY-AUDIT-2026-11-14.md`,
  `docs/MASTER_COMPLETION_REPORT.md`.

### Changed

- The production service now serves the new build (verified: served assets and
  `ASSET_VERSION` match the local build; `/healthz` and `/readyz` pass).

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
