# Worker setup

The autonomous lab advances runs in a loop of its own: it claims tasks,
dispatches them, polls, verifies and decides. A browser request never drives a
run, and closing the browser never stops one. The loop runs in two places:

- **inside the web server**, on a thread, by default - so a deployment that is
  one web process still runs what it accepts;
- **as a separate process**, `lab_worker`, for a deployment that can run one.

Both may run at once: every claim is a lease in PostgreSQL. `NEXUS_LAB_INLINE=0`
on the web server leaves the work to the separate process alone.

## Run the worker

```bash
# A long-lived worker (recommended):
python -m orchestration.lab_worker --worker-id lab-1

# One pass, then exit (useful for a cron or a health check):
python -m orchestration.lab_worker --once
```

The worker reads the same `SUPABASE_DB_URL` (or `NEXUS_DB_URL`) as the web
server and uses the same migrations. It holds no per-run state in memory: a
restart is a non-event. It re-reads the active runs and continues.

Run it beside the web server. More than one worker is fine — claims are leases
(`SELECT ... FOR UPDATE SKIP LOCKED`), so extra workers only take more work.

## Worker heartbeat

The worker registers itself and writes a heartbeat every tick
(`nexus_lab_workers`). This is how the diagnostics can tell a **live** worker
from a **dead** one: a lease alone cannot, because a task with no lease is just
"nothing claimed right now", which is also true when no worker exists.

- A worker whose heartbeat is older than the diagnostics' `worker_max_age`
  (default 120 s) reads as not live.
- `GET /api/openhands/diagnostics` reports `lab_worker`:
  - `VERIFIED` — a live worker exists;
  - `FAILED` — active runs exist but no worker heartbeat is fresh (this is the
    defect the heartbeat exposes);
  - `UNVERIFIED` — no live worker, but no active run needs one.
- A graceful stop (`SIGTERM`/`SIGINT`) marks the worker `stopped`.

## Process supervision

Pick one:

**Docker Compose** — `docker-compose.yml` defines a `worker` service beside the
`app` and `db` services. It shares the database and the `.env` file, so the two
processes agree on configuration.

**Render** — `render.yaml` defines a `worker` service
(`python-dispetcher-lab-worker`) beside the web service. Both run
`python -m orchestration.lab_worker` / `web_server` against the same database.

**systemd** — a ready unit ships in the repository
(`packaging/systemd/nexus-lab-worker.service`). Install it on a host that runs
the web server and shares its PostgreSQL:

```bash
sudo install -m 0644 packaging/systemd/nexus-lab-worker.service \
    /etc/systemd/system/nexus-lab-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-lab-worker
systemctl status nexus-lab-worker
journalctl -u nexus-lab-worker -f
```

It reads the same `EnvironmentFile` as the web server (so both agree on
`SUPABASE_DB_URL`, `OPENHANDS_*`, `GITHUB_TOKEN`, `NEXUS_SECRET_KEY`), runs
`python -m orchestration.lab_worker --worker-id systemd-1`, restarts on failure
(`Restart=always`) and stops gracefully on `SIGTERM`. Adjust `WorkingDirectory`
and the interpreter path in the unit to the deployment.

## The web server's own loop

Unless `NEXUS_LAB_INLINE` is `0` (or `false` / `no` / `off`), the web server
runs the same loop on a thread. It registers a heartbeat as kind `inline`, so
the diagnostics see which kind of loop is alive, and `GET /api/studio` reports
it as `lab.inline`. It lives and dies with the web process: on a host that
stops an idle process it stops too, and a separate worker is what keeps runs
moving then.

## Recovery

A worker that dies between claiming a task and starting its conversation is
recovered when the lease expires (`reclaim_expired`): the task is requeued, or
marked `failed` if it has exhausted its attempts. A task whose conversation was
already started is simply followed by whichever worker ticks next — polling is
stateless. Nothing is dispatched twice.

The loop also outlives the database. A pass that fails because the store is
briefly unreachable (a database restart, a pooler recycling its clients, a
network reset) is logged and retried on the next pass — the worker process does
not exit, and registration is retried until it sticks. Without that, one
transient error would kill the only thing advancing the runs.

`tests/test_lab_store.py` covers lease reclaim, attempt budgets and the
heartbeat. `tests/test_lab_loop.py` covers the full tick ordering.
`tests/test_planner_worker.py` covers the loop surviving a failing run and a
transient database error. `tests/test_lab_worker_process.py` starts the real
`python -m orchestration.lab_worker` child process, against real PostgreSQL and
the fake OpenHands Cloud/GitHub, and proves it registers, heartbeats, claims,
dispatches, survives a hard kill and continues with no client connected.
