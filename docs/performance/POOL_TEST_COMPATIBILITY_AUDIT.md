# PostgreSQL pool/test compatibility audit

**Date:** 2026-11-15
**Base commit:** `5ebbcec9b69a245c2458fbe131fd75039d103da2` (`origin/main`, PR #39 merge
commit — independently re-verified at the start of this audit, see §1).
**Scope:** investigate a reported incompatibility between the test suite and
"the new pooled PostgreSQL architecture", decide whether a `_conn`
compatibility property (proposed by another executor) is warranted, and fix
whatever the real root cause turns out to be.

## Headline finding

**There is no connection pool anywhere in this codebase.** Not in
`orchestration/cloud_store.py`, not anywhere else in `orchestration/`, not in
any open or closed pull request, not in `requirements.txt`, and not anywhere
in git history. `PostgresStore` has always held **exactly one physical
`psycopg` connection** for the life of the process, documented as a deliberate
choice in its own source:

```python
# A single connection is enough for one dispatcher process; the atomic
# claim still protects against *other* processes. Callers needing more
# concurrency can open one store per worker.
```

Every caller — every HTTP request thread, every background loop — serialises
on that one connection through `self._lock` (a `threading.RLock`), inside the
`_tx()` context manager. A dead connection is detected and transparently
replaced (`_reconnect()`); there is no `psycopg_pool.ConnectionPool`, no
`getconn()`/`putconn()`, anywhere in the code the dispatcher runs.

The word "pool" *does* appear in this codebase, but it refers to two unrelated
things, neither of which is an application-level database connection pool:

1. **An external PostgreSQL pooler in front of the database** — Supabase's
   transaction-mode pooler / PgBouncer (port 6543). The store is written to
   be *safe behind* one (server-side prepared statements are off by default
   for exactly this reason — see `tests/test_schema_bootstrap.py`), but it
   does not implement one itself.
2. **OpenHands account pools** — `nexus_openhands_pool`,
   `openhands_account_pool`: a business-logic concept (which OpenHands
   account services a task) with its own tables and its own `PoolService`
   class, unrelated to database connections.

## 1. Verifying ground truth (not trusting the prior report)

Per instruction, the actual repository state was checked directly rather than
assumed from the brief:

```
$ git rev-parse HEAD
5ebbcec9b69a245c2458fbe131fd75039d103da2
$ git rev-parse origin/main
5ebbcec9b69a245c2458fbe131fd75039d103da2
$ gh pr view 39 --json mergeCommit,state,mergedAt
{"mergeCommit":{"oid":"5ebbcec9..."},"state":"MERGED","mergedAt":"2026-11-14T23:53:30Z"}
```

`HEAD` and `origin/main` are identical and match the merge commit claimed for
PR #39. The working tree was clean before this audit began.

## 2. Phase 0 search (`rg -n "_conn|prepared.statement|prepare|pool.getconn|pool.putconn" orchestration tests`)

The search was run exactly as specified. Results:

- **`pool.getconn` / `pool.putconn`: zero matches anywhere.** No file in
  `orchestration/` or `tests/` calls either name. There is nothing to be
  compatible with.
- **`_conn`**: every match is either (a) `orchestration/cloud_store.py`'s own
  `self._conn` — the one real connection, not a pool handle — or (b) a test
  reading `store._conn` directly (`tests/test_schema_bootstrap.py`,
  `tests/test_store_reconnect.py`, `tests/test_transactions.py`). These tests
  are not working around an incompatibility; they are asserting on the actual,
  single, physical connection object the store already exposes as
  `self._conn`. There is no "return it to the pool" step missing, because
  there is no pool to return it to.
- **`prepare` / `prepared statement`**: `orchestration/cloud_store.py` sets
  `prepare_threshold` at connect time (`None` by default, `5` when
  `PSYCOPG_PREPARE=session` is set) and the tests in
  `tests/test_schema_bootstrap.py` assert this policy end-to-end, including a
  real `pg_prepared_statements` check. This is a *connection-level* setting on
  the one connection, not a per-checkout pool setting — it survives a
  reconnect unchanged (`tests/test_store_reconnect.py::
  test_the_replacement_is_opened_the_same_way`), which is exactly what a
  single-connection design should guarantee and a pool would have to work much
  harder to guarantee (session state does not automatically follow a
  connection borrowed from a pool).

## 3. Where did "pooled architecture" come from?

The brief states another executor proposed a `_conn` compatibility property
that calls `pool.getconn()`. That proposal does not match anything in this
repository's history (`git log --all --oneline -i --grep=pool` and a full
grep across every PR head, open and closed, show nothing that introduces
`psycopg_pool` or a `ConnectionPool` class). The most likely explanations,
in order of probability:

