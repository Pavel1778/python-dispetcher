# Architecture

NEXUS is a durable control plane. It owns task state, leases, assignment and
recovery; it does not own the runtime. Conversations execute in OpenHands
(either the Cloud V1 API or, by default, a sandbox's own agent-server).

```
                 ┌──────────────────────────── PostgreSQL ───────────────────────────┐
                 │ nexus_projects  nexus_conversations  nexus_goals  nexus_specs    │
                 │ nexus_runs      nexus_run_tasks      nexus_task_executions       │
                 │ nexus_evidence  nexus_lab_workers    nexus_conversation_cleanups│
                 └──────────────────────────────────────────────────────────────────┘
   browser (studio)                    ▲                     ▲
        │  HTTP /api/v1/...             │                     │
        ▼                               │                     │
   web_server.App ──────────────────────┘                     │
        │  inline lab loop (default)                          │  lab_worker process
        ▼                                                     ▼
   LabScheduler.tick  ◀──────────── claim_task (FOR UPDATE SKIP LOCKED, lease) ────┐
        │                                                                          │
        ├─ authorize_run_task  (project ↔ repository link, re-checked each start)  │
        ├─ OpenHandsExecutor.dispatch  ──▶ OpenHands (start conversation)          │
        ├─ OpenHandsExecutor.poll      ──▶ OpenHands (read the conversation)       │
        ├─ GitVerifier.verify          ──▶ GitHub (read the branch head)           │
        └─ set_run_state_internal / report ──▶ the chat the run was asked for in ───┘
```

## The pieces

- **`control_plane.py`** — the legacy SQLite task store (workflows/tasks/Sr/Jr).
- **`cloud_store.py`** — `PostgresStore`: one connection for the process, the
  reconnect-and-backoff logic, `verify_schema`, and the legacy workflow/task
  tables.
- **`nexus_projects.py`** — projects, membership, RBAC scopes, goals, and the
  project/conversation lifecycle (archive, trash, restore, purge).
- **`nexus_conversations.py`** — persistent conversations, messages, attachments,
  agent sessions; project-isolated.
- **`nexus_lab_store.py`** — runs, run tasks, executions, notifications, worker
  heartbeats. Holds the invariants: atomic claim with a lease and a fencing
  token, dependency gating (`completed_verified`/`completed_no_changes`), branch
  occupancy (`_BRANCH_FREE`), and the run state machine (`_RUN_ENTERED_FROM`).
- **`nexus_lab.py`** — `OpenHandsExecutor` (dispatch + poll; never decides
  completion), `GitVerifier` (checks the *real* branch), `LabScheduler` (one tick
  = reclaim → follow starts → stop abandoned → poll → verify → claim → authorize
  → dispatch → decide). This is the only place that moves a run forward.
- **`lab_worker.py`** — the loop that advances runs. It holds no per-run state,
  so a restart is a non-event. It runs as a separate process
  (`python -m orchestration.lab_worker`) and/or inside the web server
  (`NEXUS_LAB_INLINE`, on by default).
- **`nexus_binding.py`** — the project↔repository link and the authorization
  gates (`authorize_dispatch` for workflows, `authorize_run_task` for runs).
- **`openhands_router.py` / `openhands_pool.py`** — which OpenHands account runs
  a task, and the failover hand-off when one fails.
- **`nexus_evidence.py` / `evidence_trust.py`** — the Evidence Trust Engine: an
  Ed25519 signature over a canonical encoding, so a verdict is earned.
- **`nexus_reports.py`** — the run's report, written in the transaction that
  completes the run and delivered to the chat once.
- **`tracing.py`** — one secret-free span per stage (dispatch, poll, verify).
- **`web_server.py`** — the HTTP API and the studio's static files.
- **`studio/`** — the front end: one chat application, no framework, no build.

## The execution state machine

A run task moves through:

```
pending → dispatched → running → verifying → completed_verified
                                            → completed_no_changes
                                            → completed_unverified → (retry)
                                            → partial / failed / blocked
```

`completed_verified` and `completed_no_changes` are the two accepted states; only
they unblock a dependent task and let a run complete. A `durable` run reworks a
task that did not verify (in the same conversation, then in a new one); a
`bounded` run stops when its attempts or budgets are spent. The transitions are
atomic (one `UPDATE ... RETURNING`) and fenced by the task's `lease_token`.

## Multi-worker, multi-instance

Every claim is a lease in PostgreSQL, so N workers claim N distinct tasks rather
than one worker locking the queue. A worker that dies is recovered when its
lease expires (`reclaim_expired`), and a conversation it started is followed by
whichever worker ticks next (the start is on the task's `pending_start`). Two
workers never start a second conversation for one task: the binding is
idempotent (`mark_running` is a no-op for the conversation the task already has)
and the branch is held (`_BRANCH_FREE`).

See [DURABLE-RUNS.md](DURABLE-RUNS.md) for the durable-run rules and
[OBSERVABILITY.md](OBSERVABILITY.md) for how to tell a live worker from a dead
one.
