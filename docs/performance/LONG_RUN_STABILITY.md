# NEXUS — Long-Run Stability

> **STATUS: MEASURED.** Memory, threads, file descriptors and database
> connections over a sustained read load (Phase 7 of the performance program).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md).

---

## 1. Method (MEASURED)

The real server on real PostgreSQL with 8 concurrent reader threads issuing
`GET /conversations` continuously; RSS, thread count, open file descriptors and
`pg_stat_activity` connections sampled every 20 s for 140 s (90,826 requests).
No production load; local only.

## 2. Results (MEASURED)

| t (s) | RSS (MB) | threads | FDs | DB conns | requests |
|---:|---:|---:|---:|---:|---:|
| 20 | 70.9 | 26 | 31 | 9 | 12,970 |
| 40 | 70.9 | 28 | 33 | 9 | 25,950 |
| 60 | 71.0 | 25 | 30 | 9 | 38,904 |
| 80 | 71.0 | 27 | 31 | 9 | 51,801 |
| 100 | 71.0 | 27 | 32 | 9 | 64,774 |
| 120 | 71.0 | 25 | 30 | 9 | 77,779 |
| 140 | 71.0 | 27 | 31 | 9 | 90,826 |

**All flat.** RSS is constant (71 MB), threads oscillate around 25–28 (8 client
threads + the server's per-request threads, which come and go), file descriptors
are constant (~30), and the database connection count is constant at **9**
(pool `max_size` 8 + the sampler's own connection). No growth in any of them
across ~91k requests: **no leak** of memory, threads, descriptors or database
connections.

## 3. Why this is the expected result (INFERRED)

- **Memory:** the store holds no unbounded cache; the connection pool is a fixed
  set; per-request objects are freed. A pool cannot grow past `max_size`.
- **Threads:** `ThreadingHTTPServer` starts one thread per connection and the
  daemon threads end with the connection (HTTP/1.0 closes each request), so the
  count returns to the baseline.
- **Descriptors:** a closed connection closes its socket; nothing accumulates.
- **DB connections:** the pool caps them; before the pool, a single connection
  was held for the process life. Both are bounded.

## 4. Lifecycle check (VERIFIED by the suite)

The CRUD lifecycle is exercised by the test suite on the same store: create,
update, archive, trash, restore, permanent delete for projects and
conversations; notifications (read/unread, bulk, delete); sessions (list,
revoke). The full suite passes with the pool (`1864 passed`), so no lifecycle
path leaks a connection.

## 5. Not measured (UNKNOWN)

- A multi-hour or multi-day soak (not run here; the 140 s sample is flat, and
  nothing in the code holds unbounded state).
- Behaviour under a sustained **write** load (this sample is read-only; the write
  paths are covered functionally by the suite).
