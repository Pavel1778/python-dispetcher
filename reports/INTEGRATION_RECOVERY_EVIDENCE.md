# NEXUS — Integration Recovery Evidence (Phase 6)

**Produced:** 2026-11-17 (UTC) during the recovery of the PR #46/PR #47 integration.
**Branch:** `recovery/nexus-integration-revalidated` (in `.recovery/repo`)
**Base:** `6e5c2b410d531379c3599baa53492c83244551e5` · **Verdict:** see
`NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md`.

Every number in this document was produced by this recovery, in the
environment described in §1, by the commands in §3–§9. Historical numbers
from the lost 2026-11-16 sandbox appear **only** in the clearly-labeled
historical column below and are **not** re-attributed to this branch.

## 1. Test environment (built fresh; nothing reused)

| Item | Value |
|---|---|
| OS | Debian GNU/Linux 12 (bookworm), x86_64 |
| CPU / RAM / disk | 2 vCPU · 3.8 GiB · ~20 GiB free |
| Python | 3.11.2 (Debian) · venv `.recovery/venv` |
| Node | v22.22.3 |
| PostgreSQL | **16.2** (`pgserver` 0.1.4 manylinux wheel — bundled binaries; Debian apt has no package lists in this sandbox) |
| PostgreSQL server version string | `PostgreSQL 16.2 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 10.2.1 20210130 (Red Hat 10.2.1-11), 64-bit` |
| Cluster | **brand-new** `.recovery/pgdata` (unix socket; TCP disabled by the package) — the pre-reset PG 16.2 cluster was destroyed with its sandbox and was not used |
| Base DSN | `postgresql://postgres:@/nexus_recovery?host=/home/user/python-dispetcher/.recovery/pgdata` |
| Test isolation | `tests/_test_db.py`: every pytest process created its own `nexus_test_<token>` database (superuser `CREATE DATABASE`) and dropped it at session end — per-process fresh databases |
| Dependencies | psycopg 3.3.9 (+binary,+pool) · cryptography 45.0.7 · pytest 9.0.3 · ruff 0.13.1 · playwright (pip; **browser NOT downloadable** — see §7) |
| Docker | unavailable |
| Network egress | github.com, api.github.com, codeload.github.com, registry.npmjs.org, pypi.org, files.pythonhosted.org only |

**PostgreSQL 17 was NOT available** and is **not** validated in this branch.
The historical claim "full suite on PG 16.2" belongs to the lost sandbox;
PG 17 evidence exists only for other branches in CI (`test` service
`postgres:17` on GitHub). This branch has **not been pushed** (push is an
owner gate), so **no combined CI exists** for it.

## 2. Fresh database bootstrap (Phase 4)

Executed against the fresh cluster, database `nexus_migtest`
(created empty, then wired as the DSN):

- On the first connection (`PostgresStore()`), the full chain applied:
  **41 migrations** `0001_init.sql`…`0040_run_profiler.sql`; `schema_migrations`
  **rows=41, distinct=41**; **51 public tables**.
- `uq_nexus_runs_active_goal` partial unique index present:
  `(goal_id) WHERE state = ANY (ARRAY['running','paused','waiting_for_approval','blocked'])`.
- Re-opening a store on the migrated database is a **no-op** (idempotent);
  explicit `apply_migrations()` on an up-to-date database returns `[]`.
- Sequential application, idempotent re-application, and a fresh-database
  startup were all verified directly; the migration machinery had no pending
  or drift-detected files (`MIGRATION_CORRECTIONS` untouched).
- Concurrent schema bootstrap is exercised by `tests/test_schema_bootstrap.py`
  and friends **as part of the suite in §5** (per-process fresh databases);
  no separate concurrent-bootstrap harness was re-run beyond the suite, and
  `tests/test_schema_bootstrap.py` passed (1 environment-gated skip — see §6).
- **Database integrity**: schema_migrations consistent (41/41); spot checks of
  the index and table count above; no corruption signals (suite green).

## 3. Lint

```
$ /home/user/python-dispetcher/.recovery/venv/bin/ruff check .
All checks passed!
```
(exit 0 — command run at the branch state `ba5ee97`/`13217ba`-era working tree;
re-run is part of the reproduction recipe in §10.)

## 4. Studio render test

