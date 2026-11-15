# NEXUS — DB Pool Operations

> **STATUS: OPERATIONS GUIDE (MEASURED).** How the PostgreSQL connection pool
> behaves and how to operate it. Written with Phase 1 of the performance
> program.
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md),
[PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md).

---

## 1. What changed (VERIFIED)

Before: `PostgresStore` held **one** `psycopg` connection behind a
`threading.RLock` (`orchestration/cloud_store.py`). Every concurrent request
waited for that one connection. Measured: p99 **706–747 ms** at 100 concurrent
operations, throughput flat ~2.5–3.4k ops/s
(`benchmarks/db_concurrency_bench.py`, variant A).

After: a `psycopg_pool.ConnectionPool`. A caller takes a connection for the
duration of one transaction and returns it. Measured (variant B): p99
**~35 ms** at 100 concurrent, throughput ~4.4k ops/s. No lock.

The public store API is unchanged; the change is internal.

## 2. Configuration (VERIFIED)

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_DB_POOL_MIN` | `1` | connections kept warm |
| `NEXUS_DB_POOL_MAX` | `8` | maximum concurrent connections |
| `NEXUS_DB_POOL_TIMEOUT` | `30` (s) | how long a caller waits for a free connection before `StoreUnavailable` |
| `PSYCOPG_PREPARE` | off | `session` re-enables server-side prepared statements (only for a direct, session-mode DSN) |
| `AUTO_MIGRATE` | `true` | apply idempotent migrations on start |

**Sizing rule (INFERRED):** keep `NEXUS_DB_POOL_MAX` below the database's (and
any PgBouncer's) connection limit. A pool larger than the server's limit only
moves the queue. Behind a transaction-mode pooler, a small pool (the default)
is correct; the pooler multiplexes.

## 3. Connection lifecycle (VERIFIED)

- **Initialization:** the pool opens `min_size` connections at construction; a
  bad DSN raises `StoreError("database unavailable")` at startup (the store
  used to connect eagerly too).
- **Acquire:** `_tx()` calls `pool.getconn()`; on timeout the caller gets
  `StoreUnavailable` (mapped to HTTP 503 with no driver text).
- **Health check:** a cheap `check` runs on each acquire. A healthy idle
  connection has nothing to read, so it costs **no round trip**; a connection
  whose server end went away has pending input (the goodbye / EOF) and is
  discarded, and the pool opens a replacement. When the check itself cannot be
  made, the connection is used as before (never thrown away on doubt).
- **Commit / rollback:** a connection is committed on success, rolled back on
  any exception. An already-aborted transaction is rolled back on entry.
- **Return:** the connection is **always** returned to the pool — success,
  handled error, or cancel/KeyboardInterrupt.
- **Shutdown:** `close()` closes the pool and its connections.
- **Database unavailable:** the pool backs off between reconnection attempts
  (`reconnect_timeout`); callers fail fast with `StoreUnavailable` instead of
  each spending the whole connect timeout.

## 4. Failure behaviour (VERIFIED, tests)

| Scenario | Behaviour | Test |
|---|---|---|
| A connection dropped while idle | the next call works; the dropped one is replaced | `test_a_connection_dropped_while_idle_costs_the_caller_nothing` |
| A connection lost mid-transaction | that call fails with `OperationalError`; the store recovers | `test_a_connection_lost_mid_transaction_fails_that_call_and_only_that_call` |
| The database unreachable | `StoreUnavailable` ("database unavailable"); no driver text | `test_an_unreachable_database_is_reported_as_unavailable_and_not_hammered` |
| An outage over HTTP | 503 `{"error":"database unavailable"}`; `/healthz` 200, `/readyz` 503; recovers by itself | `test_an_outage_is_a_503_without_the_driver_text_and_ends_by_itself` |
| An aborted transaction on entry | `_tx()` rolls it back; the call succeeds | `test_aborted_transaction_is_recovered_on_entry` |
| Pool exhaustion | callers wait up to `NEXUS_DB_POOL_TIMEOUT`, then `StoreUnavailable` | (behaviour of `getconn`) |

## 5. What is deliberately NOT done (VERIFIED)

- **No automatic retry of a write** after an ambiguous commit result. A retry
  needs proven idempotency; none is assumed.
- **No schema change, no new migration.** The pool is a transport change.
- **No change to the prepared-statement policy.** A transaction-mode pooler
  reuses server sessions, so preparation stays off by default.

## 6. Operational checks

- **Readiness:** `GET /readyz` does a real round trip; 503 when the database is
  unreachable.
- **Pool pressure (UNKNOWN):** the pool exposes `get_stats()`; wiring it into an
  endpoint is a Phase 8 (observability) task, not done here.
- **Tuning:** raise `NEXUS_DB_POOL_MAX` only if the database limit allows and
  connection-wait time is measured to be the bottleneck.
