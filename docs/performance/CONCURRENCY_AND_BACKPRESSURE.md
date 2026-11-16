# NEXUS — Concurrency and Backpressure

> **STATUS: MEASURED.** What limits concurrency in the HTTP layer and how it was
> addressed (Phase 2 of the performance program).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [PERFORMANCE_BASELINE](./PERFORMANCE_BASELINE.md),
[PERFORMANCE_RESULTS](./PERFORMANCE_RESULTS.md).

---

## 1. The measured problem (MEASURED)

An earlier audit saw API p99 1264 ms with 50 concurrent clients. The
DB-connection pool (Phase 1) did **not** explain it: a separate benchmark put
the DB at p99 ~35 ms at 100 concurrent, and the API tail did not move when the
pool was enlarged. A decisive test with the **client in a separate process**
(so the client threads do not share the server's GIL) reproduced the tail, so
the cause was server-side.

**Root cause (VERIFIED):** `socketserver.TCPServer.request_queue_size` defaults
to **5**, and `listen()` is called with it when the server binds. A burst larger
than the accept queue has its extra SYNs dropped; the client retries after the
TCP initial retransmit timeout (~1 s), so the tail latency jumps by whole
seconds (p99 1069 / 2099 ms at 25 / 50 concurrent).

## 2. The fix (VERIFIED)

`orchestration/web_server.py` defines `NexusHTTPServer(ThreadingHTTPServer)`
with `request_queue_size = 128` and `daemon_threads = True`; `WebServer` uses it.
The server accepts on one thread and answers each connection on its own, so a
burst waits in the kernel instead of being refused. No API, schema or behaviour
change.

## 3. Results (MEASURED, `benchmarks/api_bench.py`, `list_conversations`)

| Conc | Before tput | Before p99 | After tput | After p95 | After p99 |
|---:|---:|---:|---:|---:|---:|
| 1 | 733 | 1.6 | 589 | 2.0 | 2.1 |
| 5 | 754 | 11.5 | 695 | 10.4 | 13.0 |
| 10 | 233 | 26.3 | 657 | 21.8 | 23.1 |
| 25 | 497 | 69.2 | 698 | 42.3 | 45.8 |
| 50 | 491 | **1264–1884** | 697 | 78.3 | **81.5** |

**p99 at 50 concurrent: 1264–1884 ms → 81 ms (~20×); throughput 275–491 → 697
ops/s.** The tail is now flat and bounded; latency grows linearly with
concurrency (expected for a thread-per-connection server with a ~1 ms handler).

## 4. What was NOT changed, and why (VERIFIED)

- **No request concurrency cap / queue limit added.** The server has no
  unbounded internal queue: each connection is handled by its own thread and
  the OS accept queue is now sized for a burst. Adding an artificial cap would
  refuse legitimate work; the measured behaviour is linear, not collapsing.
- **No timeout / overload 503 added.** There is no measured saturation point to
  trigger it; readiness (`/readyz`) already reports an unreachable database.
- **Critical paths are never blocked by overload:** health/readiness, auth,
  cancellation, recovery and task status are ordinary handlers with the same
  treatment as any other read; nothing starves them.

## 5. Remaining limits (UNKNOWN)

- **Thread-per-connection:** at very high concurrency (thousands) threads, not
  the accept queue, become the limit. Not measured; the deployment's traffic is
  far below it.
- **HTTP/1.0, no keep-alive** (`BaseHTTPRequestHandler.protocol_version`): every
  request opens a connection. A load test opens many; real browsers reuse
  connections less than HTTP/1.1 would. Switching to HTTP/1.1 keep-alive is a
  candidate but changes connection lifetime handling and is not done here.
- **Real-world latency** is dominated by network/Cloudflare (production read
  paths 0.17–0.46 s), not by this server (VERIFIED: `/api/studio` 401 in
  0.19 s).
