# PARITY MATRIX — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: 282 real Python↔Rust comparisons, 0 disagreements.** The count is
produced by running both implementations, not by counting test functions
(`tools/parity_matrix.py`). Areas with no Rust counterpart are named, not counted.

---

## 1. How the count is earned

`python -m tools.parity_matrix --json parity.json`:

1. builds `nexus-rs` (`cargo build`);
2. feeds the **same inputs** to the Rust binary (`nexus-rs parity`) and the
   Python authority on this side;
3. records one row per **distinct** comparison (a repeated `(from,to)` pair is
   skipped, so duplicated parameter combinations are not counted);
4. exits non-zero if any row disagrees, so it is a gate.

The gate is pinned by `tests/test_parity_matrix_tool.py` (≥ 100 real
comparisons, zero disagreements, and the NOT-COMPARED areas must be named).

## 2. Measured result

| Area | Compared | Agree |
| --- | ---: | ---: |
| control-plane transitions (`control_plane._ALLOWED`) | 100 | 100 |
| workflow transitions (`nexus_workflow.can_transition`) | 90 | 90 |
| run transitions (`_RUN_ENTERED_FROM`) | 49 | 49 |
| retry arithmetic (`nexus_retry.cycle_wait`/`refused_wait`) | 24 | 24 |
| plan validation (`nexus_planner.validate_tasks`) | 15 | 15 |
| plan hash (`nexus_workflow.plan_hash_of`, byte-for-byte) | 4 | 4 |
| **Total** | **282** | **282** |

Plus the scheduler claim decision over real runs and the SQL claim/reclaim,
which are compared by `tests/test_rust_parity.py` (9 tests) and
`tests/test_rust_claim.py` (19 tests against a real PostgreSQL via the SQLx
`claim_probe`).

## 3. Areas with no Rust counterpart (named, not counted)

| Area | Status |
| --- | --- |
| SQL claim (`FOR UPDATE SKIP LOCKED`) | NOT COMPARABLE (Rust `claim_probe` tests it directly, see §2) |
| lease reclaim (`reclaim_expired`) | NOT COMPARABLE (same) |
| durable dispatch journal | NOT IMPLEMENTED in Rust (Python only) |
| evidence signing | NOT IMPLEMENTED in Rust |
| HTTP API surface | NOT IMPLEMENTED in Rust |
| OpenHands transport | NOT IMPLEMENTED in Rust |
| intent router | NOT COMPARABLE (language-sensitive regex; deliberately not migrated) |
| task-kind derivation | NOT COMPARABLE (language-sensitive regex; deliberately not migrated) |

## 4. Why some areas are NOT COMPARABLE rather than NOT IMPLEMENTED

The intent router and task-kind derivation are language-sensitive regexes with a
history of subtle production bugs. The Rust validator carries the `kind` it is
given; it does not derive one. Migrating them would be a risk with no parity
oracle, so they stay Python-only and are labelled honestly
(`docs/RUST_MIGRATION.md` §4).

## 5. Reproduce

```bash
cd rust && cargo build --release && cd ..
NEXUS_RUST_BIN="$(pwd)/rust/target/release/nexus-rs" python -m tools.parity_matrix
# -> {"compared": 282, "agree": 282, "disagree": 0, ...}
```

## 6. The brief's "100 cases" target

The brief asked for "at least 100 meaningful verified cases where corresponding
Rust implementations exist". This delivers **282** distinct comparisons over the
areas that *do* have a Rust counterpart, with every non-comparable area named.
No case is counted twice; a duplicated `(from,to)` pair contributes one row.
