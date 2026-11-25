# Changelog

## Unreleased — 2026-11-25

### Added

- PostgreSQL and SQLite dispatcher scan benchmarks, targeted SQL injection
  regression vectors, and a 100-worker claim uniqueness test.

### Changed

- Both dispatchers filter active task states in the store rather than fetching
  historical tasks; SQLite's dispatch gate reuses its queued snapshot. The
  Cloud dispatcher fetches bound conversation IDs in one batch instead of one
  query per active task.
- The missing-env self-hosting test now uses a fake `psql` so that it exercises
  the configuration guard even when PostgreSQL client tools are absent.

## Unreleased — the evidence viewer: the Trust Engine in the Studio

### Added

- **The Evidence Trust Engine is visible in the Studio.** The Agent tab has a
  «Доказательства» section: it lists the project's signed records (repository,
  short commit, verdict, time) and «Проверить» re-verifies one against the
  trusted keyring, showing the server's own verdict and reason under the row.
  The screen decides no verdict; it calls `GET …/evidence` and
  `POST …/evidence/{id}/verify`, which already existed and are tested.
  (`studio/app.js`, `studio/app.css`; tests `tests/test_evidence_viewer.py` and
  the Chromium journey `test_the_evidence_viewer_lists_records_and_re_verifies_one`.)
- **`/api/studio` reports whether records can be signed** (`lab.evidence`, a
  boolean: signer and keyring present). An empty evidence list can then say
  *why* it is empty — no signer means nothing is recorded — instead of reading
  as "nothing was verified". Presence only; no key, key id or path is returned.

### Changed

- The four whole-dict assertions of `/api/studio`'s `lab` object name the new
  `evidence` field (the trap AGENTS.md warns about): they expect `False` in
  those fixtures, which configure no signer.

## Unreleased — the Self-QA gate is per project, and it works on a real repository

### Added

- **A per-project quality-gate mode** (`nexus_projects.quality_gate`,
  migration `0042_project_quality_gate.sql`): `auto` (the default) runs the
  project's own checks only when its repository declares `.nexus-qa.json`;
  `on` always; `off` never. Set from the Studio's project settings, with
  `PATCH /api/v1/projects/{id}` and `nexus project quality-gate <id> [mode]`.
  `NEXUS_SELFQA_GATE` remains a deployment-wide override. A project that
  declares its checks is now gated without any operator switch — roadmap P1 #1.
- **The two check timeouts are settings** (`NEXUS_SELFQA_CHECK_TIMEOUT`,
  `NEXUS_SELFQA_TOTAL_TIMEOUT`); a junk or non-positive value is the default.

### Fixed

