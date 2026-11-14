# Performance

This document is about *where the time goes* in the execution path and how to
measure it. It does not claim an improvement that was not measured; where a
number cannot be produced in this environment, it says so.

## The path

```
User request
  → Project selection
  → Conversation (chat turn)
  → Planner (provider cascade)         [one model call per turn/plan]
  → Task decomposition
  → Queue (nexus_run_tasks, claim_task)
  → Dispatcher / LabScheduler.tick
  → OpenHands runtime (start_conversation, two-phase start)
  → Workspace (sandbox checkout)
  → Agent execution (the model's own work)
  → Verification (GitVerifier: a GitHub read)
  → Completion (set_run_state_internal + report)
  → Notification (the report in the chat)
```

Each stage that NEXUS itself controls is timed (`orchestration/tracing.py`): a
**span** with `started_at`, `finished_at`, `duration_ms`, and the
`execution_id` / `project_id` / `task_id` / `conversation_id` / `attempt_id` it
belongs to, plus a `status` and a secret-free `error`.

## What was actually slow

The audit found the cost was not in the SQL or the HTTP layer - those are
sub-millisecond to a few milliseconds (see the benchmark below). The real
latency was in three places:

1. **A start is waited for in one process, serially.** `POST
   /api/v1/app-conversations` returns a start task; the conversation id appears
   only at `READY`, about 30-50 s on the live service. The worker waits for it
   with a bounded poll (`start_poll_attempts * poll_interval`, five minutes).
   When the process is replaced mid-wait (a deploy, a `SIGTERM`), the start was
   recorded on the task (`pending_start`) so the *next* worker finishes it
   instead of waiting again (`_follow_starts`). This is the single largest
   wall-clock cost per task and it is inherent to the OpenHands two-phase
   start, not to NEXUS.
2. **Polling intervals.** The lab worker ticks every
   `poll_interval` (5 s by default); the studio polls a moving run every 5 s.
   Neither is a spin: the worker does one pass and sleeps, and the studio polls
   only while a run is actually moving and the tab is visible
   (`scheduleWorkPoll`). A stuck *claim* is therefore bounded by one tick, not
   by a busy loop.
3. **A task that was due but unclaimed** - the "running + pending" symptom - is
   not a NEXUS sleep but a missing or dead worker. It is now *visible*:
   `queue_snapshot().claimable_now` and `stuck_tasks()` in
   `GET /api/observability` (see [OBSERVABILITY.md](OBSERVABILITY.md)).

What was **not** found, having looked: no N+1 query in the loop (the per-run
queries are constant), no missing index on the claim path (the partial unique
index `uq_tasks_workspace_active` and the run/task indexes are used), no
unnecessary sequential HTTP in the scheduler (each conversation is polled once
per tick, and only tasks that are in flight).

## Measuring

```
TEST_SUPABASE_DB_URL=postgresql://… python tools/benchmark.py
```

prints JSON: the queue claim latency (`queue_claim_ms`) and the queue wait
(`queue_wait_ms`), per p50/p95/p99/max, over real PostgreSQL. Stages that need
a live OpenHands key or a real GitHub token are listed under `not_measured`
with the reason - never invented.

### Baseline (this environment, PostgreSQL 17, loopback)

Measured with `tools/benchmark.py` on 2026-11-14, before and after this change
set; the claim path was not altered by it, so the two are the same within noise:

| metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| queue claim (due → claimed) | 4.45 ms | 5.11 ms | 6.96 ms | 7.43 ms |

The dispatch, poll and verify stages are timed live by `orchestration/tracing.py`
and reported at `GET /api/observability` under `latency.stages` (p50/p95/max per
stage). Their numbers depend on the OpenHands service and GitHub, so they are
read from a real deployment rather than fixed here.

## Targets kept

- No unjustified wait: the worker sleeps between passes and does not spin; the
  studio polls only a moving run in a visible tab.
- Queue latency is bounded by one worker tick, and a task that is due is
  reported as such rather than silently pending.
- Retry never loops forever: exponential backoff with a deterministic spread
  (`nexus_retry`), a per-account circuit breaker (a `CloudAuthError` blocks at
  once), and a refusal (429) that is never counted as an attempt.
- Scaling is by workers, not by a bigger process: every claim is a lease in
  PostgreSQL, so N workers claim N distinct tasks
  (`FOR UPDATE SKIP LOCKED`), and a worker that dies is recovered when its
  lease expires.

## Environment variables

- `NEXUS_TRACE=1` - collect spans (the `/api/observability` latency table).
- `NEXUS_TRACE_LOG=1` - also emit one JSON line per span to the `nexus.trace`
  logger.
- `NEXUS_LAB_MAX_ACTIVE_TASKS` - a global ceiling on tasks at work at once.
