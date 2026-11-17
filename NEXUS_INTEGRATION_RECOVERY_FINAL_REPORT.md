# NEXUS — Integration Recovery: Final Report

**Date:** 2026-11-17 (UTC)
**Branch:** `recovery/nexus-integration-revalidated`
**Tip:** the commit carrying this file — resolve mechanically with
`git rev-parse recovery/nexus-integration-revalidated` (commit sequence:
`6e5c2b4` base → `e48861f` → `e0ee175` → `ba5ee97` → `13217ba` → `87da686`
→ `93bc28f` → this document's commit). The integration's code-complete
state is `ba5ee97`.
**Base:** `6e5c2b410d531379c3599baa53492c83244551e5` (`6e5c2b4`, merge of PR #49)
**Scope:** recovery and revalidation only — **no release, no push, no merge, no deploy**
**Verdict (§9):** **B — VALIDATED RECONSTRUCTION. NOT A. Owner authorization required for the next stage.**

This document supersedes nothing. It is the honest completion of the recovery
of the 2026-11-16 PR #46 → PR #47 integration after that work was destroyed
by a sandbox reset. Read it together with:

- `PRE_MERGE_FORENSIC_AUDIT.md` (the owner's audit; verdict C — preserved in
  the original working tree, authoritative for the state it examined),
- `reports/INTEGRATION_RECOVERY_EVIDENCE.md` (commands, environment, logs),
- `reports/INTEGRATION_RECOVERY_BASELINE.md` (inverse-order baseline note),
- `reports/INTEGRATION_CODE_REVIEW.md` (review of the reconstructed delta),
- `PR46_PR47_FINAL_INTEGRATION_REPORT.md` (the original delivery report —
  **preserved verbatim in this branch behind a SUPERSEDED banner; it is not
  evidence for this branch**).

---

## 1. Verdict — stated plainly

**B — VALIDATED RECONSTRUCTION.**

- **Preservation HOLDS.** The pre-reset state was snapshotted and verified
  (Phase 0, 17/17 verification points, `/home/user/recovery-preservation-2026-11-17/`);
  the original working tree at `/home/user/python-dispetcher` was never
  modified by the recovery and still carries exactly the integration delta it
  carried at audit time (16 entries) plus the audit's own untracked report.
- **Completeness HOLDS for everything runnable in this environment** and is
  explicitly NOT CLAIMED for what is not runnable here (Chromium E2E,
  backup/restore drill, PostgreSQL 17, combined CI — §7).
- **Provenance does NOT hold for the original artifacts.** The five lost
  commits do not exist anywhere (local object store and GitHub both lack
  them; `git cat-file` → fatal, GitHub API → HTTP 422). What exists now is a
  **byte-faithful reconstruction** of that integration content, re-created on
  the current `main`, re-verified end to end. Nothing in this report
  re-attributes the original SHAs, dates, or evidence logs to this branch.

A requires provenance + the rest. Provenance of the *original* artifacts is
unrecoverable — so **A is not available, and this report does not claim it.**
This is not C anymore either: the audit's structural defect ("the repository
does not contain the integration at all; the evidence environment is gone")
is resolved — the integration now exists as reviewable, resolvable commits
whose content has been re-verified in a fresh environment. It is not D: no
regression, security defect, or weakened protection was found (§5, §10).

**Why not A, in one line:** everything verifiable was re-verified green, but
the audit's own precondition for A — "attributable to resolvable commits …
ready for owner-approved push" — cannot be met by new commits standing in
for destroyed ones; that judgment is the owner's to make at the acceptance
gate, and this report stops there.

## 2. What was lost, what survived

Lost (sandbox re-created 2026-11-17T09:02:48Z; `git fsck` clean — the objects
are absent, not merely hard to find):

- Commits `ec0d553` (merge `f6e77c9`), `2fbdcad` (route auth), `e2c7575`
  (cancel lock order + test), `ce21e47` (code complete), `baa3251` (report
  touch-up). All five: HTTP 422 on GitHub; absent locally.
- The pre-reset PG 16.2 cluster, the venv, every log cited by the integration
  report (`/home/user/pytest_integration_final.log`, `/tmp/stress_rep_*.log`),
  and the evidence environment generally.
- The integration branch ref never existed on the remote (fetch refspec
  targeted it; `git fetch` produced nothing).

Survived (used, verbatim, as inputs):

- The original working tree's uncommitted delta at `/home/user/python-dispetcher`
  (13 modified + untracked `DEPLOYMENT_CHECKLIST.md`,
  `PR46_PR47_FINAL_INTEGRATION_REPORT.md`, `tests/test_cancel_lock_order.py`).
- `origin/main` history (through PR #46) and, on GitHub, PR #47's branch
  `f6e77c9682eb7df296a68f6720e85d89de01f5c7` (CI pass 29m8s, run 43923875960).
- The owner's audit `PRE_MERGE_FORENSIC_AUDIT.md` (verdict C; skipped-test
  reconciliation; recovery path).

## 3. The reconstruction — and its exact commit map

Re-created on `6e5c2b4` (not on `6629161`): PR #49 merged to `main` at
2026-11-17T09:24:41Z, carrying credential-handling fixes, a bound-branch
verifier fix, deterministic Self-QA and a user guide. Re-creating on the
superseded base would have re-introduced known-fixed defects. The audit's
recovery path (b) — "commit the 16-file delta … based on latest
origin/main" — points at the same base.

| Lost commit | Re-created | Contents |
|---|---|---|
| `ec0d553` (merge of `f6e77c9`) | `e48861f846b204424bb5fffe28aed2631bf64aa7` — *Merge PR #47: restart and duplicate runs* | same merge of `f6e77c9682eb7df296a68f6720e85d89de01f5c7`, plus docs-union reconciliation (`FINAL_RELEASE_REPORT.md`, `COMPLETION_LEDGER.md`) onto the new base |
| `2fbdcad` (route auth) | `e0ee175cedfbc8cdad7f60ada4491533f87c478b` — *test(route-auth): restart/duplicate POST routes in PROTECTED_POST* | byte-identical content; test gate `tests/test_route_auth.py` (+1) |
| `e2c7575` (cancel lock order + test) | `ba5ee974a9f30ac4137f530cfab9c463f0312324` — *fix(cancel): lock run tasks before execution rows in run settlement* | byte-identical content; `orchestration/` + `tests/test_cancel_lock_order.py` |
| `ce21e47` (code complete) | `ba5ee97` is the code-complete state; documented in `reports/INTEGRATION_CODE_REVIEW.md` | no separate empty commit re-created |
| `baa3251` (report touch-up) | `13217baf1c5d7954abc5af42488aa64517788fe7` — *docs: preserve the pre-recovery integration report, marked superseded* | the report preserved verbatim + banner; identity/evidence sections re-pointed via `reports/INTEGRATION_RECOVERY_EVIDENCE.md` and this document |
| *(evidence re-generated)* | `87da686d409202e12868d0c86548548bc6179992` — *docs: add recovery evidence* and `93bc28f77f78b41bf502161a8f4e78ca0ae698cf` — *docs: add recovery baseline and code review; refine skip attribution* | new evidence, new reports |

Byte-level comparison of the surviving 16-file delta against this branch:

- **11 files byte-identical:** `DEPLOYMENT_CHECKLIST.md`, `docs/CLI_REFERENCE.md`,
  `docs/CRUD-MATRIX.md`, `orchestration/mcp_server.py`, `orchestration/nexus_lab_store.py`,
  `orchestration/nexus_policy.py`, `orchestration/studio/app.js`,
  `tests/test_cli_mcp.py`, `tests/test_route_auth.py`, `tests/test_run_history.py`,
  `tests/test_cancel_lock_order.py`.
- **4 files differ — all because PR #49 changed them after the integration was
  written, resolved in favor of the PR #49 superset, integration content
  retained on top:** `orchestration/cli.py` (PR #49's `qa run` Self-QA),
  `orchestration/web_server.py` (bound-branch verifier fallback), plus
  `COMPLETION_LEDGER.md` and `FINAL_RELEASE_REPORT.md` (docs union onto the
  new base; the reconstructed integration recorded in §18 of the report).
- **1 file differs by design:** `PR46_PR47_FINAL_INTEGRATION_REPORT.md`
  (verbatim body + SUPERSEDED banner; SHA-256 of the delivered body recorded
  there).
- No unexplained divergence. No file was silently dropped.

Provenance of the reconstruction inputs is exactly: (1) PR #47 branch
`f6e77c9` (fetched from GitHub), (2) `origin/main` `6e5c2b4`, (3) the
surviving uncommitted delta, (4) the owner's audit. Nothing else.

## 4. Environment and commands (summary)

Fresh environment built for this verification; nothing reused from the lost
sandbox. Full detail: `reports/INTEGRATION_RECOVERY_EVIDENCE.md`.

Debian 12 · 2 vCPU · 3.8 GiB · Python 3.11.2 · Node v22.22.3 · PostgreSQL
**16.2** (`pgserver` 0.1.4; Debian apt has no package lists and GitHub/PyPI
egress only) · **brand-new** cluster `.recovery/pgdata` (unix socket) · DSN
`postgresql://postgres:@/nexus_recovery?host=/home/user/python-dispetcher/.recovery/pgdata`
· per-process isolated test databases (`tests/_test_db.py`) dropped at session
end. **PostgreSQL 17 was not available and is not validated on this branch.**

## 5. Results — what was re-verified, with fresh commands

| Check | Command (abbreviated) | Result |
|---|---|---|
| Lint | `ruff check .` | **All checks passed** (exit 0) |
| Studio render | `node tests/studio_render_test.js` | **passed** |
| Migrations, fresh DB | first `PostgresStore()` connect on empty `nexus_migtest` | **41/41 applied**, 51 public tables, `schema_migrations` 41 rows distinct, `uq_nexus_runs_active_goal` partial index present; re-open idempotent |
| Focused suites | `pytest tests/test_cancel_lock_order.py tests/test_deadlock_retry.py tests/test_cli_client.py tests/test_run_history.py tests/test_route_auth.py tests/test_cli_mcp.py tests/test_schema_bootstrap.py -q` | **60 passed, 1 skipped** (`TEST_POOLER_DSN`) |
| **Full suite** | `pytest tests/ -q` (exit code captured) | **1972 passed, 49 skipped, 0 failed — 2587.54s (0:43:07) — `FULL_SUITE_EXIT=0`** (`.recovery/logs/full_suite.log`) |
| Inverse-order control | `tests/test_cancel_lock_order.py` on a disposable clone with the lock order reverted | **FAILED with `psycopg.errors.LockNotAvailable` on `nexus_task_executions`** — the test discriminates; the branch's fix is load-bearing |
| **Stress** | 30 rounds × 24-test cancel/reclaim/race selection (`.recovery/run_stress.sh`) | **30/30 rounds green — 720 test executions, 0 failures** (`.recovery/logs/stress_summary.txt`: `STRESS_ROUNDS_PASSED=30 STRESS_ROUNDS_FAILED=0`) |

Note: the full-suite run was performed before the final three documentation
commits (`13217ba`, `87da686`, `93bc28f`); those commits touch only Markdown.
Re-running the suite for Markdown-only changes is not required and was not
done.

## 6. Skip attribution — all 49, runtime-verified

37 + 4 + 3 + 2 + 1 + 1 + 1 = **49**, each confirmed by re-running the module
standalone after the full suite (commands in `reports/INTEGRATION_RECOVERY_EVIDENCE.md` §6):

| Cause | Module | Count |
|---|---|---|
| Chromium unusable (browser CDN unreachable) | `tests/test_browser_e2e.py` | 37 |
| Android SDK build-tools absent | `tests/test_android_apk.py` | 4 |
| mingw-w64 absent | `tests/test_windows_exe.py` | 3 |
| PostgreSQL client tools (`pg_dump`/`pg_restore`/`createdb`/`dropdb`) not on `PATH` | `tests/test_backup_restore.py` | 2 |
| Section-F live OpenHands variant | `tests/test_comprehensive_suite.py` | 1 |
| local agent-server on `127.0.0.1:60000` absent | `tests/test_acceptance_dev_task.py` | 1 |
| `TEST_POOLER_DSN` unset | `tests/test_schema_bootstrap.py` | 1 |

No skip was counted as a pass anywhere. The audit's static Appendix A
assigned all four "other" skips to `test_acceptance_dev_task.py`; the
runtime attribution refines that (1 there + 2 backup/restore + 1 Section-F).
Both decompositions total 49.

## 7. NOT RUN / BLOCKED — explicit non-claims

- **Chromium browser E2E (37 tests) — NOT RUN.** `playwright install chromium`
  fails: browser CDN not reachable from this sandbox (code=1). Per-test skips
  in the fixture, exactly as designed. The 37/37 figures in circulation belong
  to the PR #48/PR #49 sessions which had a Chromium binary.
- **Backup/restore drill — NOT RUN.** Client tools are not on `PATH` in this
  environment. Backup/restore is therefore unvalidated here (2 skips).
- **PostgreSQL 17 — NOT AVAILABLE locally.** Debian 12; `pgserver` ships PG
  16.2; apt package lists are absent. PG 17 evidence exists only in CI
  (`postgres:17` service) on branches that were pushed. **This branch is not
  pushed, so it has no combined CI.**
- **Combined CI — NOT RUN.** Requires an owner-authorized push. No combined
  green CI exists for this branch anywhere.
- **Section-F live variant, Android APK, Windows exe, pooler case — NOT RUN**
  (skipped with reasons above).
- **Concurrent bootstrap under load beyond the suite's per-process DBs** — not
  re-harnessed separately; the suite itself exercises concurrent schema paths.
- **Performance benchmarks — NOT RE-RUN.** The DB p99 / claim-throughput
  numbers in the ledger are historical (fix/approval-notification and
  release/acceptance sessions, PG 17) and are labeled as such wherever cited.
- **Production deploy — NOT PERFORMED** (owner gate).
- **Rust rewrite / query-performance work (audit Appendix B) — NOT PERFORMED**
  and explicitly out of scope. The audit itself states the required work is
  measurement-first (`EXPLAIN (ANALYZE, BUFFERS)`, index audit,
  `pg_stat_statements`), and that nothing supports a Rust rewrite now.

## 8. Deliverables — inventory and locations

On branch `recovery/nexus-integration-revalidated` (resolve the tip with
`git rev-parse`; the integration code-complete state is `ba5ee97`), in
`/home/user/python-dispetcher/.recovery/repo`:

| Path | Contents |
|---|---|
| `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md` | this document |
| `reports/INTEGRATION_RECOVERY_EVIDENCE.md` | environment, commands, logs, skip attribution, reproduction recipe |
| `reports/INTEGRATION_RECOVERY_BASELINE.md` | the inverse-order control and why |
| `reports/INTEGRATION_CODE_REVIEW.md` | review of the reconstructed delta |
| `PR46_PR47_FINAL_INTEGRATION_REPORT.md` | the original delivery report, preserved verbatim + SUPERSEDED banner |
| `tests/test_cancel_lock_order.py` | the deterministic lock-order probe |

Outside the branch (local only; nothing pushed):

- `.recovery/recovery-branch.bundle` — complete history bundle of
  `origin/main` + this branch (3.9 MB; verified with `git bundle verify`).
- `.recovery/logs/full_suite.log`, `.recovery/logs/full_suite.exit`,
  `.recovery/logs/stress_rep_01..30.log`, `.recovery/logs/stress_summary.txt`,
  `.recovery/logs/full_suite_rs.*` (unused dry-run artifacts).
- `/home/user/recovery-preservation-2026-11-17/` — the Phase 0 snapshot of
  the pre-reset state (verified 17/17). Untouched.
- The original working tree `/home/user/python-dispetcher` — untouched by the
  recovery; carries the user's 16-entry delta + `PRE_MERGE_FORENSIC_AUDIT.md`
  + (added by this delivery) the four report files listed above, mirrored as
  **new untracked files only** under `reports/`.

## 9. Acceptance criteria — point by point

| Criterion | Finding |
|---|---|
| **Preservation** | **HOLDS.** Phase 0 snapshot verified (17/17). Original working tree byte-preserved; user's uncommitted delta intact; nothing deleted, renamed, reset, stashed or cleaned. The `.recovery/` scratch lives inside the workspace root and on the same filesystem. |
| **Provenance (original artifacts)** | **DOES NOT HOLD.** Lost SHAs unrecoverable (local fsck clean / GitHub 422). Content provenance of the reconstruction is fully documented (PR #47 `f6e77c9` + `origin/main` `6e5c2b4` + surviving delta + audit). The re-created commits are **new** commits; the old identity is not restored and is explicitly not claimed. |
| **Completeness (runnable scope)** | **HOLDS.** Fresh-environment full suite 1972/49/0 (exit 0); stress 30/30 (720 executions); ruff clean; Studio render passed; 41/41 migrations on a fresh database; focused suites green; inverse-order control discriminates. |
| **Completeness (excluded scope)** | **NOT ESTABLISHED.** Chromium E2E, backup/restore, PG 17, combined CI — NOT RUN (§7). Explicitly not claimed. |
| **Stop at the acceptance gate** | **HONORED.** No push, no merge, no release, no tag, no deploy, no PR changes (PR #47 remains open and CONFLICTING; PR #49's merge is historical fact, not this branch's doing). |

**Verdict: B — VALIDATED RECONSTRUCTION.** Not A (provenance of the lost
original artifacts cannot be re-established; that alone disqualifies A under
the audit's rubric). Not C (the audit's structural block — "the repository
does not contain the integration at all; evidence environment gone" — is
resolved, by fresh commits and fresh evidence). Not D (no regression,
security or weakening defect found; the one latent concurrency defect found
in review was fixed and proven by a discriminating control).

## 10. What this changes relative to the audit

- **Resolved:** integration exists as commits (`e48861f` → … → `93bc28f`);
  verification environment re-created from scratch; evidence regenerated
  (suite, stress, lint, migrations); the report's identity/evidence sections
  re-pointed to the re-created SHAs; lock-order defect fixed and proven;
  skipped-test reconciliation re-derived at runtime (49 = 37+4+3+2+1+1+1).
- **Not changed / not changeable here:** the lost artifacts' identity; the
  absence of Chromium E2E and backup/restore evidence; PG 17 and combined-CI
  validation (require owner push); the owner's merge sequence and any release.
- **Audit §12 ("no repository modification")** — that was true of the audit.
  This recovery modified nothing in the original tree either; all changes
  live on the new branch (or in scratch).

## 11. Owner decision points — in order

1. **Accept the reconstruction as the integration of record** (or not). If
   not, the fallback is to re-integrate PR #47's branch afresh onto `main`;
   nothing here forecloses that.
2. **Authorize (or refuse) the push** of
   `recovery/nexus-integration-revalidated` (or an equivalent branch cut from
   it). Nothing in this report authorizes it.
3. After a push: combined CI (PG 17 + Chromium) is the real gate; then the
   merge sequence (merge commit; PR #47 closes-as-merged via the merge;
   rollback via `git revert -m 1`).
4. Re-run the backup/restore drill in an environment with the client tools.
5. Run the Chromium E2E suite where a browser can be installed.
6. Only then grade A — and cite the CI run ids, not this document's claims.

## 12. Honesty statements

- The original commits were **not** recovered. Any statement implying they
  were is false; this report does not make it.
- Historical "1951 passed" / "30/30 stress" numbers are the lost session's and
  are labeled historical wherever they appear; the figures of record for this
  branch are **1972/49/0** and **30×24 = 720, 0 failures**.
- The lost report's phrase "24 concurrent workers / 720 worker operations per
  round" cannot be reproduced as stated; what was executed is 30 rounds of the
  24-test selection (720 executions) plus the deterministic lock-order probe.
- Chromium, PG 17, backup/restore, live Section-F: not run, not passed.
- PR #47 is still open and conflicting; closing or merging it is the owner's
  call after the gate sequence above.
- No code was pushed, merged, tagged, released or deployed in producing this
  report.
