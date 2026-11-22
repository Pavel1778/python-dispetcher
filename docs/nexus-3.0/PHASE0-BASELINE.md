# NEXUS 3.0 — Phase 0 baseline: tests, performance, screenshots

Every number here was measured in this session on this machine
(4-core sandbox, PostgreSQL 17.11 local, Python 3.13.15, Rust 1.100.0). Nothing
is carried over from an earlier report without a fresh run.

## 1. Test baseline (before any NEXUS 3.0 change)

| Suite | Command | Result |
|---|---|---|
| Full Python suite (incl. browser E2E) | `pytest tests/ -q` | **2094 passed, 9 skipped, 0 failed** (530 s) — **2098** after adding the 4 claim-parity tests (525 s) |
| Lint | `ruff check .` | clean |
| Rust workspace (existing 3 crates) | `cargo test` | **29 passed** (nexus-domain 14, nexus-core 15) |
| Rust store (new `nexus-store` crate) | `cargo test -p nexus-store` | **5 passed** against real PostgreSQL |
| Rust↔Python claim parity | `pytest tests/test_rust_claim_parity.py` | **4 passed** |

The 9 skips are environmental only (Android SDK, mingw-w64, a pooler DSN, the
live-OpenHands section). They are reported as skips, never as passes.

## 2. API / store performance baseline (`tools/benchmark.py`)

Real server + real PostgreSQL + the stand-in Cloud/GitHub/LLM servers. Seed:
5 projects, 100 conversations, 50 messages each. Milliseconds.

| Path | p50 | p99 |
|---|---|---|
| `studio_status` (`/api/studio`) | 0.90 | 1.62 |
| `list_projects` | 1.37 | 1.87 |
| `open_project` | 1.83 | 2.28 |
| `list_conversations` | 1.74 | 2.10 |
| `open_conversation` | 1.68 | 1.78 |
| `fetch_history` (50 messages) | 2.85 | 4.67 |
| `commands_registry` | 0.81 | 1.07 |
| `static_asset` | 0.62 | 0.75 |
| `search_by_content` | 2.33 | 3.10 |
| `export_conversation` | 3.18 | 3.53 |
| `chat_turn` | 12.72 | 18.51 |
| `start_run` | 31.09 | 35.82 |
| `store_dashboard_counts` | 0.62 | 2.10 |
| `file_upload` | 3.10 | 3.98 |
| `queue_claim` | 3.31 | 4.67 |

**Read path is single-digit milliseconds.** The slow paths are network-bound
(`chat_turn` waits on a provider; `start_run` waits on a plan and a conversation
start). This is why a wholesale Rust rewrite of the HTTP layer would not move
the numbers a user feels — and why the plan targets the *claim/worker* path and
memory, not the request path.

## 3. The claim path: Python vs Rust, in-process

The claim transaction (`LabStore.claim_task` in Python, `nexus-store` in Rust)
is the hot path of the worker. Both were measured **in-process** (one pool, N
fresh runs, one claim each), so the figure is the claim itself, not process
start.

| | p50 | p99 | mean | peak RSS |
|---|---|---|---|---|
| Python `LabStore.claim_task` | 1.22 ms | 4.31 ms | 1.39 ms | **43.1 MB** |
| Rust `nexus-store` claim (release) | 1.39 ms | 2.31 ms | 1.49 ms | **4.7 MB** |

**Honest reading.** The claim is **database-bound**: the latency is the same in
both languages, because the time is the round trip to PostgreSQL and the row
locks, not the code around it. The real, measured win of Rust here is **memory:
about 9× less** (4.7 MB vs 43.1 MB peak) for the same work, and a tighter p99
(2.31 ms vs 4.31 ms — fewer tail spikes, which matters under contention).

On a 4-core / 8 GB server this is the number that changes the operating budget:
the worker process's own footprint drops by an order of magnitude, leaving the
memory to PostgreSQL and the OS page cache, which is where it does the most good.

This is stated plainly because the requirement forbids claiming a speedup that
was not measured. **No end-to-end speedup is claimed** — the slow paths are
network-bound and unchanged.

## 4. Rust claim transaction: what was proven, and how

The new `rust/crates/nexus-store` crate performs the **same** claim as the
Python one: the run gate (locked row), the global backpressure cap under an
advisory lock, candidate selection with all three predicates (`_TASK_DUE`,
`_DEPS_VERIFIED`, `_BRANCH_FREE`) one row at a time (`FOR UPDATE SKIP LOCKED`),
the per-branch advisory lock and re-check, and the fenced take that spends one
iteration and issues a fresh `lease_token`.

Proof is a real comparison on real PostgreSQL, not a compile:

- `rust/crates/nexus-store/tests/claim_parity.rs` (5 tests): oldest-task-first;
  the token fence (a stale worker's bind is a no-op); a busy branch is not
  claimed twice; **10 concurrent workers take 10 distinct tasks** (no duplicate,
  no lost task); a pausing run accepts no claim. Migrations are applied from the
  real `migrations/` into a dedicated database, so the schema is the real one.
- `tests/test_rust_claim_parity.py` (4 tests): Python and Rust agree on which
  task is first, leave the same row shape (`dispatched`, one attempt, one
  iteration, a fresh token), refuse a busy branch identically, and — the
  load-bearing one — **5 Python and 5 Rust workers claiming together take 10
  distinct tasks**.

## 5. Screenshot baseline

Captured through real Chromium against the real studio (same stack the browser
E2E uses) into `docs/nexus-3.0/baseline/`:

- `login.png` (1440×900, dark)
- `studio-empty.png` (1440×900, dark)
- `studio-phone.png` (390×844, dark)
- `studio-light.png` (1440×900, light)

Reproduce with `tools/capture_studio_baseline.py`. These are the reference for
"the external appearance is preserved": after any change, re-run it and diff.

## 6. Environment facts that shaped the numbers

- PostgreSQL 17.11 on the same host (loopback), so DB latency is the floor.
- `max_connections` 100; the store uses `psycopg_pool`; Rust uses a 4–16
  connection SQLx pool.
- The per-process test database (`tests/_test_db.py`) isolates every pytest run;
  the Rust tests create and drop their own database so they never collide with
  the Python suite.
