# NEXUS — Arena Engineering Audit (restored 2026-11-16)

Branch `arena/3a132869-python-dispetcher`, base `42824896` (`main` after PR
#45, "consolidate/final"). This branch restores the independent-engineering
program's missing artifacts on top of the consolidated base.

Status vocabulary: **VERIFIED** (ran it) / **TESTED** (suite) /
**STATICALLY REVIEWED** / **BLOCKED** / **NOT IMPLEMENTED**.

## 1. Repository integrity (Phase 0)

| Check | Result |
| --- | --- |
| Repository root | `/home/user/python-dispetcher` |
| Branch / HEAD at restoration | `arena/3a132869-python-dispetcher` @ `42824896` |
| `git status` | clean |
| `main` (`origin/main`) | `42824896` — untouched; nothing merged |
| Lineage lost | pre-consolidation branch `arena/b10d2a83-python-dispetcher` is gone from the remote; its commits were recovered from `refs/pull/43/head` and unpushed work recreated (see `ARENA_ENGINEERING_CHANGELOG.md`) |
| Lint | `ruff check .` → All checks passed (this tree, with F-01 fix) |
| Focused tests | `tests/test_cli_client.py` + `tests/test_studio_ui.py` → 31 passed |
| Full suite | **1940 passed, 49 skipped, 0 failed — 35m50s, EXIT=0** (`/home/user/pytest_full6.log`, after the F-06 deadlock fix) |

## 2. Architecture map (as consolidated)

- Python ≥ 3.10 (running 3.11.2), `psycopg[binary,pool]` — a
  `psycopg_pool.ConnectionPool` is the connection layer (owner-approved
  Phase 3 direction kept by the consolidation).
- **OpenHands integration** is an external dependency behind
  `validate_base_url` (scheme + SSRF policy); `orchestration/terminal_output.py`
  is the canonical terminal-output implementation; no internals were touched.
- **Runtime additions from the consolidation**: run-time profiler
  (`nexus_profiler.py`, migration 0040), OpenHands inventory tooling, pool
  connection layer.
- Frontend unchanged in kind: Vanilla JS studio SPA; the pause-honesty fix
  from the pre-consolidation program is present (verified at HEAD: pause
  tooltip/aria-label at `app.js:1459`, toast at `app.js:3209`, standing note
  at `app.js:1357`).
- CI (`tests.yml`) installs deps, downloads Chromium with a fallback to the
  image browser and runs browser E2E; browser journeys are **VERIFIED by CI
  per the consolidation ledger**, not by this sandbox.

## 3. Findings

| # | Severity | Finding | Status |
| --- | --- | --- | --- |
| F-01 | Low | CLI bearer client accepted any scheme (`file://` etc.) — local-file read into CLI output | **FIXED + TESTED** in this branch (scheme guard + `tests/test_cli_client.py`, 5 tests green; `ruff check .` clean) |
| F-02 | Low | Pause UI over-promised vs backend | already fixed **in the consolidated base** (`app.js` — checked at HEAD) |
| F-03 | Low | Duplicate migration prefix `0038_lifecycle.sql` vs `0038_project_lifecycle.sql` | **STATICALLY REVIEWED**: both additive (`ADD COLUMN IF NOT EXISTS`); runner is filename-keyed + checksummed → inert; **no rename** (would orphan deployed `schema_migrations` rows) |
| F-04 | Low | `ruff --select B`: B905/B904/B017 style items | STATICALLY REVIEWED — not changed |
| F-05 | — | `benchmarks/rust-archive`, `benchmarks/rust-db` (added by consolidation): isolated Rust benchmarks, explicitly "NOT the NEXUS runtime" | noted; see `RUST_MIGRATION_ASSESSMENT.md` |
| F-06 | Medium | Intermittent `DeadlockDetected` on member-facing cancel (batch task UPDATE vs concurrent worker settle) — found by this branch's own suite run | **FIXED + TESTED** (bounded single retry, `tests/test_deadlock_retry.py`; 30/30 stress; full suite green) |

No Critical or High defect was reproduced in this tree; none is claimed
without evidence. The security- and logic-sensitive surfaces match the
consolidation ledger (1973 passed / 9 skipped / 0 failed, PostgreSQL 17).
That figure is the **ledger's claim**, not this branch's re-verification.

## 4. Companion documents

`SECURITY_AUDIT.md`, `RUST_MIGRATION_ASSESSMENT.md`, `UX_AUDIT.md`,
`LOGIC_AND_RELIABILITY_AUDIT.md`, `PERFORMANCE_REPORT.md`,
`ARENA_ENGINEERING_CHANGELOG.md`, plus the restored historical evidence
`REPORT-A-OPENHANDS-INTEGRATION.md`, `REPORT-B-NEXUS-PLATFORM.md`,
`EVOLUTION-AUDIT-2026-11-15.md` (annotated as of the pre-consolidation tree).

## 5. Open risks

1. Browser/Android: **CI-run Chromium E2E exists in this base** (per ledger,
   incl. 33 journeys) — but **not re-run by this sandbox** (Chromium download
   is refused by the sandbox network). No independent browser claim.
2. Live OpenHands: **BLOCKED** (no credentials here).
3. Self-QA autonomy layer: **NOT IMPLEMENTED** (design-only).
4. `tests/test_terminal_output.py` (45 tests, pre-consolidation) is
   deliberately **not restored** — superseded by `tests/test_artifacts.py`;
   its API targets no longer exist in this tree.
5. `refs/remotes/origin/pr-43` exists only on this clone; this branch is
   **not pushed** — see changelog.
