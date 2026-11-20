# ARCHITECTURE RECONCILIATION — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission` (local, from `main` `20eb8bf`)
**Verdict: ONE canonical Rust storage authority (`nexus-storage`). There was
never a competing implementation to reconcile; the brief's "two competing Rust
implementations" does not exist in this repository.**

---

## 1. What the brief claimed, and what is actually here

The mission brief describes two Rust implementations:

| Claimed in the brief | Reality (checked against every ref) |
| --- | --- |
| Candidate `nexus-store`, `nexus-worker`, `nexus-api` | **Do not exist** — 0 files on all 11 remote branches, all tags, all history |
| Main `nexus-storage` | **Exists** (`rust/crates/nexus-storage`, 144 lines + `reclaim_expired.sql`) |
| Candidate branch `nexus/3.0-foundation` | **Does not exist** (HTTP 404 on GitHub) |
| Phase-2 commit `9c11f52` | **Does not exist** (422, not a valid object) |

Mechanical proof (run in this session):

```bash
git log --all --oneline --diff-filter=A -- 'rust/crates/nexus-store/*' \
    'rust/crates/nexus-worker/*' 'rust/crates/nexus-api/*'
# -> empty: those files were never added on any branch
git ls-tree -r --name-only <every remote branch> -- rust/crates/
# -> nexus-cli, nexus-core, nexus-domain, nexus-storage. Only these four.
```

The strings `nexus-store` / `nexus-worker` / `nexus-api` appear **only** inside
prose documents (`docs/nexus-3.0/*` on the abandoned `nexus/3.0-audit` branch),
never as code. A prior audit (`origin/nexus/3.0-audit`, commit `5688854b`)
reached the same conclusion and is corroborated here.

**Consequence.** There is nothing to "reconcile away". Deleting a competing
implementation would be deleting something that does not exist. The honest
action is to document the single real authority and prove it is correct — which
this document and `PARITY_MATRIX.md` do.

---

## 2. The four real crates

| Crate | Purpose | Lines | I/O |
| --- | --- | ---: | --- |
| `nexus-domain` | Pure domain: run/task/workflow state machines, DAG validation, plan hash, retry arithmetic | ~1 025 | none |
| `nexus-core` | The scheduler's **claim decision** (pure) | ~387 | none |
| `nexus-storage` | Two SQL operations: `claim_task`, `reclaim_expired` (SQLx) | ~239 | PostgreSQL |
| `nexus-cli` | `nexus-rs` binary; the parity oracle (`parity`, `claimable`) | 354 | none (stdin/stdout JSON) |

The workspace manifest (`rust/Cargo.toml`) names exactly these four members.
There is **no** Rust worker loop, **no** Rust HTTP API, **no** Rust run-lifecycle
code, **no** Rust stop/cancel code. The production worker, HTTP layer, evidence
engine and OpenHands transport are Python.

---

## 3. Canonical selection

**`nexus-storage` is the canonical Rust storage crate.** It is the only one, so
selection is trivial, but it is still justified by quality:

| Dimension | `nexus-storage` behaviour |
| --- | --- |
| Eligibility predicates | **Shared with Python** via `include_str!("…/orchestration/sql/*.sql")` — one source of truth for "what is claimable", not a second copy |
| Claim | `SELECT … FOR UPDATE SKIP LOCKED` inside one transaction; lease token bump |
| Reclaim | `reclaim_expired` bumps the fencing token and clears the owner, in one statement |
| Idempotency | A claim of an already-claimed task is refused; the SQL is the guard |
| Transaction semantics | One statement per decision; no long-lived lock held across I/O |
| Error handling | Typed errors; a `UniqueViolation` is "workspace taken", not a crash |

The predicates living in `orchestration/sql/*.sql` and being `include_str!`-ed by
both sides is the key property: Rust cannot drift from Python on *what is
claimable*, because it reads the same bytes.

---

## 4. Database authority

**Python PostgreSQL behaviour remains the production compatibility authority.**
Rust is a *verified candidate*, exercised only through:

- `tests/test_rust_claim.py` (Rust claim/reclaim over a real PostgreSQL),
- `tests/test_rust_parity.py` (Rust answers vs Python answers),
- `tools/parity_matrix.py` (282 real comparisons, §6).

Nothing in production dispatch calls Rust. The switch procedure
(`docs/RUST_PYTHON_COMPATIBILITY.md` §6) ends with removing the Python version,
and has not been executed. This satisfies the rule "Rust must not silently become
the production authority".

