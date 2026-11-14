# NEXUS — Ground-Truth Build, Lint and Test State

This document is the single authoritative, reproducible record of the actual
build, lint and test state of the NEXUS `main` branch. It exists because the
repository had accumulated **six distinct, mutually inconsistent test-count
claims** (382, 433, 445, 465, 557, 625) spread across older reports. Those
numbers were each true of *some* commit or *some* environment; none of them
describes the current tip. This file replaces them with one measurement that
anyone can reproduce.

If a historical report disagrees with this file, this file wins.

---

## 1. What was measured

| Item | Value |
|---|---|
| Branch | `main` |
| Commit (`HEAD`) | `e85d1c6c53581ee1c8814274fc1d6c20b4637518` |
| Tag | `preservation/autonomous-completion` |
| Commit subject | `docs: autonomous lab, worker, clients and live-verification report` |
| Commit date | 2026-11-09 18:42:28 +0000 |
| Measured on | 2026-11-10 |
| Working tree at measurement | clean except for this evidence increment |

Toolchain (exact versions):

| Tool | Version |
|---|---|
| Python | 3.13.15 |
| pytest | 9.0.3 (pinned in `requirements-dev.txt`) |
| ruff | 0.13.1 (pinned in `requirements-dev.txt`) |
| psycopg | 3.3.8 |
| cryptography | 45.0.7 |
| PostgreSQL server | 17.11 |

---

## 2. Result

| Check | Command | Result |
|---|---|---|
| Build | `python -m pip wheel . --no-deps` | **OK** — `python_dispetcher-2.0.0-py3-none-any.whl` built |
| Lint | `python -m ruff check .` | **All checks passed** (exit 0, no warnings) |
| Tests (collected) | `python -m pytest tests/ --collect-only -q` | **626 tests collected** |
| Tests (run) | `python -m pytest tests/ -q -p no:cacheprovider -rs` | **618 passed, 8 skipped, 1 warning** |

The pytest summary line, verbatim:

```
618 passed, 8 skipped, 1 warning in 340.91s (0:05:40)
```

Two independent full runs on the same commit produced the **same** result
(618 passed / 8 skipped, ~5m40s each), so the number is stable, not a fluke.

---

## 3. Reproduce it

One command produces every number above:

```bash
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/disposable_db" \
  tools/verify_build_lint_test.sh
```

`tools/verify_build_lint_test.sh` runs the real toolchain (it does not mock
pytest, ruff or the build), prints each command, and ends with a `SUMMARY`
block. It never hardcodes an expected count: whatever the run yields is the
result. `TEST_SUPABASE_DB_URL` must point at a **disposable** PostgreSQL
database — the tests create, migrate and truncate tables in it.

Without `TEST_SUPABASE_DB_URL` the DB-backed modules skip cleanly. That run is
useful as a smoke check but is **not** a full verification, and the script says
so. CI (`.github/workflows/tests.yml`) always provides a throwaway PostgreSQL
service, so CI exercises the full set.

---

## 4. The 8 skips, explained

Every skip is environmental, not a failure or a disabled assertion. The suite
skips a test when the tool it needs is genuinely absent, so a developer machine
without the Android SDK is not falsely red.

| # | Test file | Reason |
|---|---|---|
| 4 | `tests/test_android_apk.py` | Android SDK build-tools not installed |
| 3 | `tests/test_windows_exe.py` | mingw-w64 cross-compiler not installed |
| 1 | `tests/test_schema_bootstrap.py` | needs `TEST_POOLER_DSN` (a transaction-mode PgBouncer) |

The Android APK and Windows `.exe` tests are *verification* tests for client
artifacts that are built by `packaging/android/build-apk.sh` and
`packaging/windows/build-exe.sh`. They assert against an already-built artifact;
without the SDK/mingw toolchain there is nothing to assert against, so they
skip. The Debian `.deb` tests do **not** skip — `dpkg-deb` is present, so the
`.deb` is built and inspected for real.

The single warning is a `DeprecationWarning` from CPython's `pty` module
(`os.forkpty()` in a multi-threaded process), raised by
`tests/test_auth.py::test_cli_hash_output_authenticates_over_http`. It is a
Python-version artifact of the test harness, not a defect in NEXUS code.

