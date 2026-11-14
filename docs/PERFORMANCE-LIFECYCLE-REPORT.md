# Performance, reliability and lifecycle — implementation report

This report states what was measured, what was changed, and what is verified.
It is written so a reader can check every claim; nothing here is asserted
without an artefact behind it.

## 1. Baseline

- Repository: `Pavel1778/python-dispetcher`, branch `main`, HEAD
  `42609a41252d8ef5ed9593d84be510fd07f367fa`.
- Working branch: `nexus/perf-reliability-lifecycle`.
- Baseline test run (PostgreSQL 17, local): **1734 passed, 1 failed, 44
  skipped** in 22:58. The one failure
  (`test_auth.py::test_expired_sessions_and_stale_attempts_are_swept`) passes on
  its own and is a pre-existing, load-sensitive flake — not from this work.
- Baseline queue benchmark (`tools/benchmark.py`): claim p50 4.45 ms, p95
  5.11 ms, p99 6.96 ms, max 7.43 ms.

## 2. Root causes found (with evidence)

1. **False acceptance.** `GitVerifier.verify` accepted a task when the report
   named the head of the branch. An agent whose push was refused names the head
   it found, so a task with no new change was recorded `completed_verified`.
   Evidence: `tests/test_run_reports.py` (before: the refused-push test asserted
   the run completed; now it asserts the task is reworked).
2. **No per-stage timing.** Nothing measured dispatch, poll or verify, so
   "NEXUS is slow" had no number. Evidence: no `duration_ms` anywhere in the
   code before `orchestration/tracing.py`.
3. **The "running + pending" state was invisible.** A due, unclaimed task looked
   the same as a busy one. Evidence: `queue_snapshot`/`stuck_tasks` did not
   exist.
4. **No project or conversation delete.** Only rename/archive existed, and
   `DELETE …/conversations/{id}` merely archived. Evidence: the old
   `do_DELETE` called `set_archived`.

## 3. What was changed

- **Evidence-based acceptance** (`orchestration/nexus_lab.py`,
  `nexus_lab_store.py`, `nexus_planner.py`, migration `0037_task_kind.sql`): a
  change-requiring task is accepted only when the branch moved; a
  `verification_only`/`research_audit` task is accepted as
  `completed_no_changes`. The nine concepts the request names are separated.
- **Stage tracing** (`orchestration/tracing.py`) and `GET /api/observability`.
- **Project/conversation lifecycle** (`nexus_projects.py`,
  `nexus_conversations.py`, `web_server.py`, migration `0038_lifecycle.sql`),
  with soft delete, restore, guarded purge, and studio context menus.
- **Benchmark** (`tools/benchmark.py`).

## 4. Verification (what was run)

- `ruff check .` — clean.
- The full suite on PostgreSQL 17 (see §5 for the final numbers).
- `tests/test_run_binding.py` — the ForgeCRM incident regression (run
  `2ae6adc3`): a task for another project's repository is never started.
- `tests/test_lifecycle.py` — lifecycle and isolation (a purge touches nothing
  else and never the external repository).
- `tests/test_observability.py` — tracing and the observability endpoint.
- `tools/benchmark.py` — the queue/claim numbers above.

## 5. Test result

After the PR #38 integration, the full suite on PostgreSQL 17 (disposable
database):

```
1811 passed, 9 skipped in 1653.27s (0:27:33)
ruff check .  -> All checks passed!
Browser E2E (Chromium): 37 passed
```

The baseline was **1734 passed, 1 failed, 44 skipped** (the single failure,
`test_auth.py::test_expired_sessions_and_stale_attempts_are_swept`, is a
pre-existing load-sensitive flake; it passed 5/5 on its own here). The skip
count dropped from 44 to 9 only because Chromium is now installed, so the 29
browser journeys that used to skip now run.

**Where the count difference comes from.** The number of *collected* tests on
`main` is 1779; on this branch it is 1820 (+41), all from new files this branch
adds or from ported tests, not from tests removed:

- `tests/test_lifecycle.py`, `test_observability.py`, `test_run_binding.py`,
  `test_lifecycle_migrations.py` — new;
- `tests/test_concurrent_runs_policy.py`, `test_regression_p0_suite.py` — ported
  from PR #38;
- one renamed test (`test_a_run_in_which_nothing_was_pushed_is_not_reported_as_done`
  → `test_a_push_that_was_refused_is_not_accepted_and_is_reworked`), on purpose.

No test was deleted or its assertion weakened; the `main` collected set is a
subset of this branch's.

## 6. Known limitations (honest)

- A live OpenHands key and a real GitHub token are not present in this
  environment, so the live dispatch, the live verification and the live agent
  action were not exercised here; they are covered against the stand-in servers
  in `tests/`. The stages that need them are listed under `not_measured` by
  `tools/benchmark.py`.
- The stage-latency table is empty until `NEXUS_TRACE=1`; that is by design
  (collection is opt-in) and stated in the UI.
- The studio's new context menus and dialogs are now covered by real Chromium
  journeys as well (`tests/test_browser_e2e.py`: a chat pinned/trashed/restored/
  purged and a project deleted/restored, driven through the menus) plus the
  structure tests (`tests/test_studio_ui.py`) and the HTTP tests.

## 7. PR #39 finalization: history reconciliation

The previous stage's report named a line of work that **does not exist in this
repository** — `5c4f08c`, `orchestration/nexus_commands.py`,
`orchestration/nexus_generation.py`, `LabStore.retry_failed`,
`LabStore.delete_run`, `tools/benchmark_compare.py`, and a prior `0037` for
command/dialog management. Every one was searched for across every ref
(`git rev-list --all`, `git grep` on all branches, `git log --all -- <path>`)
and none is present; the only `0037` files are this branch's
`0037_task_kind.sql` and `arena/ab0d7665`'s
`0037_project_conversation_lifecycle.sql`.

What *is* real is a parallel line, **PR #38** (`arena/ab0d7665`, 4 commits from
`42609a4`), which implemented the same project/conversation lifecycle
independently. The two lines collided on migration number `0037`. This branch
keeps its lifecycle as the superset and ports PR #38's two unique pieces
(conversation pin; analysis-turn continuity) plus its concurrent-runs contract
test, under a fresh migration number `0039`. See [../CHANGELOG.md](../CHANGELOG.md)
and [DATABASE.md](DATABASE.md), "Two `0037`s in the repository's history".

Bugs found while verifying the new menus over Chromium (all fixed):

- `ProjectService.stats` passed one parameter to a two-placeholder query (the
  active-runs count) — a `500` on the delete confirmation, previously untested.
- `GET …/stats` was `POST`-only, so the studio's confirmation never opened.
- `loadChats` did not fetch the trash by default, so the trash fold showed no
  count.
