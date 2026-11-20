# NEXUS Completion Ledger

The requirement (Part IX, deliverable 9) names `docs/COMPLETION_LEDGER.md`. The
ledger itself lives at the repository root, where it has been maintained across
the program: [`../COMPLETION_LEDGER.md`](../COMPLETION_LEDGER.md). This file is a
pointer so both paths resolve, and it records the program-level summary that the
root ledger's phases aggregate.

## What the ledger records

The root ledger is the **requirement-by-requirement** status: every item of the
completion program, marked VERIFIED, IMPLEMENTED — NOT LIVE VERIFIED,
BLOCKED — EXTERNAL DEPENDENCY, NOT IMPLEMENTED, or FAILED, with the evidence for
each. Its legend defines those five states and forbids the two substitutions the
program's rules forbid: an environmental skip is never a pass, and a claim is
never a proof.

Its phases:

1. Repository health
2. Core product
3. Interface
4. CLI
5. Resilience and performance
6. Deployment readiness
7. Security
8. This session's changes
9. Restart / Duplicate Run and the cancel/reclaim deadlock (integrated)

## This program's additions, in the ledger's terms

| Item | Status | Evidence |
|---|---|---|
| Canonical union (C1 shutdown fix, profiler schema guard) | VERIFIED | `test_connection_concurrency.py`, `cloud_store.REQUIRED_COLUMNS`, `docs/phase4-consolidation-audit.md` |
| PR #49 (Self-QA, Git credential, bound branch) | VERIFIED | `test_selfqa.py`, `test_git_credential.py`, `test_lab_http.py` |
| PR #47 (Restart/Duplicate Run, deadlock fix) | VERIFIED | `test_run_history.py`, `test_deadlock_retry.py` |
| Project isolation at every boundary | VERIFIED | `tests/test_project_isolation.py` |
| Rust core, first components | VERIFIED | `rust/`, `cargo test` (29) |
| Python/Rust parity | VERIFIED | `tests/test_rust_parity.py` (9) |
| Rust/Python benchmark | VERIFIED | `tools/benchmark_rust.py`, `benchmarks/rust_vs_python.json` |
| Rust SQL claim path | NOT IMPLEMENTED (next, gated) | `docs/RUST_MIGRATION.md` §8 |
| Two-live-account failover | BLOCKED — EXTERNAL DEPENDENCY | one OpenHands key |
| Live push on the live account | BLOCKED — EXTERNAL DEPENDENCY | no GitHub provider token on the account |
| Android APK / Windows `.exe` runtime | BLOCKED — EXTERNAL DEPENDENCY | no emulator / no Windows host |

The root ledger is the authority; this table is a summary of the same facts.
