# NEXUS 3.0 — Phase 1: Architecture and compatibility

Status: design, grounded in the code that exists (see `PHASE0-AUDIT.md`).
Nothing here is a rewrite plan; it is the boundary map, the compatibility
contract, and the staged migration the later phases follow.

## 1. The shape

A **Rust-first modular monolith**, two processes, one database:

```
                      clients:  Web Studio | APK | DEB | EXE
                                   │  HTTP + SSE/WS (one contract)
              ┌────────────────────┴─────────────────────┐
              │  API process                             │
              │  auth · projects · chat · studio · MCP    │
              └────────────────────┬─────────────────────┘
                                   │  shared crates (nexus-domain, nexus-store, …)
              ┌────────────────────┴─────────────────────┐
              │  Worker process                          │
              │  claim · DAG · dispatch · verify ·       │
              │  selfQA · evidence · reports             │
              └────────────────────┬─────────────────────┘
                                   │
                  ┌────────────────┴─────────────────┐
                  │  PostgreSQL  ·  OpenHands · GitHub │
                  └────────────────────────────────────┘
```

Two processes, not many. The requirement says "no extra processes without a
measured reason": the API and the worker have different scaling and failure
profiles (a slow OpenHands poll must not stall a request), which is the measured
reason. Both depend on the **same** crates, so no business logic is duplicated.

## 2. Crate map (what moves where)

| Crate | Responsibility | State today |
|---|---|---|
| `nexus-domain` | state machines, DAG validation, plan hash, retry arithmetic | **done**, parity-proven |
| `nexus-core` | the claim **decision** over a snapshot | **done**, parity-proven |
| `nexus-store` | the claim **transaction** (SQLx) + fences + reclaim | **done this session**, parity-proven on real PG |
| `nexus-openhands` | the two transports, account pool, router, diagnostics | Python authoritative; port next |
| `nexus-github` | reads, guarded writes, per-project accounts | Python authoritative |
| `nexus-api` | Axum routers over `nexus-store` (parity with the Python routes) | not started |
| `nexus-worker` | the loop (claim → dispatch → poll → verify → record → decide) | Python authoritative; port after store |

The `nexus-cli` binary (`nexus-rs`) is already the parity oracle and stays so.

## 3. Data model (unchanged)

PostgreSQL is the single source of truth. All 50 tables stay; every migration in
3.0 is **additive** (new column with a default, new table, new index) in a **new**
file. `REQUIRED_COLUMNS` in `cloud_store.py` is the schema contract and must name
any column a store reads. No destructive migration; no rename; no drop.

## 4. API compatibility contract

The `/api` surface is frozen for the transition. A Rust route is added only when
it answers the **same** JSON shape for the same inputs; `docs/API.md` is the
written contract and the test suite is the executable one. During the transition
the Python server keeps serving every route; the Rust server is compared against
it (the same way `nexus-store` is compared against `LabStore.claim_task`).

The machine-checkable part of the contract (the list of routes and their auth
class) is `tests/test_route_auth.py`; a new route without auth fails it, in
either implementation.

## 5. What stays in Python, on purpose

- The **intent router** (`nexus_intent.py`) and **task-kind derivation** — large,
  subtle, Russian/English morphology with a history of production incidents. A
  Rust rewrite has no behavioural gain and a real divergence risk.
- The **HTTP layer** until the Rust API is at parity — the read paths are already
  single-digit milliseconds, so there is nothing to win by moving them first.

## 6. The staged migration (Phase 2 onward), in order

Each step is gated by a **parity test on real PostgreSQL**, and each step removes
the Python version in the same change that makes Rust authoritative (so no
component is duplicated indefinitely).

1. **Claim transaction** — *done this session* (`nexus-store`); not yet
   authoritative (the Python loop still calls `LabStore.claim_task`). The next
   change wires the worker to the Rust store behind a flag and compares in
   production-like conditions before removing the Python path.
2. **Run-lifecycle transitions** — port `_apply_run_state` / `_RUN_ENTERED_FROM`
   as SQLx mutations, parity-proven, then authoritative.
3. **Reclaim / stop / requeue** — the recovery paths (`reclaim_expired`,
   `nexus_executor_stops`, `abandoned`).
4. **Worker loop** — `nexus-worker` claims via `nexus-store`, dispatches via
   `nexus-openhands`, verifies via `nexus-github`, records evidence.
5. **API process** — `nexus-api` over the same crates; the studio connects
   unchanged (same contract, same appearance).
6. **Native apps** — Tauri 2 around the unchanged studio (Phase 5).

## 7. Honest status

Only `nexus-store` is new and proven this session (5 real-PG tests + 4
Python↔Rust parity tests). The rest is design. The claim is DB-bound, so the
win is **memory (~9×)** and a tighter p99, not end-to-end speed; this is stated
so no later report overstates it.
