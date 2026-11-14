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

Final full-suite numbers are recorded in the pull request and in the commit
history (run on PostgreSQL 17 against a disposable database). The baseline was
1734 passed / 1 failed / 44 skipped; this branch is compared against that.

## 6. Known limitations (honest)

- A live OpenHands key and a real GitHub token are not present in this
  environment, so the live dispatch, the live verification and the live agent
  action were not exercised here; they are covered against the stand-in servers
  in `tests/`. The stages that need them are listed under `not_measured` by
  `tools/benchmark.py`.
- The stage-latency table is empty until `NEXUS_TRACE=1`; that is by design
  (collection is opt-in) and stated in the UI.
- The studio's new context menus and dialogs are covered by the structure tests
  (`tests/test_studio_ui.py`) and the HTTP tests; the Chromium journey tests
  cover the surrounding flows. They were not re-run under Chromium in this
  session for the new controls specifically.
