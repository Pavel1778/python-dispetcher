# NEXUS — Arena Engineering Changelog (restoration, 2026-11-16)

Branch `arena/3a132869-python-dispetcher` restores the independent
engineering program onto the consolidated `main` (`42824896`, PR #45
"consolidate/final"). Full disclosure of what happened and what was
rebuilt:

## What was lost, and recovered from where

| Artifact | Lost? | Recovery |
| --- | --- | --- |
| Entire pre-consolidation branch `arena/b10d2a83-python-dispetcher` (fc026be…31bd3ec) | branch deleted from remote when `main` was consolidated | fetched from `refs/pull/43/head` (= `31bd3ec`); docs restored verbatim |
| Pause-honesty UI fix (fc026be/12fdf02) | incorporated into `main` by PR #45 ("consolidate: pause-honesty UI fix") | **already in this base** — verified at HEAD (app.js:1357/1459/1492/3209), NOT re-applied |
| `tests/test_terminal_output.py` (45 tests) | **not carried into this base** (ledger: symbol drift; superseded) | **deliberately not restored** — this tree's terminal-output API is `orchestration/terminal_output.py`; covered by `tests/test_artifacts.py` (15 tests) |
| `docs/PERFORMANCE.md` 2026-11-15 appendix | file rewritten by the consolidation | **not restored** — its baseline used a different connection layer; stale claims removed |
| `docs/REPORT-A-OPENHANDS-INTEGRATION.md`, `REPORT-B-NEXUS-PLATFORM.md`, `EVOLUTION-AUDIT-2026-11-15.md` | missing | restored from `refs/pull/43/head`, annotated with restoration notes |
| F-01: CLI scheme guard (`cli.py`) + `tests/test_cli_client.py` | unpushed local commit `49747b8` — lost with the old clone | **recreated** (same edit, same 5 tests + restoration note) |
| Arena deliverable docs (audit/security/rust/ux/logic/perf/this changelog) | unpushed local commit `49747b8`+`95af13f` — lost | **recreated**, updated for the consolidated base; no stale numbers carried |
| Third full-suite log (1920p/49s at `49747b8`) | wiped with the sandbox (`pytest_full3.log` gone) | this branch runs its own suite; result recorded below |

## Why "restore" ≠ "re-apply blindly"

The consolidation changed the store (psycopg pool), terminal outputs, and
CI (Chromium E2E works on the runner). Blindly restoring the old tree
would have resurrected a superseded implementation and stale benchmark
numbers. Restores were either (a) exact-file from `refs/pull/43/head`,
(b) rebuilt with content re-verified against this base, or (c) explicitly
dropped with the reason recorded.

## What changed in this branch (on top of `42824896`)

- `orchestration/cli.py` — F-01 scheme guard (config exit 3 on non-http(s)
  base URLs).
- `tests/test_cli_client.py` — 5 regression tests.
- `docs/REPORT-A-...`, `docs/REPORT-B-...`, `docs/EVOLUTION-AUDIT-2026-11-15.md`
  — restored, annotated.
- New/recreated deliverables: `ARENA_ENGINEERING_AUDIT.md`,
  `SECURITY_AUDIT.md`, `RUST_MIGRATION_ASSESSMENT.md`, `UX_AUDIT.md`,
  `LOGIC_AND_RELIABILITY_AUDIT.md`, `PERFORMANCE_REPORT.md`, this file.
- `orchestration/nexus_lab_store.py` — F-06 (below): bounded (×1) retry of a
  settle aborted by a PostgreSQL deadlock, both `set_run_state` variants;
  `tests/test_deadlock_retry.py` (2 deterministic tests).
- `refs/remotes/origin/pr-43` retained locally (not a push) so the old line
  remains recoverable.

## Verification in THIS branch

| Check | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `pytest tests/test_cli_client.py tests/test_studio_ui.py tests/test_artifacts.py` | **46 passed** |
| Full suite, attempt 1 (`/home/user/pytest_full5.log`) | **1937 passed, 1 failed, 49 skipped, EXIT=1** — one intermittent `psycopg.errors.DeadlockDetected` in `test_after_the_run_has_ended_the_same_text_runs_again_from_the_same_plan`; passes 33/33 in isolation |
| Full suite, attempt 2 after the deadlock fix (`/home/user/pytest_full6.log`) | **1940 passed, 49 skipped, 0 failed — 35m50s, EXIT=0** |

## Honesty notes

- No claim of green full-suite for this branch until the run above exits 0.
- Numbers from the pre-consolidation program (1915/1920 passed; perf tables;
  36-38 min) describe that tree, not this one, and are labelled as such.
- No push performed. `main` untouched. No merge performed.
- Environment: fresh venv from `requirements.txt` + `requirements-dev.txt`;
  PostgreSQL 16.2 via `pgserver` (socket `/home/user/pgdata-nexus`);
  `TEST_SUPABASE_DB_URL` required.
