# NEXUS — Independent Acceptance

> **STATUS: INDEPENDENT AUDIT (MEASURED).** An independent re-verification of the
> Performance & Hybrid Runtime Evolution program, performed from scratch against
> the real code, not by trusting the earlier reports.
>
> Evidence classes: **VERIFIED** (reproduced here), **MEASURED**, **INFERRED**,
> **UNKNOWN**, **BLOCKED**.

- **Branch:** `engineering/performance-evolution`
- **Base:** `a7442632bb05492f65f72b1bf2e3ec94436625d5`
- **Control commit (start of this audit):** `832dcb82ceb9c8805572114f4665a00eb898456c`
- **Environment:** PostgreSQL 17.11, Python 3.13.15, isolated databases
  (`nexus_accept`, `nexus_bench`, `nexus_soak`). No production contact.

---

## 0. Preservation & forensics (VERIFIED)

| Check | Result |
|---|---|
| Current branch | `engineering/performance-evolution` |
| HEAD | `832dcb82ceb9c8805572114f4665a00eb898456c` (matches the control commit) |
| Working tree | clean |
| 11 program commits present | yes (`a744263..832dcb8`) |
| `main` unchanged | yes — and `origin/main` has since advanced to `5ebbcec9` (PR #39 merged); this branch still bases on `a744263` |

**Note (documented, not a defect):** `origin/main` advanced while this branch was
developed. `orchestration/cloud_store.py`, `orchestration/web_server.py` and
`tests/test_long_task_cases.py` were changed on **both** sides, so a future PR
will need a rebase/merge; that is not done here (merges are out of scope). This
is recorded as a known integration task, not a correctness problem in this
branch.

## 1. Independent code review (VERIFIED)

### PostgreSQL connection pool (`orchestration/cloud_store.py`)

| Aspect | Finding |
|---|---|
| Init | `psycopg_pool.ConnectionPool`, `open=True`, `wait()` at construction so a bad DSN fails fast |
| min/max | `NEXUS_DB_POOL_MIN` (1) / `NEXUS_DB_POOL_MAX` (8); `max < min` is corrected up |
| Exhaustion | a caller waits `NEXUS_DB_POOL_TIMEOUT` then gets `StoreUnavailable` — measured, bounded, not a hang |
| Return on exception | **measured**: idle count identical before/after 5 failing transactions — no leak |
| close / reopen | `close()` closes the pool; a later call reopens it (`_ensure_pool`) — measured |
| double close | safe — measured |
| shutdown | `App.stop()` drains the loops then `store.close()` |
| restart / drop | a server-side drop is discarded by the pool `check`; the next call works — measured |
| nested `_tx` | **none** (AST check across 301 transaction-opening methods) — no self-deadlock |
| transaction boundaries | commit on success, rollback on any exception, always returned |
| prepared statements | off by default; policy preserved on every pooled connection |

### HTTP concurrency (`orchestration/web_server.py`)

| Aspect | Finding |
|---|---|
| `request_queue_size` | `NexusHTTPServer` sets 128; the A/B below proves it is the fix |
| peak load | p99 flat and bounded to 50 concurrent (measured) |
| slow clients | a per-connection thread; HTTP/1.0 closes each connection — see §11 |
| shutdown | `shutdown()` + `server_close()` + thread join |
| 503 / 500 | 503 for an unreachable database with no driver text (measured) |

### gzip (`_static`)

| Aspect | Finding |
|---|---|
| `Accept-Encoding` / `Content-Encoding` / `Vary` / `Content-Length` | correct (measured) |
| identity | a request without gzip gets the plain body (measured) |
| **`gzip;q=0`** | **defect found and fixed** — a refusal was ignored; now honoured |
| already-compressed (woff2) | not re-compressed (measured) |
| double compression | impossible: only `text/`, `application/javascript|json`, `svg`, `manifest+json` |

### File Laboratory (`orchestration/file_lab.py`)

| Aspect | Finding |
|---|---|
| `executemany` correctness | same rows as the per-member loop; `ON CONFLICT DO NOTHING` kept |
| transaction | one transaction; a failure rolls the whole extraction back (no partial workspace) |
| security | **unchanged** — independent probes below all refuse |

## 2. Archive safety — independent probes (VERIFIED)

Against the real `archive_safety.open_archive`:

| Input | Result |
|---|---|
| `../evil.txt` (zip-slip) | **REFUSED** "member escapes the destination" |
| `/etc/evil` (absolute) | **REFUSED** "absolute path" |
| zip symlink (S_IFLNK) | **REFUSED** "contains a symlink" |
| 2001 entries | **REFUSED** "limit is 2000" |
| depth > 16 | **REFUSED** "nested too deeply" |
| garbage bytes | **REFUSED** "not a valid zip" |
| tar symlink | **REFUSED** "contains a link" |
| a valid archive | **ACCEPTED**, members preserved |

The `executemany` change did not weaken any of these.

## 3. Reliability probes (VERIFIED)

| Scenario | Result |
|---|---|
| pool exhaustion | `StoreUnavailable` after the bounded timeout, then recovery |
| connection dropped while idle | next call works; the dropped connection is replaced |
| connection lost mid-transaction | that call fails; the store recovers |
| database unreachable | `StoreUnavailable` / HTTP 503, no driver text |
| transaction interrupted | rolled back on entry; the call succeeds |
| concurrent reads/writes | no errors; row count correct |
| close during use / double close | safe |
| worker outlives server | test passes |

## 4. Performance reproduction (MEASURED)

| Metric | Reported | Reproduced here |
|---|---:|---:|
| DB p99 @100 concurrent | ~35 ms | **42 ms** (pool) vs 713 ms (single conn) |
| API p99 @50 concurrent | ~81 ms | **85 ms**; with the old backlog **2725 ms** |
| API throughput @50 | ~697 ops/s | **691 ops/s** |
| Archive extract 1000 | 78 ms | **72 ms** |
| app.js over the wire | 52 KB | **52,146 B** (from 206,711 B) |

The **backlog A/B is decisive**: `request_queue_size=5` gives p99 2725 ms /
166 ops/s at 50 concurrent; `=128` gives 85 ms / 691 ops/s.

## 5. Defects found and fixed (this audit)

| # | Severity | Defect | Fix |
|---|---|---|---|
| 1 | P3 | `gzip;q=0` (explicit refusal) still got gzip | parse `Accept-Encoding`; honour `q=0` |
| 2 | P3 | dead `_reconnect_failed_at` / `RECONNECT_BACKOFF_SECONDS` (written, never read) | removed |

No P0/P1/P2 defect was found. Regression tests added for both fixes and for two
pool-hygiene properties (no leak on failure; exhaustion is bounded).

## 6. Not verified here (UNKNOWN / BLOCKED)

- Production p95/p99 under real load — BLOCKED (no paid Render metrics / OTel).
- Real OpenHands/LLM/GitHub latency — BLOCKED (no load on real services).
- HTTP/1.1 keep-alive and minification — assessed, not implemented (§11).
- A multi-hour soak beyond the run in [SOAK_TEST_RESULTS](./SOAK_TEST_RESULTS.md).
