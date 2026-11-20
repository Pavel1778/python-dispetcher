# Rust Migration

> **Scope:** this is the historical NEXUS 2.0 sidecar report. Its phase numbers
> are not milestones of the 3.0 migration. For the current storage candidate,
> gates and measured results see [NEXUS 3](../reports/nexus3/README.md).

2.0 status: **Phase 3-4 delivered, partial by design.** The deterministic core of
NEXUS is implemented in Rust with proven parity; the I/O, SQL and HTTP layers
remain in Python. This document says exactly what moved, what did not, why, and
what the evidence is. It does not claim more than was measured.

## 1. What "migrated" means here

The requirement is explicit: *"Do not perform a blind one-shot rewrite"* and
*"Every migrated component must have one authoritative implementation."* A
component is migrated only when:

1. its Python behaviour is documented;
2. language-independent contract tests exist;
3. the Rust version is implemented;
4. both run against the same cases and the answers are compared;
5. the parity is proven (not asserted), and the Python version is removed only
   after that.

A component that merely *compiles* is not migrated. The gate is step 4.

## 2. The workspace

```
rust/
  Cargo.toml                    workspace: three crates, shared deps, LTO release
  crates/
    nexus-domain/               pure decisions: state, DAG, retry
      src/state.rs              the three state machines
      src/dag.rs                plan validation + the plan version hash
      src/retry.rs              the retry arithmetic
      src/error.rs              the error model (thiserror)
    nexus-core/                 the scheduler decision over nexus-domain
      src/scheduler.rs          which task is claimable, is the run finished
    nexus-cli/                  the CLI and the parity oracle
      src/main.rs               `nexus-rs`
```

Why three crates and not one: the requirement says *"Avoid a monolithic Rust
crate."* `nexus-domain` has no dependency on `nexus-core`; `nexus-core` depends
on `nexus-domain`; `nexus-cli` depends on both. A future HTTP service can depend
on `nexus-core` alone, without the CLI.

Dependencies are chosen for the work, not adopted wholesale:

| Crate | Why |
|---|---|
| `serde` / `serde_json` | the wire shapes are JSON; the Python side is JSON |
| `sha2` | the plan hash and the retry spread are SHA-256, as in Python |
| `thiserror` | the error model, so a caller matches a variant, not a string |
| `clap` | the CLI, for the same reason the Python CLI uses `argparse` |

Tokio, Axum and SQLx are **not** dependencies yet. They would be needed by an
HTTP service and a database layer, and those are not migrated: adopting them now
would add a large, unproven surface with nothing to run on it. The architecture
document says where they would go when the claim path itself moves.

## 3. What is migrated, and the parity evidence

| Component | Python source | Rust | Parity test |
|---|---|---|---|
| Run state machine | `nexus_lab_store._RUN_ENTERED_FROM` | `state::RunState` | exhaustive (7×7) |
| Chat workflow machine | `nexus_workflow._ALLOWED` | `state::WorkflowState` | exhaustive (10×9) |
| Control-plane task machine | `control_plane._ALLOWED` | `state::TaskState` | exhaustive (10×10) |
| Plan validation + DAG check | `nexus_planner.validate_tasks` | `dag::validate_tasks` | 4 valid + 11 invalid plans |
| Plan version hash | `nexus_workflow.plan_hash_of` | `dag::plan_hash_of` | byte for byte, 4 plans |
| Retry arithmetic | `nexus_retry.cycle_wait` / `refused_wait` | `retry::cycle_wait` / `refused_wait` | 18 + 6 cases |
| The claim decision | `LabStore.claim_task`'s SQL | `scheduler::claimable` | real runs in a live store |

The parity test is `tests/test_rust_parity.py`. It is a Python test because the
Python implementation is what it compares against and the repository's suite is
Python. It builds the cases **from the Python side** - the real tables, the real
validators - feeds them to the Rust binary, and compares. A disagreement fails
the suite, named by case.

The comparison is exhaustive where it can be: every `(from, to)` pair of all
three state machines, not a sample. The run table alone is 49 pairs; the
workflow table 90; the task table 100. A hand-picked sample would not catch the
one transition a refactor drops.

### The claim decision, over real runs

`test_the_claim_decision_agrees_on_real_runs` builds a two-task chain on one
branch in a **real PostgreSQL lab store**, then walks it through three states:

1. nothing claimed - `t1` is claimable, `t2` is not (its dependency is not
   verified);
2. `t1` dispatched - `t2` is still not claimable (the branch is busy);
3. `t1` verified - `t2` is claimable, and Python's `claim_task` takes it.

At each state, both the Python SQL and the Rust `claimable` are asked, and the
answers must match. This is the "same test cases, compare outputs and state
transitions" the requirement asks for, on the boundary that matters.