- **The gate never worked on a real project.** A task's `repository` is
  `owner/name`, and the gate ran `git clone owner/name`, which git reads as a
  *path* — every real project came back `blocked` ("repository does not
  exist") while every test passed on a local directory. `clone_url` resolves
  `owner/name` to its https URL; the clone fetches the exact accepted commit
  (a shallow clone cannot check out an arbitrary SHA); a private repository is
  cloned with the project's own account, and the token reaches git through the
  environment only — never argv, never the URL, never a report.
- **A failing project passed after the first task.** A fixed `workdir/checkout`
  was reused by the next run, so the clone failed, the report was `blocked`,
  and a `blocked` gate leaves the verdict alone. Each run now gets its own
  scratch directory.

Tests: `tests/test_quality_gate_policy.py` (33 cases; the clone, the mode
policy and the shared-checkout fix each fail when reverted), and the updated
`tests/test_quality_gate.py`.

## Unreleased — run-time profile (Phase 5), canonical consolidation

### Added

- **A durable run-time profile.** `nexus_runs` gains stage milestones
  (`queued_at`, `claimed_at`, `dispatch_started_at`, `conversation_ready_at`,
  `prompt_sent_at`, `first_agent_event_at`, `verification_started_at`,
  `verification_completed_at`, `completed_at`) written once at each lifecycle
  point (migration `0040_run_profiler.sql`). `orchestration/nexus_profiler.py`
  turns them into per-stage durations, idle time and the dominant bottleneck;
  the run-detail route returns a `profile`, and the studio shows it. A stage
  that was not measured is `None`, never a fabricated zero.
  (`nexus_profiler.py`, `nexus_lab_store.py`, `nexus_lab.py`, `web_server.py`,
  `cloud_store.py`; tests `tests/test_run_profiler.py`, the run-detail case in
  `tests/test_lab_http.py`; doc `docs/RUN-PROFILER.md`.)

### Changed

- **Canonical consolidation.** The three consolidation candidates (the
  owner-approved Phase 3+4 base, the engineering/performance-evolution branch,
  and the Phase 5 profiler) are reconciled into one branch: one
  `terminal_output.py` (the conversation-scoped trajectory transport with the
  ZIP-bomb bound, the `..`-skip and PR #42's recomputed-name source verification),
  the kept connection pool, the engineering docs and the `file_lab` batch insert,
  and a cluster-wide advisory lock around the cluster-global Supabase-role test
  case. See `AGENTS.md`, "Canonical consolidation".

## Unreleased — hardening, audits, CLI, architecture extraction

### Added

- **`nexus migrations`** and **`nexus worker-status`** CLI commands and
  **`GET /api/migrations`** (read-only; applied/pending against `migrations/`;
  behind the read gate; pinned by route-auth and functional tests).
- **`tests/test_project_isolation.py`** — 18-surface cross-project refusal
  battery (non-member gets `NotFound` everywhere: tasks, runs, repositories,
  files, conversations, notifications, context, memory, reports, executors…),
  plus membership-revocation and no-self-grant cases.
- **Docs**: `docs/audit/INITIAL_REPOSITORY_FORENSICS.md` (measured Phase Zero
  inventory), `docs/audit/FEATURE_MATRIX.md` (22 features with executed
  evidence), `docs/architecture/ARCHITECTURE.md` (dependency direction;
  deferred Handler split and why), `docs/performance/BASELINE.md` (startup,
  ~52 MB idle VmRSS, measured claim-path numbers), `docs/security/SECURITY_AUDIT.md`
  (0 critical / 0 high; mediums/lows with mitigations), `docs/deployment/
  SINGLE_VM_OPERATIONS.md` (tested backup/restore rehearsal, Caddy/HTTPS,
  resource budget, RPO/RTO honesty), `docs/mcp/MCP_AND_SELFQA.md` (actual MCP
  surface and self-QA bounds).

### Changed

- **Refactor**: pure HTTP helpers extracted from `web_server.py` into
  `orchestration/web_helpers.py` (no behavior change; re-exported for
  compatibility; auth/route/web/write-gate suites green).
- **Docs**: `OPERATIONS.md`/`CLI_REFERENCE.md` cover the new commands;
  `KNOWN_LIMITATIONS.md` records security follow-ups (generic 500s, cookie
  `Secure` deploy flag, requirements lock) and the load-sensitive flake.

## Unreleased — single VM, claim-path indexes, evidence refresh

### Added

- **One virtual machine, everything on it** (`docs/SINGLE_VM.md`): PostgreSQL
  17, the app and the lab worker via `docker compose` — quickstart, required
  secrets, backups, hardening and honest limits. `docker-compose.yml` now has
  `restart: unless-stopped` on `db`, `app` and `worker` (a VM reboot brings the
  stack back), and no longer carries the dead `HOST`/`PORT` keys (the app
  takes host/port only as CLI flags).
- **Migration `0041_branch_claim_indexes.sql`** — the claim path's
  `_BRANCH_FREE` probes get `(repository, branch)` indexes
  (`nexus_run_tasks`, `nexus_executor_stops`). Measured with
  `EXPLAIN (ANALYZE, BUFFERS)` on 60,000 seeded tasks: Q2 (abandoned probe)
  11.51 ms seq scan → **0.008 ms**; Q1 (active probe) 1.40 ms → **0.037 ms**.
  Claim latency and its lock hold no longer grow with task history.

### Changed

- **One timing-sensitive test observed flaking under full-suite load**
  (2026-11-18, this branch): `test_long_task_cases.py::test_a_restart_while_the_plan_is_being_built_goes_on_from_the_part_it_was_on`
  expects the third stage's planning attempt to have committed before the
  simulated crash (`attempts == [1, 1, 2, 1, 1]`); under 2-CPU saturation the
  kill can land first and yields `[1, 1, 1, 1, 1]`. The run itself stays
  correct in both interleavings (stages planned, one run, `asked` has the
  repeated third request). Passes 6 of 6 in isolation; unrelated to the
  claim-path/migration work. Left untouched deliberately — see
  `docs/KNOWN_LIMITATIONS.md`.
- **Migration-bearing test fixture fixed.** `tests/test_durable_migrations.py`
  simulates a database from *before* the durable-runs work by applying every
  migration except the ones under test; `0041` (created after that state, and
  indexing a table migration `0030` creates) now belongs to the same "new"
  set — caught by a failing full-suite run, not by inspection.
- **Docs refreshed to current reality**: `docs/PERFORMANCE.md` (0041 measured
  before/after; `orjson` measured and recorded as not-adopted with the
  reason), `docs/RUST_MIGRATION_ASSESSMENT.md` (dated 2026-11-18
  re-validation: no toolchain in this sandbox, crates.io unreachable, apt
  refused — the assessment stands; nothing was wired into the runtime),
  README (single-VM pointer), DEPLOY.md, COMPLETION_LEDGER.md,
  DEPLOYMENT_CHECKLIST.md (42 migrations), FINAL_RELEASE_REPORT.md.

## Unreleased — auth-gate guard, packaging hygiene, evidence addenda

### Added

- **A structural guard for the auth gate** (`tests/test_write_gate.py`). The
  web server dispatches explicitly public routes and then refuses everything
  without a credential; the regression this class produces — an endpoint added
  above the gate or a method losing its gate call — passes any hand-maintained
  route list it is not added to. The guard reads `orchestration/web_server.py`
  with `ast` and requires, per method, that every path dispatched *before*
  `self._require_read()` / `self._require_write()` is a member of a small,
  exact allowlist (GET: `/`, `/login`, `/login/state`, `/healthz`, `/readyz`
  plus the static asset maps; POST: `/login`, `/logout`; DELETE/PATCH: none).
  It also fails when the gate itself disappears, and when the allowlist goes
  stale. Discrimination verified: an injected pre-gate route and a deleted
  gate both fail the test. The guard import-analyzes only; it needs no
  database.

### Changed

- **`.dockerignore` added** (there was none): the runtime image's build
  context no longer carries `.git/`, tests, docs, reports, tooling or local
  state into the Docker daemon or the image layers.
- **Post-merge addenda** to `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md`
  (PR #50, combined CI pass, merged 2026-11-18T16:46:28Z as `c0f49fc`) and to
  `COMPLETION_LEDGER.md` (current `main`).

## Unreleased — full terminal output, run controls, test isolation, asset latency

### Added

- **Full terminal output recovery.** The agent-server cuts a terminal
  `ObservationEvent` at 30 000 characters and writes the whole output to a file
  beside the conversation; NEXUS can now read it. The truncated event's text is
  an exact prefix of the file, so the file is found by matching that prefix in
  the conversation's own trajectory archive (the only conversation-scoped way to
  reach it). Routes: `GET …/runs/{run}/tasks/{task}/outputs` (which events were
  cut) and `GET …/runs/{run}/tasks/{task}/outputs/{event_id}` (the full text).
  The studio shows a terminal icon on a task that ran and reads the text on
  demand; the CLI has `nexus run outputs|output`; MCP has
  `list_run_task_outputs` and `get_run_task_output`. The Cloud *app* transport
  cannot reach a sandbox filesystem and says so (`unsupported`), never an empty
  result. See `AGENTS.md`, "Full terminal output recovery".
  (`orchestration/terminal_output.py`, `cloud_direct.py`, `local_runtime.py`,
  `openhands_client.py`, `web_server.py`, `cli.py`, `mcp_server.py`; tests
  `tests/test_artifacts.py`.)

### Changed

- **The run card's controls are compact icon-only buttons** with a real
  accessible name (a tooltip and an `aria-label`), a busy state and a danger
  tone, offered only for the states the server accepts. A paused run says
  honestly what the pause means: it stops new tasks, not a conversation already
  running in OpenHands.
- **A studio asset's gzipped body is computed once, not per request.** Measured
  `static_asset_ms` p50: 7.23 ms → 0.59 ms (`tools/benchmark.py`). The served
  bytes are unchanged.

### Fixed

- **Two pytest processes no longer share one test database.** Each process makes
  its own copy of a local disposable database, and the one test that creates and
  drops the cluster-global Supabase roles takes a cluster-wide advisory lock.
  Before this, running two suites on one database failed in
  `test_schema_bootstrap` and a browser test while each passed alone
  (`tests/test_test_isolation.py`).
- **A ZIP-bomb trajectory archive is refused, not buffered.** The full-output
  reader bounded the *compressed* archive (64 MiB) but read each member with
  `archive.read`, which fully decompresses it: a ~190 KB archive could expand to
  200 MB in memory. Each member is now bounded on its own
  (`OpenHandsClient.bounded_zip_member`, `OUTPUT_MAX_BYTES`), refusing a member
  over the limit before it is held. `tests/test_artifacts.py::
  test_a_zip_bomb_member_is_refused_by_the_decompressed_limit` fails if
  `archive.read` is used again.
- **The browser flake is fixed.** `tests/test_browser_e2e.py::
  test_a_repository_that_cannot_be_read_is_said_so_under_the_answer` failed
  about 2 runs in 5: connecting the repository fired a background
  `context/refresh` (the dialog's "analyze" box was checked) that raced the
  test's forced GitHub 401. `_connect_repository(..., analyze=False)` is used
  for this one test, so nothing races the forced failure. 8/8 isolated runs
  pass; the browser module is 37/37.
- **The test-isolation cases run instead of skipping.** `tests/test_test_isolation.py`
  hardcoded a base database name that may not exist, so two of its cases skipped
  silently. It now derives the base from the session's own DSN (the base marker
  `conftest` records), so all four cases exercise the real path.

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

At the start of this work the report named a line of work (`5c4f08c`,
`nexus_commands.py`, `nexus_generation.py`, `LabStore.retry_failed`/`delete_run`,
`benchmark_compare.py`) that was **not yet in this repository** (checked across
every ref). The only parallel line was **PR #38** (`arena/ab0d7665`), which
implemented the same lifecycle independently with a colliding `0037`; this
branch ported its two unique pieces (conversation pin under a fresh
`0039_conversation_pin.sql`; `web_server._continues_analysis`) plus its
concurrent-runs contract test.

`origin/main` then advanced to `a6bb451` and **now contains** that named work
(`5c4f08c` and the whole commands / generation-cancel / run-history / benchmark
increment, `bcd854b`…`5c4f08c`, plus `002c875 feat(projects): archive and soft
delete`). The finalization is therefore a **merge** of `origin/main` into
PR #39, keeping both lines:

- main's lifecycle (`set_deleted`, `set_pinned`+`pinned_at`, content search,
  `export`, generations, run history, commands, `benchmark.py`/`benchmark_compare.py`);
- this branch's `purge`, `stats`, `active_run`, the `409` active-run refusal,
  `/api/observability`, tracing, `_queue_claim_ms`, and the migration-collision
  test.

### Fixed during the merge

- **`main`'s `0037_conversation_management.sql` failed on a plain PostgreSQL.**
  It declared `r record` and passed it to `format('%I', r)`, which renders
  `"(authenticated)"` (`role "(authenticated)" does not exist`). Now `r text`.
- `ProjectService.stats` passed one parameter to a two-placeholder query (the
  active-runs count) — a `500` on the delete confirmation.
- `GET …/stats` was `POST`-only, so the studio's confirmation never opened.
- `loadChats` did not fetch the trash by default, so the trash fold showed no
  count.

### Migrations

Five files, distinct names, all idempotent: this branch's `0037_task_kind.sql`,
`0038_lifecycle.sql`, `0039_conversation_pin.sql` and main's
`0037_conversation_management.sql`, `0038_project_lifecycle.sql`. See
[docs/DATABASE.md](docs/DATABASE.md).

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
