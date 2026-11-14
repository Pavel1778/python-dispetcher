# Observability and diagnostics

`GET /api/observability` (protected) answers "what is the execution path doing
right now, and how slow is each stage?" with counts, durations and booleans
only. It never returns a secret and never a project's content. It is read during
an outage, so everything that can fail is reported as an `error` field rather
than a 500.

The studio shows the same data under **Настройки → Диагностика**.

## The shape

```jsonc
{
  "queue": {
    "tasks_by_state": {"pending": 2, "running": 1, ...},
    "pending": 3,              // pending + waiting
    "running": 1,              // dispatched + running + verifying
    "failed": 0,               // failed + blocked
    "retrying": 0,             // waiting
    "claimable_now": 2,        // due, no live lease, wait over
    "oldest_claimable_seconds": 12,
    "active_runs": 1
  },
  "stuck_tasks": [
    {"task_id": "…", "run_id": "…", "project_id": "…", "task_key": "t1",
     "state": "pending", "run_state": "running", "attempt": 0, "max_attempts": 3,
     "age_seconds": 640, "reason": "…"}
  ],
  "workers": {"count": 1, "live": [{"worker_id": "lab-1", "kind": "worker",
                                    "age_seconds": 3, "last_error": null}]},
  "last_tick": {"at": 1731500000.0, "advanced": 1, "errors": []},
  "latency": {"enabled": true, "spans": 412, "stages": {
    "dispatch": {"count": 8, "p50_ms": 31000, "p95_ms": 48000, "max_ms": 51000, "errors": 0},
    "poll": {"count": 380, "p50_ms": 90, "p95_ms": 400, "max_ms": 2100, "errors": 0},
    "verify": {"count": 8, "p50_ms": 220, "p95_ms": 600, "max_ms": 900, "errors": 0}
  }},
  "openhands_reachable": true
}
```

## Reading it

- **"run running, task pending, attempt 0/3, executor never started"** - this is
  the symptom the request names. Look at `queue.claimable_now` and
  `stuck_tasks`. If `claimable_now > 0` and `workers.count == 0`, nobody is
  ticking: start `python -m orchestration.lab_worker` (or set
  `NEXUS_LAB_INLINE=1`). If `workers.count > 0` and tasks are still stuck, the
  worker is alive but not claiming: read `stuck_tasks[].reason` and
  `workers.live[].last_error`.
- **A dead worker** is told from a live one by its heartbeat
  (`nexus_lab_workers.heartbeat_at`), not by a lease: a lease cannot tell idle
  from dead. The web server's own inline loop registers too, as kind `inline`.
- **Latency**: `dispatch` is the two-phase OpenHands start (tens of seconds -
  see [PERFORMANCE.md](PERFORMANCE.md)); `poll` is one look at a conversation;
  `verify` is the GitHub read. Enable collection with `NEXUS_TRACE=1`; without
  it the tracer is a no-op and `latency.stages` is empty.

## The full diagnostics report

`GET /api/openhands/diagnostics` (and the CLI `nexus openhands diagnostics`) is
the deeper, earned report: account health, the pool, Cloud reachability, the
queue/lease round trip, lease recovery, the provider cascade, the tool catalog,
MCP configuration and the worker. Each check is `VERIFIED` / `FAILED` /
`BLOCKED` / `UNVERIFIED`, and a status is earned by a real call, never assumed.
See [OPENHANDS_SETUP.md](OPENHANDS_SETUP.md).

## No secrets

Neither endpoint returns a credential. `error` strings carry a type name and a
bounded message; the tracer's `_safe_error` keeps at most 300 characters and
reads no environment value. `tests/test_observability.py` asserts that a
credential-looking value does not appear in the answer.