Preserved invariants:

- **Database contracts**: `nexus-storage` reads the same columns Python writes;
  `REQUIRED_COLUMNS` in `cloud_store.py` is the schema guard for both.
- **Idempotency**: the claim SQL, shared verbatim, is the single definition.
- **Transaction boundaries**: unchanged; Rust does not widen them.
- **Migration compatibility**: Rust never runs migrations; Python
  `apply_migrations` owns the schema.
- **Rollback**: because Rust holds no state Python does not, removing the Rust
  call path reverts to Python with no data migration.

---

## 5. Feature-by-feature reconciliation matrix

| Feature | Python (authority) | `nexus-storage` | Status |
| --- | --- | --- | --- |
| Claim a task (lease + fencing token) | `LabStore.claim_task` | `claim_task` (SQLx) | **PARITY** (tested) |
| Reclaim expired lease | `LabStore.reclaim_expired` | `reclaim_expired` | **PARITY** (tested) |
| Claim eligibility predicate | `orchestration/sql/*.sql` | `include_str!` same files | **SHARED SOURCE** |
| Workspace/branch lock | partial unique index `uq_tasks_workspace_active` | reads same index | **PARITY** |
| Run transition machine | `_RUN_ENTERED_FROM` | `nexus-domain` | **PARITY** (49 cases) |
| Task transition machine | `control_plane._ALLOWED` | `nexus-domain` | **PARITY** (100 cases) |
| Workflow transition machine | `nexus_workflow.can_transition` | `nexus-domain` | **PARITY** (90 cases) |
| Plan validation | `nexus_planner.validate_tasks` | `nexus-domain::dag` | **PARITY** (15 cases) |
| Plan hash | `nexus_workflow.plan_hash_of` | `nexus-domain` | **PARITY** (byte-for-byte, 4) |
| Retry arithmetic | `nexus_retry` | `nexus-domain::retry` | **PARITY** (24 cases) |
| Scheduler claim decision | `nexus_core` mirror | `nexus-core` | **PARITY** (`test_rust_claim`) |
| Durable dispatch journal | `dispatch_journal.py` | — | **NOT IMPLEMENTED** in Rust (Python only) |
| Evidence signing | `nexus_evidence.py` | — | **NOT IMPLEMENTED** in Rust |
| HTTP API | `web_server.py` | — | **NOT IMPLEMENTED** in Rust |
| OpenHands transport | `cloud_client`/`cloud_direct` | — | **NOT IMPLEMENTED** in Rust |
| Intent router | `nexus_intent.py` | — | **NOT COMPARABLE** (language-sensitive regex) |
| Task-kind derivation | `nexus_lab` | — | **NOT COMPARABLE** (language-sensitive regex) |

The last two are deliberately not migrated: they are language-sensitive regexes
with a history of subtle production bugs, and the Rust validator carries the
`kind` it is given rather than deriving one (`docs/RUST_MIGRATION.md` §4).

---

## 6. Measured parity

`python -m tools.parity_matrix --json parity.json` builds `nexus-rs`, runs both
implementations on the same inputs, and records one row per **distinct**
comparison (a repeated `(from,to)` pair is not counted twice):

| Area | Compared | Agree |
| --- | ---: | ---: |
| control-plane transitions | 100 | 100 |
| workflow transitions | 90 | 90 |
| run transitions | 49 | 49 |
| retry arithmetic | 24 | 24 |
| plan validation | 15 | 15 |
| plan hash | 4 | 4 |
| **Total** | **282** | **282** |

6 areas are recorded `NOT IMPLEMENTED` and 2 `NOT COMPARABLE` — named, not
counted. The gate is pinned by `tests/test_parity_matrix_tool.py` (≥ 100 real
comparisons, zero disagreements).

---

## 7. What changed in this increment

- **No Rust code was deleted** — there was no duplicate to delete.
- `tools/parity_matrix.py` added: the machine-readable parity counter the brief
  requires, which counts real comparisons instead of test functions.
- `tests/test_parity_matrix_tool.py` added: pins the ≥100-comparison gate.
- This document replaces the brief's premise with the repository's facts.

## 8. Open, reproducible items

- A **second** live OpenHands account is not available here, so a two-account
  failover stays proven only against the local fakes + PostgreSQL.
- Migrating the durable dispatch journal to Rust is **not** attempted: it is
  newer than the Rust storage crate and has no parity oracle yet. Doing so would
  be new work, not reconciliation.