```
$ node tests/studio_render_test.js
studio render tests passed
```

## 5. Full Python test suite (the authoritative run)

```
$ cd /home/user/python-dispetcher/.recovery/repo
$ export SUPABASE_DB_URL='postgresql://postgres:@/nexus_recovery?host=/home/user/python-dispetcher/.recovery/pgdata'
$ unset OPENHANDS_API_KEY AUTH_USERNAME AUTH_PASSWORD_HASH AUTH_SESSION_SECRET GITHUB_TOKEN
$ .recovery/../venv/bin/python -m pytest tests/ -q
... (progress) ...
1972 passed, 49 skipped in 2587.54s (0:43:07)
```

- **Result: 1972 PASSED · 49 SKIPPED · 0 FAILED · EXIT=0**
- Log: `.recovery/logs/full_suite.log` (progress) and
  `.recovery/logs/full_suite.exit` (`FULL_SUITE_EXIT=0`).
- Progress-character count in the log independently reconciles:
  2021 progress markers = 1972 `.` + 49 `s`.
- Environment variables: `OPENHANDS_API_KEY` unset (the suite exercises code,
  not the shell), mirroring the historical practice; `GITHUB_TOKEN` popped by
  `conftest.py`.

Focused pre-flight (before the full run; same environment):
`tests/test_cancel_lock_order.py tests/test_deadlock_retry.py
 tests/test_cli_client.py tests/test_run_history.py tests/test_route_auth.py
 tests/test_cli_mcp.py tests/test_schema_bootstrap.py -q`
→ **60 passed, 1 skipped, 0 failed in 83.46s** (the skip: `TEST_POOLER_DSN`).

## 6. Skip attribution — all 49, exact (nothing is a pass)

| Module | Count | Reason (from source marker) |
|---|---|---|
| `tests/test_browser_e2e.py` | 37 | "Chromium could not be started" — playwright browser download blocked (see §7); per-test skip in the browser fixture |
| `tests/test_android_apk.py` | 4 | Android SDK build-tools are not installed |
| `tests/test_windows_exe.py` | 3 | mingw-w64 is not installed |
| `tests/test_backup_restore.py` | 2 | PostgreSQL client tools (`pg_dump`/`pg_restore`/`createdb`/`dropdb`) not on PATH — *environment limitation, not a defect; backup/restore NOT validated here* |
| `tests/test_comprehensive_suite.py` | 1 | Section F live OpenHands key required (absent) |
| `tests/test_acceptance_dev_task.py` | 1 | no local agent-server on `127.0.0.1:60000` |
| `tests/test_schema_bootstrap.py` | 1 | `TEST_POOLER_DSN` (transaction-mode pooler) not set |
| **Total** | **49** | |

Cross-checked by mapping the `s` clusters in the raw log to collection order
(5 at ~tests 1–72 = 1 + 4; 2 at ~tests 145–216; 37 at ~217; 1 at ~505 (comprehensive F);
1 at ~89 % (pooler); 3 at ~99 % (mingw)) — consistent with per-module
`--collect-only` counts and gate locations (`test_backup_restore` collects
exactly 2; `test_android_apk` 4; `test_windows_exe` 3; `test_browser_e2e` 37).

**Consequence:** browser E2E, Android packaging, Windows exe, the live
Section-F variant, and the pooler case were **NOT RUN** in this environment.
Backup/restore was **NOT RUN** (client tools absent — note: `pgserver` ships
the binaries inside its package but they are not on `PATH`).

## 7. Chromium / browser E2E — NOT RUN (blocked)

```
$ /home/user/python-dispetcher/.recovery/venv/bin/python -m playwright install chromium
Failed to install browsers
Error: Failed to download Chrome for Testing 159.0.8154.6 (playwright chromium v1255), caused by
Error: Download failure, code=1
```
The browser CDN is not reachable from this sandbox (egress allowlist). The 37
browser tests therefore skipped exactly as designed; no override, no bypass,
no fabricated pass. Historical 37/37 numbers belong to the PR #48/PR #49
sessions that had a Chromium binary and are labeled as such wherever cited.

## 8. Concurrency regression and the controlled inverse-order baseline

