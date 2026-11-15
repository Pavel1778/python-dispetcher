# NEXUS — Release Readiness

> **STATUS: READY FOR PR (with documented limitations).** The independent
> acceptance of the Performance & Hybrid Runtime Evolution program.
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

- **Branch:** `engineering/performance-evolution`
- **Base:** `a7442632bb05492f65f72b1bf2e3ec94436625d5`
- **Final commit (this audit):** see the branch HEAD (the audit adds
  `00819e6` and the docs commit that carries this file).
- **Companion:** [INDEPENDENT_ACCEPTANCE](./INDEPENDENT_ACCEPTANCE.md),
  [SOAK_TEST_RESULTS](./SOAK_TEST_RESULTS.md).

---

## 1. Verdict

**READY FOR PR.** No P0/P1 defect. The full regression suite is green, ruff is
clean, reliability and isolation are verified, the pool closes and recovers, no
leak was found, performance is reproduced, and Render compatibility is checked
statically. Two small P3 defects found during the audit are fixed with tests.

## 2. Criteria

| Criterion | Status | Evidence |
|---|---|---|
| No P0/P1 defect | ✅ | §5 of [INDEPENDENT_ACCEPTANCE](./INDEPENDENT_ACCEPTANCE.md) |
| Full regression suite green | ✅ | 1870 passed, 9 skipped, 0 failed |
| Ruff clean | ✅ | `ruff check .` → All checks passed |
| Reliability tests green | ✅ | pool exhaustion, drop, outage, restart, worker-outlives-server |
| Project isolation / RBAC | ✅ | `test_route_auth`, `test_auth`, isolation group (267 tests) |
| Pool closes and recovers | ✅ | close/reopen, double close, drop recovery (measured) |
| No confirmed leak | ✅ | current RSS flat; DB conns constant 9 over 2.28M reads |
| Performance reproduced | ✅ | DB p99 42 ms @100c; API p99 85 ms @50c; extract 72 ms |
| Render compatibility | ✅ | §4 |
| Limitations documented | ✅ | §6 |
| Working tree clean, changes committed | ✅ | (at the end of the audit) |

## 3. Performance summary (MEASURED, reproduced)

| Metric | Before | After |
|---|---:|---:|
| DB p99 @100 concurrent | 713 ms | **42 ms** |
| API p99 @50 concurrent | 2725 ms (old backlog) | **85 ms** |
| API throughput @50 | 166 ops/s | **691 ops/s** |
| Archive extract 1000 members | 165 ms | **72 ms** |
| app.js over the wire | 206,711 B | **52,146 B** |

All Python changes; the Rust archive pilot stayed **PYTHON_KEEP** and is not in
the runtime.

## 4. Render compatibility (VERIFIED, static)

| Item | Finding |
|---|---|
| `render.yaml` | **unchanged** by this branch |
| start command | `python -m orchestration.web_server ...` unchanged |
| `requirements.txt` | `psycopg[binary,pool]` — the `pool` extra adds `psycopg_pool`, no compiler needed |
| Python version | Render uses 3.12.6; every changed file parses under 3.12 (`ast` feature_version) and 3.13 |
| health / readiness | `/healthz` (liveness) and `/readyz` (DB round trip) unchanged |
| shutdown | `App.stop()` drains loops then closes the pool |
| connection config | `SUPABASE_DB_URL` unchanged; the pool adds `NEXUS_DB_POOL_*` with safe defaults |
| worker service | `lab_worker` unchanged |

## 5. Rollback

1. **Revert the branch** (it is not merged): the runtime change is confined to
   `orchestration/cloud_store.py`, `orchestration/web_server.py`,
   `orchestration/file_lab.py` and `requirements.txt`. No schema change, no
   migration.
2. **Deploy the previous build** (Render "Deploy previous") if already shipped.
3. **Pool off:** there is no feature flag to disable the pool; a rollback is the
   revert. The Python fallback for every path is the previous code itself.
4. **No data migration** is needed in either direction.

## 6. Known limitations (VERIFIED / UNKNOWN / BLOCKED)

- **Branch base vs `origin/main`:** `origin/main` advanced (PR #39 merged); the
  branch still bases on `a744263` and shares edited files with the new main, so
  a **rebase/merge is required before the PR can land**. Not done here (out of
  scope). See [INDEPENDENT_ACCEPTANCE](./INDEPENDENT_ACCEPTANCE.md) §0.
- **Production p95/p99 under real load** — BLOCKED (no paid Render metrics/OTel).
- **HTTP/1.1 keep-alive, minification, ETag** — assessed, deferred (below).
- **`do_HEAD`** — not implemented (pre-existing; not introduced here).
- **Multi-hour soak** — the 60-minute run is flat; longer is UNKNOWN.

## 7. Frontend completion assessment (Stage 7)

| Item | Benefit | Cost | Risk | Decision |
|---|---|---|---|---|
| gzip | payload 355 KB → 141 KB (−60 %) | none (done) | low | **done** |
| HTTP/1.1 keep-alive | fewer TCP handshakes on a page of 8 assets | medium (connection lifetime) | medium | **defer** — assets are versioned and cached 300 s; the measured server cost is ~1 ms/request and the page is one screen |
| asset minification | a smaller `app.js` | high (no build step today; a minifier dependency + CI) | medium | **defer** — gzip already gives most of it; add only with a build step |
| ETag / If-None-Match | a 304 instead of a body on repeat visits | low | low | **defer** — `?v=<hash>` + `max-age=300` already revalidates cheaply; low value now |

None of these is needed for correctness; each is a future improvement with the
reason above.

## 8. Observability assessment (Stage 8)

Existing: structured logging (`logging` per module), error classification
(`StoreUnavailable`/`CloudUnavailable`/`CloudRateLimited`), `/api/system` with a
`db_pool` block (size, idle, waiting, requests, wait time), `/readyz`.

**OpenTelemetry: not added.** There is no measured need; the pool metrics and
the existing logs answer the operational questions, and OTel would add a
dependency and a collector to run. The minimal production metric set to watch:
`db_pool.waiting` (connection pressure), `db_pool.wait_ms` (acquisition time),
`/readyz` status, request p99, and the error-rate by class. This is a
recommendation, not an implemented change.

## 9. Rust decision (Stage 9)

**PYTHON_KEEP, unchanged.** Rust is absent from `requirements.txt` and from the
runtime imports; `orchestration/*` imports without any Rust toolchain; the two
prototypes live only under `benchmarks/` and are not built by the app. No
further Rust integration is pursued without a new measured justification.
