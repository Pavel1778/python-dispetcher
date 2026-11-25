# RFC-001: Event-driven control plane

| | |
|---|---|
| **Status** | Accepted (incremental) |
| **Author** | NEXUS engineering |
| **Date** | 2026-11-25 |
| **Scope** | Phase 1 of the NEXUS 2.1 performance programme |
| **Supersedes** | nothing |
| **Related** | `docs/PERFORMANCE.md`, `docs/ARCHITECTURE.md`, `docs/DURABLE-RUNS.md`, `AGENTS.md` |

## 1. Problem

NEXUS is a control plane for autonomous agent work. Two loops advance that
work:

* the web server's inline loop (`App._run_lab`), and
* the standalone worker (`python -m orchestration.lab_worker`).

Both loops are **polling** loops. A pass runs, then the process waits
`poll_interval` (default 5 s) before the next pass:

```python
# orchestration/lab_worker.py, run_forever
summary = self.tick_once()
...
self.wait(self.poll_interval)          # Event.wait(timeout) - a poll
```

The consequences are measurable:

1. **A new run waits up to one poll interval** before its first task is
   claimed. On the live deployment a start already takes ~45–60 s; adding up
   to 5 s of pure waiting is avoidable latency on the critical path.
2. **The worker wakes even when there is nothing to do**, once per interval,
   which is one PostgreSQL round trip per interval per worker.
3. Every wake is a `SELECT` over the active runs. That is cheap, but it is
   load that carries no information when nothing changed.

The requirement asks for a p99 read latency below 50 ms and 100+ concurrent
agents. Polling is not the cause of the read latency (the read paths are
already single-digit milliseconds — see `docs/PERFORMANCE.md`), but it is the
cause of avoidable *dispatch* latency and of idle database traffic, and it is
the piece that will not scale to many workers polling one database.

## 2. Goals and non-goals

### Goals

* A run that is started, resumed or retried is picked up **within tens of
  milliseconds**, not up to five seconds.
* An idle worker performs **no periodic query** while nothing changes.
* No new required dependency for the default single-process deployment.
* The correctness model does not change: leases, fencing tokens and the state
  machine stay in PostgreSQL. A wake-up is a *hint*, never a source of truth.
* Backwards compatible: a deployment that sets nothing keeps working, with
  polling as the backstop.

### Non-goals (this RFC)

* Replacing PostgreSQL as the system of record.
* Moving task state into Redis. Redis is a cache and a bus, never the
  authority.
* Changing how OpenHands conversations are driven.

## 3. Design

### 3.1 PostgreSQL LISTEN/NOTIFY (the first increment)

PostgreSQL already ships the primitive. No new service is needed for the
default deployment.

```
        POST /goals/{id}/run
                │
                ▼
   LabStore.start_run()  ──►  INSERT run + tasks (one transaction)
                │
                ▼
   SELECT pg_notify('nexus_work', '<run_id>')   (same transaction, on commit)
                │
        ┌───────┴────────┐
        ▼                ▼
  LabWorker          App._run_lab
  LISTEN nexus_work  LISTEN nexus_work
        │                │
        └────► Event.set() ◄────┘
                │
                ▼
          tick_once()  (immediately)
```

* **Channel:** `nexus_work`.
* **Payload:** a small JSON object `{"kind": "run", "id": "<uuid>",
  "reason": "started"}`. The payload is a hint only; the worker still reads
  the run from the database.
* **Producer:** `orchestration/nexus_notify.notify_new_work(store, kind, id,
  reason)`. It runs `SELECT pg_notify(...)` inside the caller's transaction so
  the notification is delivered **only if the transaction commits**. A
  rolled-back start notifies nobody.