- `tests/test_cancel_lock_order.py` — **PASSED** in this branch (fix present).
- Disposable inverse-order control (NOT this branch): a throwaway local clone
  (`.recovery/baseline-inverse`) with the cancel fix **reverted** (executions
  closed before tasks, the old order) ran the same test and **FAILED** with
  `psycopg.errors.LockNotAvailable` on `nexus_task_executions` — exactly the
  recorded failure mode. The candidate branch was never touched; the control
  proves the test discriminates both orders on this tree.

## 9. Stress reproduction (30 rounds × the 24-test selection)

Selection (documented; aligned with the recorded integration description — the
original exact list is unrecoverable and is not claimed):

13 × `test_race_protection.py` + 2 × `test_deadlock_retry.py` +
1 × `test_cancel_lock_order.py` + 6 reclaim tests (`test_acceptance_recovery` ×2,
`test_comprehensive_suite` ×2, `test_lab_store` ×1, `test_dispatcher` ×1) +
2 cancel/settle race tests (`test_durable_runs` ×2) = **24 tests**
(`tests/` paths are enumerated in `.recovery/run_stress.sh`).

```
rep 01..30: rc=0 · "24 passed" each round
STRESS_ROUNDS_PASSED=30 STRESS_ROUNDS_FAILED=0
```

- **30/30 rounds green — 720 test executions, 0 failures** (~13–14 s per round).
- Logs: `.recovery/logs/stress_rep_01..30.log` · summary: `.recovery/logs/stress_summary.txt`.
- Each round used the per-process isolated database the suite harness creates.
- **Honesty note:** the lost report's phrase "24 concurrent workers per round /
  720 worker operations" cannot be reproduced as stated — its orchestration
  harness is gone, and this selection exercises the race conditions the tests
  encode (fencing, interleaving, one threaded lock-order probe), not 24 live
  worker processes per round. What is verified beyond doubt: the selection is
  green 30 times in a fresh-schema-per-process environment, including the
  deterministic cancel-vs-settle lock-order probe.
- The controlled pre-fix comparison for the deadlock inversion was PR #47's
  own recorded evidence (reproduced 1/6 before, 0/12 after) — **not re-verified
  here**; our inverse-order control (§8) covers the task↔executions inversion
  instead. No pre-fix stress baseline was re-run for the reclaim fix; reverting
  that hunk in a disposable clone is possible but was not needed to validate
  this reconstruction.

## 10. Reproduction recipe

```bash
git clone <remote> /path/to/work && cd /path/to/work     # or use .recovery/repo
git checkout recovery/nexus-integration-revalidated      # tip = <see final report>
python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt pgserver
./venv/bin/python .recovery/pg_holder.py &              # fresh cluster; needs an empty pgdata path
export SUPABASE_DB_URL='postgresql://postgres:@/nexus_recovery?host=/home/user/python-dispetcher/.recovery/pgdata'
unset OPENHANDS_API_KEY
venv/bin/ruff check .
venv/bin/python -m pytest tests/ -q                     # ~43 min on 2 vCPU
./run_stress.sh                                          # 30 × 24-test selection
```
(Paths inside this evidence document point at this sandbox's
`/home/user/python-dispetcher/.recovery/` layout; substitute your own paths.)

## 11. Not run / blocked / unavailable — summary

| Gate | Status |
|---|---|
| Full suite (fresh PG 16.2) | **PASSED** — 1972/49/0 |
| Stress 30×24 | **PASSED** — 0 failures |
| `ruff check .` | **PASSED** |
| Studio render test | **PASSED** |
| Migration fresh/sequential/idempotent/bootstrap | **PASSED** (§2, §5) |
| `tests/test_studio_ui.py` (Studio) | **PASSED** (inside the suite; 26 tests) |
| Chromium E2E (37) | **NOT RUN** — browser download blocked (§7) |
| Backup/restore drill | **NOT RUN** — client tools not on PATH (2 skips) |
| PostgreSQL 17 | **NOT AVAILABLE** — PG 16.2 locally; PG 17 is CI's service image; branch not pushed |
| Combined CI | **NOT RUN** — requires owner-authorized push |
| Android APK / Windows exe / Section F live / pooler | **NOT RUN** (skipped, reasons in §6) |
| Performance benchmarks | **NOT RE-RUN** — historical numbers only |
| Production deploy | **NOT PERFORMED** — owner gate |
