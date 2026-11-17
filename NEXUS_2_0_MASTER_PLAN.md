# NEXUS 2.0 Master Plan

The plan of record for the NEXUS 2.0 program: what was integrated, what was
built, what is verified, and what remains. It is written to be read against the
repository, not as a promise: every "done" names its evidence.

Date: 2026-11-16. Base: `main` @ `6629161`. Integration branch:
`nexus/2.0-integration`.

## 0. Summary

| Area | State |
|---|---|
| Repository integration (union + PRs #47, #49) | **done** |
| Correctness & security (deadlock, bound branch, isolation) | **done** |
| Product completion (Self-QA, restart/duplicate, git credential) | **done** |
| Rust foundation + first migrated components with parity | **done** |
| Rust core migration (the SQL claim path) | **next** (documented, gated) |
| Reliability & acceptance | **in progress** (full suite running) |
| Release | **not started** (needs owner authorisation) |

Nothing here is merged to `main`, tagged, or deployed. That is the owner's call.

## 1. Phase 0 — Preservation and integration

**Goal:** one canonical branch, no lost work, no duplicate implementations.

What was done:

- **Established the real state** by fetching, not by trusting reports. `main`
  was `6629161`; the open PRs were **#47** (`feat/run-restart-duplicate`) and
  **#49** (`release/acceptance`); #46 and #48 were already merged. The
  "canonical union" SHAs (`c09d888`, `c2d24b3`) did **not** exist on `origin` -
  they live in a preserved local repository, which was fetched read-only.
- **Integrated the reconciled union** (`c2d24b3`, 11 files, +361/−17). Its
  changes are disjoint from what `main` gained in PR #48 and merge cleanly. The
  C1 shutdown fix was confirmed still needed (main still asserted the
  whole-database `_backend_count(store) == 0`); the C1 isolation half was
  confirmed not applicable (main's conftest imports `tests._test_db` first). The
  union also repairs the schema guard: `REQUIRED_COLUMNS["nexus_runs"]` was
  missing the profiler columns migration `0040` adds.
- **Integrated PR #49**: the Self-QA engine, the Git credential for a sandbox,
  bound-branch enforcement. It merged cleanly; a merge artifact that dropped
  `NEXUS_ACCEPTANCE.md` (a main-side file) was restored.
- **Integrated PR #47**: Restart Run, Duplicate Run, and the cancel/reclaim
  deadlock fix. Three conflicts were resolved by hand: one import
  (`IdempotencyConflict`), and two documents (`COMPLETION_LEDGER.md`,
  `FINAL_RELEASE_REPORT.md`) where **both** sides' content was kept, not one.
- **One canonical branch**: `nexus/2.0-integration`, not several competing ones.

Evidence: `git log --oneline nexus/2.0-integration`; the merge commits name each
source and what was decided where they met.

## 2. Phase 1 — Correctness and security

| Item | State | Evidence |
|---|---|---|
| Cancel/reclaim deadlock | fixed | lock order: the run row first, everywhere; `test_run_history.py`, `test_deadlock_retry.py` |
| Bound branch is used when a task names none | fixed | `ProjectRepositoryService.primary_branch`; `test_lab_http.py` |
| No unintended push to `main` | fixed | the branch fix above; two regression tests |
| Structural deadlock corrections **and** bounded retries both preserved | done | PR #46's bounded retry kept; PR #47's lock-order fix kept |
| Project isolation at every boundary | proven | `tests/test_project_isolation.py` (8 cases, service + HTTP) |
| Secret never in a response/log | verified | redacted `__repr__`; token-free views; asserted |
| SSRF, redirect, client-IP, login ceilings | verified | `test_auth.py`, `test_dispatcher.py`, `test_cloud_integration.py` |

## 3. Phase 2 — Product completion

Already present and verified on `main` (per the completion ledger): the full
development lifecycle, planning and DAG execution, the multi-account pool with
failover, verification and the Evidence Trust Engine, notifications, the studio.
Added or completed by this program:

| Item | State | Evidence |
|---|---|---|
| Self-QA engine (isolated checkout, real exit codes, honest verdicts) | done | `orchestration/nexus_selfqa.py`, `tests/test_selfqa.py` |
| Git credential handed to a task's sandbox | done | `git_credential_for`, `tests/test_git_credential.py` |
| Restart Run | done | `LabStore.restart_run`, `tests/test_run_history.py` |
| Duplicate Run | done | `LabStore.duplicate_run`, `tests/test_run_history.py` |
| Bound-branch enforcement | done | `primary_branch`, `test_lab_http.py` |

## 4. Phase 3 — Rust foundation

**Goal:** a Rust workspace, the shared domain model, the error model, and a
parity harness - without a blind rewrite.

Done (see `docs/RUST_MIGRATION.md`):

- workspace of three crates (`nexus-domain`, `nexus-core`, `nexus-cli`);
- the error model (`thiserror`), the configuration read (`RetryPolicy::from_env`);
- the domain types (states, planned task, run/task snapshots);
- **29 Rust tests** pass.

## 5. Phase 4 — Rust core migration (first components)

Migrated, with parity proven by `tests/test_rust_parity.py` (9 cases):

| Component | Parity evidence |
|---|---|
| Run / workflow / control-plane state machines | exhaustive, every `(from, to)` pair |
| Plan validation + DAG check | 4 valid + 11 invalid plans, reason compared by category |
| Plan version hash | byte for byte |
| Retry arithmetic | 24 cases |
| The claim decision | real runs in a live PostgreSQL lab store |

**Not migrated, deliberately** (and said so in the doc): the intent router and
the task-kind derivation (language-sensitive), and the SQL claim transaction
(needs a Rust store client - the next step, gated the same way).

Benchmark, real numbers on this machine (`benchmarks/rust_vs_python.json`):
Python 4.88 s vs Rust 1.27 s over 20 000 cases including one process start
(~3.8×) - **on the migrated decisions only**, no product-level claim.

## 6. Phase 5 — UX and distribution

| Item | State |
|---|---|
| Responsive studio (desktop/laptop/tablet/phone) | present; browser E2E covers six widths |
| PWA | present (`orchestration/pwa/`) |
| Android APK | build script present; **not runtime-verified** (no emulator here) |
| Windows `.exe` | build script present; **not runtime-verified** (no Windows host) |
| Debian package | build script present; verified by `dpkg-deb` inspection |

The requirement says not to claim a package is available unless it was built and
tested. This plan does not: it says which are verified and which are not.

## 7. Phase 6 — Reliability and acceptance

- **Full suite** on `nexus/2.0-integration`: see `NEXUS_2_0_FINAL_ACCEPTANCE_REPORT.md`
  for the exact count.
- **Rust tests**: 29 pass.
- **Parity**: 9 pass.
- **Isolation**: 8 pass.
- The acceptance scenarios (A-G) are recorded in the final report, with what is
  verified, what is BLOCKED (no second OpenHands key), and what is UNVERIFIED.

## 8. Phase 7 — Release

**Not started, and not started on purpose.** The requirement is explicit: *"Do
not merge into main, create a public release tag, or deploy to production
without explicit owner authorization."* The release artifacts and procedure are
prepared (`docs/RELEASE_GUIDE.md`); the act is the owner's.

## 9. Definition of Done — status

| Condition | State |
|---|---|
| Canonical repository state is stable | yes (`nexus/2.0-integration`) |
| No critical integration conflicts remain | yes |
| The full development lifecycle works | verified (tests; live acceptance per the ledger) |
| OpenHands execution verified live | verified on the account available (live probe 17/17) |
| Git operations respect project and branch bindings | verified |
| Independent Self-QA works | verified |
| Project isolation is proven | verified (`test_project_isolation.py`) |
| Restart and recovery work | verified (durable PostgreSQL; worker process tests) |
| Concurrency is tested | verified (lease/fencing, claim, deadlock stress) |
| Security gates pass | verified (see `SECURITY_MODEL.md`) |
| Rust owns the migrated core components | **partial by design** - pure core migrated, SQL path next |
| Python/Rust parity is demonstrated | verified |
| The CLI works | verified (Python CLI; `nexus-rs` for the pure part) |
| Web and mobile experiences are usable | web verified; mobile build not runtime-verified |
| Required distribution artifacts are built and tested | Debian verified; Android/Windows build only |
| Documentation matches the product | this set, written against the code |
| Release acceptance evidence is available | in the final acceptance report |

## 10. External blockers

Listed separately, as the requirement demands - never used to hide unfinished
internal work:

- **A second live OpenHands key.** Only one is available, so a *two-live-account*
  failover is BLOCKED. The failover logic is proven against real PostgreSQL with
  honest transport fakes; a live switch needs a second account.
- **A GitHub provider token on the live OpenHands account.** The account has an
  LLM key but no GitHub token, so a live task that must clone and push ends
  `completed_unverified`. That is correct behaviour, not a defect.
- **An Android emulator / a Windows host.** The APK and `.exe` build scripts
  produce artifacts but cannot be *run* here.
- **Owner authorisation for the release.** Merging, tagging and deploying are
  the owner's decisions.