* **Consumer:** `orchestration/nexus_notify.NotificationListener`. It opens a
  **dedicated** connection (LISTEN cannot share a pooled connection, because
  the session must stay subscribed), runs `LISTEN nexus_work`, and blocks on
  `conn.notifies(timeout=...)`. On a notification it calls the callback (the
  worker's `wake()`).
* **Backstop:** the poll interval remains. A notification can be missed (a
  connection drop, a restart between commit and LISTEN); the next poll
  recovers. **A missed notification must never lose work** — this is why the
  database stays the source of truth.

Why a dedicated connection and not the pool: a `LISTEN` subscription lives on
the session. A transaction-mode pooler (PgBouncer) hands a different backend
to the next transaction, so `LISTEN` there is unreliable. The listener
therefore opens its own direct connection, and the RFC treats the listener as
**best effort**: if it cannot connect, the loop polls as before and logs once.

### 3.2 Redis (second increment, optional)

Redis is introduced only where it earns its place:

| Use | Why Redis | Fallback when absent |
|---|---|---|
| Cache `/api/config`, `/api/studio`, account lists | read-mostly, changes rarely | read PostgreSQL as today |
| Cross-process event fan-out (WebSocket) | pub/sub across instances | in-process fan-out |
| Rate-limit counters | shared across instances | `_LoginThrottle` in memory |

Rules:

* **Cache keys are versioned and invalidated on write.** A cached account
  list is dropped when an account is added, edited or removed.
* **No secret is ever a cache value.** Cached payloads are the same
  key-free views the API already returns.
* **The cache is never authoritative.** A miss falls through to PostgreSQL.
* A deployment with no `REDIS_URL` behaves exactly as today.

### 3.3 PgBouncer (deployment concern, not code)

A transaction-mode pooler in front of PostgreSQL raises the connection
ceiling without changing the code. The two constraints already documented in
`AGENTS.md` apply:

* server-side prepared statements stay off (`prepare_threshold=None`);
* migrations use a **session-mode** connection (`MIGRATE_DSN`), because DDL
  and `LISTEN` need a stable backend.

### 3.4 Binary data out of PostgreSQL (later increment)

Files, archives and voice notes currently live in `bytea` columns. Moving
them to S3-compatible storage (MinIO) keeps the metadata row and stores an
object key. This is **not** part of this RFC's implementation; it is listed
because it changes the same read paths the benchmark measures. It must be an
additive migration with a dual-read path (row bytes first, object key second)
until the backfill is verified.

## 4. What was implemented in this increment

To keep the change reviewable and reversible, this RFC's first increment is
deliberately small:

1. `orchestration/nexus_notify.py` — the producer and the listener, with a
   real LISTEN connection and an honest "unavailable" state.
2. `LabStore.start_run` and the run-action routes emit `pg_notify` in the
   transaction that creates or re-queues work.
3. `LabWorker.run_forever` and `App._run_lab` start a listener that calls
   `wake()`; the poll interval stays as the backstop.
4. Tests: a real PostgreSQL LISTEN round trip, a missed-notification
   backstop, a listener that cannot connect, and an HTTP-level test that a
   started run wakes a listener.

Redis, PgBouncer and MinIO are **not** implemented here. They are listed as
the next increments with their acceptance criteria below.

## 5. Acceptance criteria

| # | Criterion | How it is proven |
|---|---|---|
| 1 | A `pg_notify` reaches a listening session | `tests/test_notify.py`, real PostgreSQL |
| 2 | A rolled-back transaction notifies nobody | `tests/test_notify.py` |
| 3 | A started run wakes a listener | `tests/test_notify.py` over HTTP |
| 4 | A missed notification still advances the run | existing worker tests + backstop case |
| 5 | A listener that cannot connect does not stop the loop | `tests/test_notify.py` |
| 6 | No behaviour change when the listener is off | full suite green |

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A notification is lost | The poll backstop; the database is authoritative |
| `LISTEN` through a pooler is unreliable | Dedicated connection; listener is optional |
| A listener thread leaks on shutdown | `stop()` joins with a timeout; daemon thread |
| Notification storms under load | Payload is a hint; the worker coalesces (an `Event`), and one pass services every active run |
| A new dependency creeps in | The first increment uses only `psycopg`, already required |

## 7. Rollout

1. Ship the module and the producer with the listener **off** by default.
2. Enable the listener (`NEXUS_NOTIFY=1`) in staging; compare dispatch
   latency in `tools/benchmark.py`.
3. Enable in production; watch `nexus_lab_workers` heartbeats and the
   `notify` counters in `/metrics`.
4. Only then consider Redis for cross-instance fan-out.

Each step is reversible by unsetting the variable: the loop returns to pure
polling.