## 4. What is deliberately NOT migrated, and why

Honesty here is part of the deliverable. These are not oversights:

- **The intent router** (`nexus_intent.py`). It is 673 lines of carefully tuned
  regular expressions over Russian and English morphology, with negative
  lookaheads that exist because of specific production incidents (the
  "завершившегося" participle collision). A Rust rewrite would be a
  re-implementation of a large, subtle, language-sensitive artefact with no
  behavioural gain and a real risk of divergence. It stays in Python, one
  authoritative implementation.
- **The task-kind derivation** (`nexus_planner.task_kind`). Same reason: it
  reads Russian and English stems to decide whether a task must change the code.
  The Rust validator carries whatever `kind` the caller supplies and does not
  derive one; the parity test excludes the derivation rather than pretending to
  match it.
- **The SQL claim itself.** `scheduler::claimable` is the *decision*; the
  `SELECT ... FOR UPDATE SKIP LOCKED`, the advisory branch lock and the row
  update stay in Python. Migrating the decision first is what makes the
  migration safe: the decision is pure and comparable, the SQL is not.
- **The HTTP layer, the store, the clients.** These are I/O. The requirement's
  Priority B-D areas need a service boundary and a database driver, which is a
  larger, riskier change than the pure core. They are next, not now.

## 5. Interoperability contract

The two implementations do not share memory and do not call each other in
production yet. The contract is:

- **JSON messages, versioned by shape.** `nexus-rs parity` reads a document and
  writes a document; the shapes are in `nexus-cli/src/main.rs` and are the same
  shapes the Python modules use. A change to one side that the other does not
  follow fails the parity test.
- **The Python side stays authoritative.** Nothing in the running NEXUS service
  calls Rust. The migration is a *candidate* implementation until the parity
  gate is widened to the SQL path and the service is switched deliberately.
  Switching is a separate, reviewable change - not a side effect of this one.
- **No second implementation is left running.** Where a component is migrated
  and its Rust version is authoritative (none yet in production), the Python
  version would be removed in the same change. Today both exist and the parity
  test is what keeps them honest; that is a transitional state, stated as such.

## 6. The benchmark, and what it does and does not show

`tools/benchmark_rust.py` measures the migrated decisions in both
implementations on identical inputs, on one machine. Real numbers, best of five,
20 000 cases (`benchmarks/rust_vs_python.json`):

| Measure | Time |
|---|---|
| Python, validate + hash + retry (in-process) | 4.88 s |
| Rust, all three, one process for the whole batch | 1.27 s |
| **Ratio** | **~3.8–4.2×** (varies run to run; a fresh run on the 2.0 candidate measured 4.0×) |

What this shows: for the pure decisions, Rust is materially faster even when the
figure **includes one process start** for the whole batch. What it does **not**
show: any end-to-end product speedup. The slow paths of NEXUS are network-bound
(a provider, OpenHands, GitHub) and database-bound; a faster plan hash does not
move them. Rust is **not in the production execution path** — it is invoked only
by the parity test and this benchmark, and **Python remains authoritative** — so
this document claims a ~3.8–4.2× improvement **on the migrated decisions only**,
and nothing about the product.

## 7. How to build and test

```bash
cd rust
cargo test            # 29 tests: nexus-domain 14, nexus-core 15
cargo build --release # the nexus-rs binary

# The parity gate (needs a disposable PostgreSQL for the two DB cases):
TEST_SUPABASE_DB_URL=postgresql://user:pw@127.0.0.1:5432/db \
  python -m pytest tests/test_rust_parity.py -q

# The benchmark:
python tools/benchmark_rust.py --cases 20000 --repeat 5
```

`tests/test_rust_parity.py` skips cleanly when no Rust toolchain is present
(`cargo` absent and `NEXUS_RUST_BIN` unset) - an environment without Rust cannot
run the parity, and says so rather than passing.

## 8. Next steps, in order

1. Migrate the **claim transaction** (Priority A): keep `claimable` as the
   decision, add a Rust store client (SQLx) that performs the same
   `FOR UPDATE SKIP LOCKED` claim, and compare against Python over a real
   database under concurrency (N workers, N distinct tasks).
2. Migrate **run lifecycle transitions** (Priority A) once the claim path is
   authoritative, using the same parity gate.
3. Add an **HTTP service** (`axum` + `tower`) only when a component is ready to
   be authoritative, and put it behind the same JSON contract.
4. Remove the Python version of each component **in the same change** that makes
   the Rust version authoritative, so no component is ever duplicated
   indefinitely.

Until step 1 is done and reviewed, the Python core is authoritative and Rust is
a verified candidate. That is the honest state of the migration.
