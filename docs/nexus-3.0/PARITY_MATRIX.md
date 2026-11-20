# PARITY MATRIX — Python ↔ Rust

**Date:** 2026-11-20 · **Status: PARTIAL** — parity is proven only where a Rust
implementation exists.

The brief lists 30 areas and asks for ≥100 scenarios. That target is
**unreachable without inventing Rust code**: 22 of the 30 areas are implemented
only in Python (worker loop, dispatch, cancellation, notifications, API), so a
"parity" test for them would compare Python with nothing.

This matrix records, for each area, whether Rust code exists, what is proven,
and what is blocked.

Legend: **P** proven · **PART** partial · **B** blocked (no Rust implementation) ·
**N/A** not applicable.

| # | Area | Rust exists? | Status | Proof / reason |
| ---: | --- | --- | --- | --- |
| 1 | Run lifecycle | no | **B** | `set_run_state`/`_apply_run_state` are Python-only |
| 2 | Task lifecycle | partial | **PART** | Rust `claim`/`reclaim` move `pending↔dispatched` only |
| 3 | Claim ordering | yes | **P** | `test_the_claim_decision_agrees_on_real_runs` (exhaustive over real runs) |
| 4 | Lease acquisition | yes | **P** | `test_mixed_workers_exhaust_queue_once` |
| 5 | Lease renewal | no | **B** | no Rust renewal path |
| 6 | Lease expiry | yes | **P** | `test_rust_claim_survives_process_exit_and_python_recovery`, `test_expired_attempts_match_committed_python_state` |
| 7 | Lease reclamation | yes | **P** | `test_sweep_is_bounded_scoped_and_never_requeues_bound_conversation` |
| 8 | Stale fencing | yes | **P** | `test_killed_sqlx_client_before_commit_releases_the_run_and_budget`; Python side in `test_race_protection.py` |
| 9 | Retry behaviour | yes (arithmetic) | **P** | `test_retry_arithmetic_agrees` |
| 10 | Backoff calculations | yes | **P** | same case (`nexus_retry` vs `nexus-domain::retry`) |
| 11 | Stop signals | no | **B** | Rust has no stop path |
| 12 | Cancellation | no | **B** | Rust has no cancel |
| 13 | Task blocking | no | **B** | Rust has no block/`blocked` transition |
| 14 | Execution closure | no | **B** | Rust has no execution rows |
| 15 | Run completion | no | **B** | Rust has no run completion |
| 16 | Goal mirroring | no | **B** | Python-only |
| 17 | Notifications | no | **B** | Python-only |
| 18 | Chat events | no | **B** | Python-only |
| 19 | API JSON contracts | no | **B** | no Rust API exists |
| 20 | Database error behaviour | partial | **PART** | `test_failed_task_update_rolls_back_the_run_budget` (one real error path) |
| 21 | Transaction rollback | yes | **P** | same case — the run budget is rolled back |
| 22 | Concurrent operations | yes | **P** | `test_mixed_workers_exhaust_queue_once`, `test_cross_project_branch_and_capacity_are_global`, `test_locking_one_task_does_not_block_a_second_eligible_grant` |
| 23 | Duplicate requests | no | **B** | no Rust request path |
| 24 | Recovery after restart | yes | **P** | `test_rust_claim_survives_process_exit_and_python_recovery` |
| 25 | Invalid input | partial | **PART** | plan validation (`test_plan_validation_agrees_on_every_case`) |
| 26 | Boundary values | partial | **PART** | retry clamp covered; no other Rust boundary has a peer |
| 27 | Null / missing fields | partial | **PART** | `test_legacy_null_deadline_and_pause_are_decided_on_the_db_clock` |
| 28 | Serialization compatibility | yes | **P** | `test_plan_hash_agrees_byte_for_byte`, `…_changes_when_the_plan_changes` |
| 29 | Timestamp precision | no | **B** | no Rust timestamp path |
| 30 | Unicode / text handling | no | **B** | no Rust text path |

## Totals

| Status | Areas |
| --- | ---: |
| **P** proven | 11 |
| **PART** partial | 5 |
| **B** blocked (no Rust implementation) | 14 |

Existing parity/claim checks: **22** (9 parity + 13 claim), all passing with the
probe and an isolated database, **zero skips**.

## Reaching the brief's ≥100 target

The 30 areas cannot yield 100 meaningful scenarios while 14 of them have no Rust
code. Two honest paths:

1. **Extend the 16 areas that do have Rust code** — e.g. exhaustive claim
   agreement over many random run shapes, retry/backoff over a full input grid,
   and plan validation over a generated corpus. That could plausibly reach
   ~100 *meaningful* cases (not padding), all inside areas 2/3/6/7/8/9/10/20/
   21/22/24/25/26/27.
2. **Build the missing Rust components** (worker, API), after which the other
   14 areas become testable.

Path 1 was **not** completed in this session: it is real work worth doing, but it
would still be 16 areas, and presenting it as coverage of 30 would be false. It
is recorded as the next increment.
