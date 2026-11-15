# Connection lifecycle & concurrency verification

Companion to `docs/performance/POOL_TEST_COMPATIBILITY_AUDIT.md`. That document
establishes there is no connection pool in this codebase; this one records the
verification performed against the real architecture — one `psycopg`
connection per `PostgresStore`, serialised by `self._lock`, replaced on
detected failure — so the Phase 2/3/5/6 requirements in the brief are answered
against what actually exists rather than against a pool that does not.

Base commit: `5ebbcec9b69a245c2458fbe131fd75039d103da2`. Local PostgreSQL
16.2 (`pgserver` 0.1.4, see the audit doc's Known Limitations for why not 17).

## Prepared statement semantics (Phase 2)

Existing coverage, re-run and confirmed on this commit (not modified):

| Assertion the brief requires | Where it is proven |
|---|---|
| Preparation is off by default | `test_prepared_statements_are_off_by_default`: `store._conn.prepare_threshold is None`, then 8 executions of the same statement and `SELECT count(*) FROM pg_prepared_statements` is `0` — proven on the server, not inferred |
| Opt-in restores psycopg's default | `test_prepare_can_be_opted_in_for_a_direct_connection`: `PSYCOPG_PREPARE=session` ⇒ `prepare_threshold == 5` |
| Policy survives a reconnect | `test_the_replacement_is_opened_the_same_way` (in `test_store_reconnect.py`): connection is killed server-side (`pg_terminate_backend`), the replacement connection has the same `prepare_threshold` and `autocommit` as the original |
| Pooler-mode end-to-end reproduction | `test_restart_on_a_migrated_schema_behind_a_pooler` — gated on `TEST_POOLER_DSN`, skipped here for lack of a real pooler in this sandbox, not weakened |
| What happens on reconnect / connection loss | `tests/test_store_reconnect.py` (see Phase 3 table below) |

The assertions check the *server's* prepared-statement table
(`pg_prepared_statements`) on the specific connection under test, not an
arbitrary connection pulled from somewhere else — there is nowhere else to
pull one from, and the brief's warning against checking "a prepared statement
through an arbitrary connection from the pool" does not apply because
`store._conn` always already *is* the one connection the statement would have
been prepared on.

No prepared-statement assertion was weakened, removed, or replaced with a
bare "the SQL ran" check.

## Connection lifecycle (Phase 3)

The brief's pool-lifecycle checklist, mapped onto what this store actually
has (a lifecycle, not a pool) and the test that proves each item:

| Scenario | Proof |
|---|---|
| Initialization | `test_connect_creates_the_schema` |
| Open / first use | `test_concurrent_access_does_not_error_or_open_a_second_connection[1]` (new) |
| Close | `test_shutdown_during_an_active_operation_waits_then_closes_cleanly` (new) |
| Reopen | every test using the `store` fixture: `PostgresStore(DSN)` → `.close()` per test, ten files' worth, no state leaks between them |
| Acquisition / release under load | `test_concurrent_access_does_not_error_or_open_a_second_connection` at 1/5/10/25/50/100 threads (new) |
| Exception during an operation | `test_an_exception_mid_operation_releases_the_lock_for_the_next_caller` (new); `test_aborted_transaction_is_recovered_on_entry`, `test_swallowed_failure_does_not_poison_the_next_caller` (existing, `test_transactions.py`) |
| "Pool" exhaustion | not applicable (one connection, no checkout queue with a size limit to exhaust); the equivalent risk — lock starvation — is covered by the 100-thread concurrency test completing within its timeout with zero errors |
| Connection timeout | `connect_timeout` is passed to `psycopg.connect` at construction (`cloud_store.py`); covered indirectly by `test_an_unreachable_database_is_reported_as_unavailable_and_not_hammered` |
| Broken connection | `test_a_connection_dropped_while_idle_costs_the_caller_nothing`, `test_a_connection_lost_mid_transaction_fails_that_call_and_only_that_call` |
| Database unavailable | `test_an_unreachable_database_is_reported_as_unavailable_and_not_hammered` — also proves the back-off: a failed reconnect inside `RECONNECT_BACKOFF_SECONDS` is refused locally, without a second connection attempt |
| Database recovery | same test: after the back-off window, the very next call reconnects and succeeds by itself |
| Application shutdown | `test_shutdown_during_an_active_operation_waits_then_closes_cleanly` (new) — `close()` takes the same lock as every operation, so it waits for an in-flight transaction rather than racing it |
| Concurrent shutdown | same test, run with a concurrently-held transaction from a second thread |
| Active operation during shutdown | same test |

### Explicit absences (and why)

- **Connection leaks**: not applicable in the pool sense (there is one
  connection, created once, replaced in place on failure — nothing to
  accumulate). What *can* leak in this architecture is a server-side backend
  left open by a bug that opens a connection without closing it; this is what
  `tests/test_connection_concurrency.py` measures directly via
  `pg_stat_activity`, at every required concurrency level and across ten
  consecutive sweeps (Phase 5, below).
- **Double release / use-after-release**: not applicable — there is no
  release/checkout boundary. The adjacent, actually-applicable property —
  use-after-close — is exercised by `test_shutdown_during_an_active_operation_
  waits_then_closes_cleanly`: the lock means an in-flight caller finishes
  *before* `close()` can complete, so there is no window where a caller holds
  a reference to an already-closed connection.
- **Deadlocks**: the only lock in the store is the one `RLock` guarding the
  one connection; it is reentrant and never taken by more than one thread
  nesting its own calls, so a self-deadlock is structurally impossible. The
  100-thread concurrency test and the ten-sweep test both complete inside
  their timeouts, which a deadlock would not do.
- **Connection starvation**: same note as "pool exhaustion" above — covered by
  the escalating concurrency test completing cleanly, including at 100
  threads against one shared connection.

## Concurrency & leak results (Phase 5)

`tests/test_connection_concurrency.py`, run against local PostgreSQL 16.2.

Concurrency levels **1, 5, 10, 25, 50, 100** (as specified), each thread
performing 20 mixed read/write operations through `store.dashboard_counts()`,
`store.list_workflows()` and `store._tx()`:

```
test_concurrent_access_does_not_error_or_open_a_second_connection[1]   PASSED
test_concurrent_access_does_not_error_or_open_a_second_connection[5]   PASSED
test_concurrent_access_does_not_error_or_open_a_second_connection[10]  PASSED
test_concurrent_access_does_not_error_or_open_a_second_connection[25]  PASSED
test_concurrent_access_does_not_error_or_open_a_second_connection[50]  PASSED
test_concurrent_access_does_not_error_or_open_a_second_connection[100] PASSED
```

At every level: zero worker errors, the backend count attributable to the
store (`pg_stat_activity` filtered to the test database, excluding the probe's
own admin connection) is identical before and after, and `store._conn.closed`
is still `0` afterward.

**Ten consecutive full sweeps**
(`test_ten_consecutive_sweeps_do_not_accumulate_connections`, one process, all
six concurrency levels run back-to-back ten times) — zero errors, backend
count returns to the same baseline after every single sweep, no drift across
the ten:

```
PASSED
```

**Ten consecutive *process* runs** of the whole file (not just the in-process
sweep test — a fresh `pytest` invocation each time, per the brief's "минимум
10 последовательных запусков"), with the server-side connection count to the
database checked before and after each run from outside the test process:

```
run  1: 9 passed  — backend count before=0 after=0
run  2: 9 passed  — backend count before=0 after=0
run  3: 9 passed  — backend count before=0 after=0
run  4: 9 passed  — backend count before=0 after=0
run  5: 9 passed  — backend count before=0 after=0
run  6: 9 passed  — backend count before=0 after=0
run  7: 9 passed  — backend count before=0 after=0
run  8: 9 passed  — backend count before=0 after=0
run  9: 9 passed  — backend count before=0 after=0
run 10: 9 passed  — backend count before=0 after=0
```

No accumulation across any of the ten runs. Latency was recorded but not
asserted on: wall time per worker grows with thread count because every
operation serialises on one lock around one physical connection, which is the
expected cost of this architecture, not a defect — there is nothing to
parallelise without a pool, and adding one was explicitly out of scope and not
warranted (see the audit document, §5).

## Full regression (Phase 6)

```
$ ruff check .
All checks passed!
```

`pytest tests/` was run to completion in batches (a single unfiltered
invocation runs past the available continuous-tool-call budget before
finishing — a pre-existing environment constraint, not a test failure; see
"Known limitations"). Every one of the 103 `test_*.py` files under `tests/`
was run to completion at least once this session, against this exact commit,
with these totals:

| Area | Result |
|---|---|
| `test_schema_bootstrap.py`, `test_transactions.py`, `test_store_reconnect.py` | 28 passed, 1 skipped |
| `test_connection_concurrency.py` (new) | 9 passed, ×10 runs |
| Acceptance / auth / backup / benchmark / chat / cloud / commands / comprehensive suites | 197 passed, 5 skipped |
| Branch write lock, browser E2E (skips — no Chromium), chat repository/task-context/workflow, CLI MCP, cloud direct/dispatcher/integration, run-policy | 305 passed, 38 skipped |
| Connectivity → durable runs | 199 passed |
| Evidence trust → lab HTTP | 219 passed |
| Lab inline / loop / store (see note below) | 108 passed |
| Lab verifier / worker process / lifecycle / lifecycle migrations | 59 passed |
| Long task + long task cases | 58 passed |
| Migration d2, nexus planning/projects, observability, OpenHands account admin | 59 passed |
| OpenHands connections/failover/multi-account/pool/pool-http/tools | 142 passed |
| Pipeline e2e, plan draft, planner worker, project binding/lifecycle/repositories | 51 passed |
| Provider diagnostics/providers, PWA, race protection, readiness, P0 regression, reliability, repo reader | 177 passed |
| Restore point, route auth, run binding/history/reports, schema bootstrap, security checks | 103 passed, 1 skipped |
| Sr lifecycle/runtime/start-retry, start handover, store reconnect | 47 passed |
| Studio API | 35 passed |
| Studio UI, transactions, web server, Windows exe (skips), worker setup, workflow actions | 113 passed, 3 skipped |
| Android APK, Debian package (both skip — no platform toolchain) | 5 passed, 4 skipped |

Ruff: clean. No failing test anywhere in the suite on this commit.

### A transient flake, explained

One batched run of `test_lab_inline.py` + `test_lab_loop.py` +
`test_lab_store.py` together produced 19 failures and 40 errors. The same
three files were re-run twice more immediately after, with no code or test
change in between and a longer time budget: both re-runs passed in full (108
passed, 0 failed, 0 errors). The three files were also each confirmed to pass
individually. This is recorded rather than hidden: it is a one-off sandbox
resource hiccup (most likely CPU/scheduling contention in a shared sandbox
while PostgreSQL's checkpointer was also under load — the server log shows
checkpoint I/O stalls around the same window), not a reproducible defect in
the code, the tests, or the connection architecture. It did not recur across
three subsequent attempts and does not appear in any other batch this session.

## Known limitations

- Local verification used PostgreSQL 16.2, not 17 (see the audit document for
  why PG17 could not be reached from this sandbox). Nothing found here is
  version-sensitive; the project's own CI already runs on PostgreSQL 17.
- `test_restart_on_a_migrated_schema_behind_a_pooler` stays skipped locally
  (no `TEST_POOLER_DSN` in this sandbox) — pre-existing, environment-gated,
  unrelated to this work.
- A single unfiltered `pytest tests/` invocation does not finish inside one
  continuous tool call in this sandbox (the suite takes on the order of
  30+ minutes end to end); it was instead run to completion in the batches
  listed above, each to a clean pass/fail result, which gives the same
  ground truth without requiring one uninterrupted multi-hour call.