---

## 5. Reconciling the conflicting historical claims

Each historical number was real for its own commit/environment. They differ for
three reasons: the suite grew over time, some runs had no database (so DB tests
skipped), and some were taken on a different branch.

| Claim | Source | What it actually describes |
|---|---|---|
| 382 passed / 1 skipped | `AGENTS.md`, `docs/MASTER_COMPLETION_AUDIT.md`, `docs/MASTER_COMPLETION_STATUS.md`, `docs/FINAL_ACCEPTANCE_REPORT.md` | `main` @ `bd31381`, **2026-11-08**, before the autonomous-lab work landed |
| 433 passed | `docs/NEXUS-AUDIT.md` | an intermediate audit snapshot (skip count not recorded); not reproducible at the current tip |
| 445 passed / 1 skipped | `AGENTS.md` | the **unmerged `nexus/laboratory-files-github` branch**, not `main` |
| 465 passed / 1 skipped | `docs/MVP_RELEASE_REPORT.md` | `main` after the D-2 project-binding / evidence-trust increment |
| 557 → 625 passed / 1 skipped | `docs/AUTONOMOUS-LAB.md` | the **same 626-test suite** as today, measured with the Android SDK + mingw-w64 present (625 = 618 + 7) |
| **618 passed / 8 skipped** | **this document** | **current `main` tip `e85d1c6`, measured 2026-11-10, without those toolchains** |

Two clarifications the old numbers obscured:

- The "1 skipped" in the older claims is not comparable to today's "8 skipped".
  Those runs either had no Android/Windows toolchain tests yet, or counted the
  pooler test differently. Skip counts move whenever a new conditional test is
  added; only the passed count tracks suite growth.
- **The `AUTONOMOUS-LAB.md` claim of 625 is the *same* 626-test suite measured
  in a different environment, not a bigger suite.** That run had the Android
  SDK and mingw-w64 toolchains installed, so its 7 packaging tests ran instead
  of skipping: `618 + 7 = 625` passed and `8 − 7 = 1` skipped. The total (626)
  is identical. So neither number is wrong — they are the same suite with the
  external toolchains present or absent. This measurement environment lacks
  those toolchains, so the reproducible figure here is 618 / 8.

---

## 5a. Why the number moves without the code changing

The 7 tests that swing between "passed" and "skipped" are exactly the client
*artifact-verification* tests:

- `tests/test_android_apk.py` — 4 tests, need Android SDK build-tools
  (`aapt2`, `d8`, `zipalign`, `apksigner`).
- `tests/test_windows_exe.py` — 3 tests, need `x86_64-w64-mingw32-gcc`.

Install both toolchains and the same commit reports **625 passed, 1 skipped**.
Install neither and it reports **618 passed, 8 skipped**. The 1 residual skip is
always `tests/test_schema_bootstrap.py`'s pooler case, which needs
`TEST_POOLER_DSN` and is skipped everywhere except a dedicated PgBouncer job.

This is the concrete reason a bare test count is a weak signal: quote the
commit, the environment and the toolchain alongside it, or quote the
`collected` total (626), which does not move with the environment.

---

## 6. What this does and does not prove

**Proven by this run:**

- `main` builds a valid wheel.
- `ruff check .` is clean.
- 626 tests are collected; 618 pass; 8 skip for missing external toolchains.
- The result is stable across repeated runs at the same commit.

**Not proven here (stated so this is not over-read):**

- The 8 skipped tests are not exercised in this environment. Android/Windows
  artifact correctness is asserted only where those toolchains exist.
- No live OpenHands, GitHub-provider, or LLM-provider call is part of this
  suite; those paths are exercised against the local fake servers in `tests/`.
- A green suite is a regression guard, not a product acceptance. See
  `docs/FINAL_ACCEPTANCE_REPORT.md` for what remains gated.

---

## 7. Maintenance rule

When the suite's size changes, update **§2** and **§5** of this file by
re-running `tools/verify_build_lint_test.sh`; do not paste a count from an
earlier report. Any other document that states a test count must point here
rather than carry its own number.
