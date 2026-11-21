# DATABASE RELIABILITY — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: VERIFIED.** Concurrency, isolation, the connection pool, migrations and
recovery are exercised against real PostgreSQL 17.11; the claim path is proven at
1–128 workers and mutation-tested.

---

## 1. Isolation and transactions

- **Claim**: one transaction, `SELECT … FOR UPDATE SKIP LOCKED` on the task row,
  a budget check on the locked run row, a branch advisory lock when the task has
  a repository. Nothing is claimed unless the run is `running`, not pausing, has
  iterations left and (bounded policy) is inside its deadline.
- **Fencing**: every post-claim mutation carries `AND lease_token=%s`, so a stale
  holder's write matches no row and is a no-op (`tests/test_race_protection.py`).
- **Reclaim**: `reclaim_expired` bumps the token and clears the owner in one
  statement.

## 2. Concurrency at width (this session)

`tests/test_concurrent_claims.py` — real `LabStore`, real PostgreSQL, one store
connection per worker thread:

| Case | Result |
| --- | --- |
| 40 tasks, 1 worker | every task claimed exactly once |
| 40 tasks, 4 workers | every task claimed exactly once |
| 40 tasks, 16 workers | every task claimed exactly once |
| 40 tasks, 64 workers | every task claimed exactly once |
| 1 task, 16 workers | exactly one wins; the recorded `lease_owner` matches |
| 256 tasks, 128 workers (`NEXUS_STRESS=1`) | every task claimed exactly once |

**Load-bearing:** changing `orchestration/sql/task_due.sql` to re-allow
`dispatched` tasks makes the 64-worker case fail with tasks claimed **3×** each.
The test catches a real regression, it is not decorative.

## 3. The connection pool

`PostgresStore` holds a bounded pool (`psycopg_pool`, default max 8).
`tests/test_connection_concurrency.py` proves: every thread completes, the
server-side backend count never exceeds `pool_max` (no leak), and `close()`
closes every pooled connection — at 1, 5, 10, 25, 50, 100 threads, repeated ten
times so a slow leak is visible.

## 4. Migrations

- The runner applies every `*.sql` in lexical order; a gap is fine.
- **An edited, already-applied migration is refused** (`MigrationError`), not
  re-executed under its recorded name — the history cannot claim one thing while
  the schema is another (`tests/test_schema_bootstrap.py`).
- The schema guard iterates `REQUIRED_COLUMNS` (all 32 tables), so a dropped
  column on any table is caught (`tests/test_run_profiler.py`).
- Migrations are idempotent and applied on first connect (`AUTO_MIGRATE`), since
  the Render free plan has no shell.

## 5. Recovery

- **Expired lease** → reclaimed and re-claimed with a higher fencing token; a
  bound conversation is not re-claimed (`recovery` diagnostic check, VERIFIED
  live).
- **Dropped connection** → `PostgresStore._tx()` replaces the connection on
  evidence (a poll, then `SELECT 1`), never takes a working one away
  (`tests/test_store_reconnect.py`).
- **Database unavailable** → `StoreUnavailable` → HTTP `503`, bounded by a
  reconnect back-off so an outage does not become a queue of slow requests.

## 6. Measured (this session)

```
pytest tests/test_concurrent_claims.py tests/test_connection_concurrency.py \
       tests/test_race_protection.py tests/test_schema_bootstrap.py -q
-> passed (the concurrency file 6 passed incl. stress)
```

## 7. Remaining risks

- The workspace lock is the partial unique index; an **outside** writer (a
  person, a bot) can still move a branch head under a task. The verifier catches
  it (the report's commit is not the branch head), but it cannot prevent it.
- A kill **before** the service answered a start is recovered by the 900 s lease,
  which is slower than the recorded-start path.
