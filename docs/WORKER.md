# The lab worker

Autonomous runs are advanced by one loop, `orchestration.lab_worker.LabWorker`.
It runs in two places and either is enough:

1. **Inside the web server** (`App._run_lab`, a thread). This is the default:
   `NEXUS_LAB_INLINE` unset or `1`. It is why a one-process deployment (the
   Render blueprint) advances a run with nothing else running.
2. **As a separate process**: `python -m orchestration.lab_worker`. Set
   `NEXUS_LAB_INLINE=0` on the web service if you want the worker to be the only
   actor. `render.yaml`, `docker-compose.yml` and
   `packaging/systemd/nexus-lab-worker.service` all launch it this way.

Both may run at once. Every claim is a lease in PostgreSQL, so the two do not
step on each other.

## What one pass does

`tick_once()`:

1. For each active run (`running`, `paused`, `waiting_for_approval`, `blocked`),
   tick the scheduler: reclaim expired leases, follow recorded starts, stop
   abandoned conversations, poll what is in flight, claim and dispatch the next
   claimable task, decide the run's fate.
2. Then, whatever the runs are doing: deliver stop signals, deliver owed reports,
   re-probe connections written off, and (periodically) delete the OpenHands
   conversations of ended runs.
3. Write the heartbeat, even when nothing advanced.

The worker holds **no per-run state in memory**, so a restart is a non-event: it
re-reads the active runs and continues. This is the property the recovery tests
assert.

## Why it is reliable

- **Atomic claim.** `claim_task` uses `FOR UPDATE SKIP LOCKED` on one row at a
  time plus an advisory lock on the branch, so N workers claim N distinct tasks.
- **Lease and heartbeat.** A claim takes a lease; the worker heartbeats every
  tick into `nexus_lab_workers`. The diagnostics tell a live worker from a dead
  one by the heartbeat, not by a lease (a lease cannot tell idle from dead).
- **Recovery.** A worker that dies between claiming and starting is recovered
  when the lease expires (`reclaim_expired`), and the conversation it may have
  started is adopted by its tags rather than duplicated.
- **A start outlives the worker.** A start that the service accepted but that the
  worker did not finish is on the task's `pending_start`, and the next worker
  binds the conversation (`_follow_starts`). A deploy mid-start loses nothing.
- **It outlives the database.** A transient `StoreUnavailable` is logged and
  retried on the next pass; it does not end the process.
- **Backpressure.** `NEXUS_LAB_MAX_ACTIVE_TASKS` is a global ceiling on tasks at
  work at once; a claim refuses when it is reached.

## Stopping

`SIGTERM`/`SIGINT` set the stop flag. A stopping worker claims nothing new,
begins no further run, and raises `StartLeft` instead of waiting for a recorded
start (the next process finishes it). The web server waits `LAB_STOP_GRACE`
(20 s) for its inline loop before closing the store.

## If a run is not advancing

Read `GET /api/observability`:

- `queue.claimable_now > 0` and `workers.count == 0` → nobody is ticking. Start
  the worker, or set `NEXUS_LAB_INLINE=1`.
- `workers.count > 0` but tasks are stuck → read `stuck_tasks[].reason` and
  `workers.live[].last_error`.

See [OBSERVABILITY.md](OBSERVABILITY.md) and
[WORKER_SETUP.md](WORKER_SETUP.md).
