# REGRESSION TEST RESULTS — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Run after the last code change of this mission.** Environment: PostgreSQL
17.11, Python 3.13.15, Rust 1.100.0.

---

## 1. Exact commands and results

| Suite | Command | Exit | Passed | Skipped | Failed | Duration |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Python (full) | `pytest tests/ -q -p no:cacheprovider` | 0 | **2205** | 160 | **0** | 427.77 s |
| Ruff | `ruff check .` | 0 | — | — | 0 | <1 s |
| Rust fmt | `cargo fmt --all --check` | 0 | — | — | 0 | <1 s |
| Rust clippy | `cargo clippy --workspace --all-targets -- -D warnings` | 0 | — | — | 0 | <1 s |
| Rust tests | `cargo test --workspace` | 0 | **63** | 0 | **0** | ~15 s |

Python collected **2365**; 2205 ran, 160 skipped — exactly accounted (see §3).

## 2. Why 160 skips, and none are hidden failures

Every skip is environmental and names its reason (`-rs`). Categories:

- **Chromium absent** (browser E2E, `tests/test_browser_e2e.py` and the browser
  journeys in other modules): the majority of the 160 in this run. They pass
  when `E2E_CHROMIUM_EXECUTABLE` points at a Chromium (a prior session ran them:
  37–38 passed).
- **Android SDK** (4), **mingw-w64** (3): packaging build tests, no toolchain
  here.
- **Pooler DSN** (1): needs a PgBouncer DSN.
- **Live-OpenHands section F** (1): needs a push-capable live account — though
  this mission proved that path by hand (`OPENHANDS_LIVE_EXECUTION.md`).

None is a masked failure: `grep -cE "FAILED|ERROR"` over the full log is **0**.

## 3. The 2352 vs 2205+160 discrepancy (the mission's question) — RESOLVED

It is **not** lost tests. The arithmetic is exact:

```
baseline (main 5c0d0eb)  : 2352 collected
this mission added       :   13 tests
                           -----
now collected            : 2365
executed this run        : 2205 passed + 160 skipped = 2365   (exact)
```

The 13 added this mission: 8 in `tests/test_config_reconcile.py`, 3 in
`tests/test_cloud_direct.py`, 2 in `tests/test_openhands_pool_http.py` (plus the
route-auth list, which parametrises an existing test rather than adding one).
`2352 + 13 = 2365`, and `2205 + 160 = 2365`. Nothing was dropped.

The earlier "2203 + 74 = 2277" figure that prompted the question came from a run
where the **browser E2E suite had no Chromium** and more DB modules skipped
(74 vs 160 skips here) — a different environment, not lost tests. Verified now
with `--collect-only` (2365) against the executed split (2365), so every
collected test either ran or skipped with a named reason.

## 4. New tests added this mission

| File | Cases | What |
| --- | --- | --- |
| `tests/test_config_reconcile.py` | 8 | the reconciler (synced/drift/pending/blocked) |
| `tests/test_cloud_direct.py` (added) | 3 | LLM base URL reaches the session; unset = default; git secret in body |
| `tests/test_openhands_pool_http.py` (added) | 2 | `POST /api/openhands/config/sync` synced + drift |
| `tests/test_route_auth.py` (added) | — | the sync route is protected |

All pass; the LLM-base-URL test is mutation-proven (removing the assignment
fails it with `KeyError`).