1. The "other executor" was reasoning from a *generic* assumption ("production
   Postgres services are pooled") rather than from this codebase, and
   proposed a fix for a problem this codebase does not have.
2. The brief is testing exactly the principle it states — "production
   architecture determines the correct testing model, not the other way
   around" — i.e. it is intentionally adversarial to see whether a
   `_conn`-as-pool-shim gets added without verifying the premise first.

Either way, the instruction not to accept the `_conn`-via-pool proposal
automatically was followed, and the investigation shows *why* it would have
been wrong to accept it: there is no pool for such a property to call into.
Adding one would not fix a compatibility problem (none exists) — it would
*introduce* a new, fake connection-pool abstraction around a single real
connection, adding a `getconn()`/`putconn()`-shaped API with no actual
multiplexing behind it. That is strictly worse than today: it invites a caller
to believe connections are independent and poolable when they are the same
physical socket serialised by one lock, which is the exact bug class the
brief's own description of "Вариант C" warns about (no clear return-of-
connection semantics).

## 4. Are any tests actually incompatible with the real architecture?

No. Every test file that reads `store._conn` or depends on prepared-statement
behaviour was run against the real architecture on this commit and passes:

```
$ NEXUS_DB_NAME=scratch nexus-env.sh test . \
    tests/test_schema_bootstrap.py tests/test_transactions.py \
    tests/test_store_reconnect.py -q
......s......................
28 passed, 1 skipped in 12.84s
```

(The one skip is `test_restart_on_a_migrated_schema_behind_a_pooler`, which is
gated on `TEST_POOLER_DSN` — a real transaction-mode pooler in front of
PostgreSQL — not available in this sandbox; it is a legitimate, pre-existing,
environment-gated skip, not a failure.)

No test needed adapting. No production code needed changing to make tests
pass. See `docs/performance/POOL_LIFECYCLE_VERIFICATION.md` for the full
regression, concurrency and leak results.

## 5. Architecture decision (Phase 1)

Given the finding above, the three options in the brief reduce to:

- **Вариант A (adapt tests to use public API / explicit pool checkout):** not
  applicable — there is no pool checkout to make explicit.
- **Вариант B (diagnostic context manager over the pool):** not applicable for
  the same reason. (`PostgresStore._tx()` already *is* the public,
  lock-guarded, commit/rollback-guaranteed context manager for every
  operation; it is not pool-backed and does not need to be.)
- **Вариант C (`_conn` compatibility property backed by a pool):** **rejected**,
  because there is no pool behind it to call `getconn()`/`putconn()` on. The
  existing `self._conn` attribute is not a compatibility shim; it is the one
  real connection, already safe to read directly in tests for exactly the
  reason Вариант C's danger does not apply: there is only ever one connection
  object for the life of the store, it is never "returned" anywhere, and nothing
  else can be handed the same physical connection concurrently without going
  through `self._lock`.

**Decision: keep the current single-connection-per-process architecture
unchanged.** It is production-proven (PR #2 "transaction-pooler support", PR
#9 "Hotfix: DuplicatePreparedStatement at startup behind a transaction
pooler"), already has mature regression coverage for exactly the pool-adjacent
failure modes that matter (a transaction-mode pooler recycling the session,
`DuplicatePreparedStatement`, a dropped connection, a mid-transaction drop, an
outage and recovery), and nothing in this audit found a defect in it. No
`_conn`-as-pool property was added. No pool was introduced.

## 6. What changed

**Production code: nothing.** No defect was found in `orchestration/
cloud_store.py`'s connection lifecycle, locking or reconnect logic.

**Tests: one new file**, `tests/test_connection_concurrency.py`, which did not
exist before and adds exactly the coverage the brief's Phase 5 asks for,
adapted to the real architecture (see
`docs/performance/POOL_LIFECYCLE_VERIFICATION.md` for what it proves and its
results at every required concurrency level). No existing test was weakened,
removed, or had an assertion softened.

## 7. Known limitations

- This audit's local verification ran on PostgreSQL 16.2 (bundled via the
  `pgserver` PyPI package), not PostgreSQL 17. The sandbox's network egress is
  restricted to `github.com`, `codeload.github.com`, `api.github.com`,
  `registry.npmjs.org`, `pypi.org` and `files.pythonhosted.org`; the only
  PyPI-distributed embedded-Postgres package reachable from here
  (`pgserver` 0.1.4) bundles PostgreSQL 16.2, and the one alternative that
  ships a PostgreSQL 17 build for Linux x86_64
  (`alexandre-fundcraft/pgserver` v0.4.0 on GitHub Releases) is hosted on
  `release-assets.githubusercontent.com`, which is not on the allowed host
  list — the download was attempted and confirmed blocked. Nothing in this
  audit's findings (single connection, no pool, reconnect, prepared-statement
  policy, locking) is version-sensitive between PostgreSQL 16 and 17; CI
  itself (which the previous report says ran on PostgreSQL 17) already covers
  that gap for anything that would be.
- `tests/test_schema_bootstrap.py::test_restart_on_a_migrated_schema_behind_a_pooler`
  stays skipped locally (no `TEST_POOLER_DSN`); this is a pre-existing,
  environment-gated skip, unrelated to this audit.
