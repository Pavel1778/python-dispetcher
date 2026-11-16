# NEXUS — Execution Engine Specification

> **STATUS: SPECIFICATION.** The Execution Engine is the highest-risk domain.
> This document specifies it from the Python code as it exists today
> (**VERIFIED** against `nexus_lab.py`, `nexus_lab_store.py`, `nexus_specs.py`)
> and states the target behaviour for the Rust port (**INFERRED** where it
> extends current behaviour).
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md) §6,
[API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md).

---

## 1. Entities (separated, not conflated)

| Entity | Meaning | Python today | Table |
|---|---|---|---|
| **Project** | the workspace; owns repositories, membership | `nexus_projects` | `nexus_projects` |
| **Conversation** | a chat thread in a project | `nexus_conversations` | `nexus_conversations` |
| **Task** | one unit of work in a run | `nexus_run_tasks` | `nexus_run_tasks` |
| **Run** | one autonomous goal execution (a DAG of tasks) | `nexus_runs` | `nexus_runs` |
| **Agent** | the actor that executes a task | the OpenHands conversation | `conversation_routes` |
| **Execution attempt** | one try of one task | `attempt` on the task | `nexus_task_executions` |
| **External OpenHands conversation** | the Cloud object the agent runs in | `conversation_id` | `cloud_sandboxes`, `conversation_routes` |
| **Evidence** | the signed record of a verification | `nexus_evidence` | `nexus_evidence` |
| **Artifact** | a produced file / push / report | `nexus_run_reports`, pushes | `nexus_run_reports` |

**Critical separation (VERIFIED in the Python code):** the executor "never
decides completion"; `GitVerifier` does, and `LabStore` is the only writer of
terminal state. The Rust design must keep this.

---

## 2. State machines

### 2.1 Run states (VERIFIED — `_RUN_ENTERED_FROM`)

States: `running`, `paused`, `waiting_for_approval`, `blocked`, `completed`,
`failed`, `cancelled`.

Transitions (from → to):

| To | From |
|---|---|
| running | paused, blocked |
| paused | running |
| waiting_for_approval | running |
| blocked | running |
| completed | running |
| failed | running |
| cancelled | running, paused, waiting_for_approval, blocked |

`ACTIVE_RUN_STATES = {running, paused, waiting_for_approval, blocked}`;
`TERMINAL_RUN_STATES = {completed, failed, cancelled}`.

### 2.2 Task states (VERIFIED)

Terminal: `completed_verified`, `completed_unverified`, `partial`, `failed`,
`blocked`. Unfinished: `pending`, `waiting`, `dispatched`, `running`,
`verifying`. In-flight: `dispatched`, `running`, `verifying`. Only
`completed_verified` unblocks a child (dependency gating).

### 2.3 Target state machine (INFERRED — explicit in Rust)

The Rust `TaskState`/`RunState` enums encode the same transitions as
type-checked methods (`fn to_running(self) -> Result<Self, TransitionError>`),
so an illegal transition is a compile-time or a returned error, not a silent
row write.

---

## 3. The loop (VERIFIED)

`LabStore.pass()` / `lab_worker` order: **reclaim → claim → dispatch → poll →
verify → record → decide.** Claiming uses `FOR UPDATE SKIP LOCKED` with a
lease and a fencing token; `pg_advisory_xact_lock('nexus-active-tasks')` guards
`NEXUS_LAB_MAX_ACTIVE_TASKS` (default 8).

---

## 4. Required behaviours (the 20 aspects)

1. **Idempotency (VERIFIED).** `nexus_specs` dedups by
   `(conversation_id, sha256(text))` and `idempotency_key`; a resubmitted
   identical message never creates a second run.
2. **Lease acquisition (VERIFIED).** `claim_task` takes a lease
   (default 900 s) and a fencing token.
3. **Lease expiration (VERIFIED).** `reclaim_expired` returns expired tasks to
   the pool; a stale owner cannot write (fencing).
4. **Concurrent scheduling (VERIFIED).** `FOR UPDATE SKIP LOCKED` +
   active-task limit; multiple workers are safe.
5. **Duplicate requests (VERIFIED).** Per-text idempotency (1); a distinct
   message legitimately creates its own run (the concurrency contract).
6. **Cancellation (VERIFIED, with a limit).** Cancelling a run does **not**
   stop an already-running Cloud conversation; the executor records a stop
   request (`nexus_executor_stops`) and the worker observes the stop.
7. **Deadlines (VERIFIED).** `NEXUS_TASK_TIMEOUT_SECONDS`,
   `NEXUS_TASK_STALL_SECONDS`; a timed-out task is `blocked`/retried.
8. **Retries (VERIFIED).** `_RETRYABLE_TASK_STATES`; bounded retry via
   `retry_at`.
9. **Backoff (VERIFIED).** `refused_start_wait`, `stop_observe_wait`.
10. **OpenHands account selection (VERIFIED).** `openhands_router` /
    `AccountRouter.select_for_task` (distribute / failover modes).
11. **Account failover (VERIFIED).** `tried_accounts` excludes a failed
    account; `handoff_context` carries 14 items on a switch.
12. **Cloud API timeouts (VERIFIED).** `CloudClient.timeout=30s`,
    `CloudDirectClient.timeout=60s`.
13. **External execution recovery (VERIFIED, partial).** The loop re-polls a
    task's conversation; a conversation that is still running is detected by
    `conversation_state` / `activity_marker`.
14. **Process restart recovery (VERIFIED).** Leases expire and tasks are
    reclaimed; a run in an active state is re-serviced.
15. **Stale run detection (VERIFIED).** `stall_seconds()`,
    `activity_marker`; a run with no progress is flagged.
16. **Exactly-once limitations (VERIFIED, documented).** Delivery is
    **at-least-once**, not exactly-once: an external conversation may run more
    than once for one task (a "resume" starts a second conversation).
17. **At-least-once safeguards (VERIFIED).** Verification requires a real
    branch head change (`GitVerifier`); a change-requiring task whose branch
    did not move is `completed_unverified` and reworked.
18. **Event ordering (INFERRED).** Events are per-entity sequenced
    (`nexus_project_events.seq`); the Rust bus must preserve per-entity order
    and be idempotent per consumer.
19. **DAG dependency resolution (VERIFIED).** A child runs only when its
    parents are `completed_verified`.
20. **Project isolation (VERIFIED).** Every run/task carries `project_id`;
    membership authorizes access (404 otherwise).

---

## 5. The hard problem: detecting that an external run is still alive

**Requirement (task):** the system must detect that an external Run is still
executing even when the local process lost contact with it.

**Current mechanism (VERIFIED):**

- The external object is an OpenHands Cloud conversation, identified by
  `conversation_id` (direct transport derives it from title + first message).
- Liveness is read from the Cloud: `get_conversation`,
  `conversation_state`, `activity_marker`, `event_count`, `final_response`.
- A stop is requested in `nexus_executor_stops` and the worker **observes**
  the stop before the branch is handed to another task (`_BRANCH_FREE` holds
  the branch while an abandoned conversation may still push).
- `SANDBOX_QUIET_SECONDS = 900`: a sandbox keeps every conversation it holds
  for a quiet window, so deleting a conversation cannot take a sandbox from
  under a new one.

**Target mechanism (INFERRED):** a `ReconciliationService` that, on every
loop pass and on startup, lists tasks in in-flight states, queries the Cloud
for each `conversation_id`, and reconciles local state with the Cloud's
(three outcomes: still running → keep the lease and the branch hold; finished →
verify; gone → fail/rework). The local process **never** assumes a run is dead
because it lost contact; it assumes the opposite until the Cloud says
otherwise.

---

## 6. Mandatory scenarios and expected behaviour

| Scenario | Expected behaviour |
|---|---|
| Two sequential orders in one conversation | each distinct message → its own run; identical text → no second run |
| Parallel independent projects | full isolation; no shared branch hold across projects |
| Repeat after timeout | idempotent; the same task is not double-created |
| Restart during execution | lease expires; task reclaimed; Cloud conversation re-polled |
| OpenHands Cloud lost | task retries with backoff; branch held until the stop is observed |
| LLM provider lost | cascade failover; a total outage is a recorded failure, not a hang |
| DB connection lost | reconnect on evidence; in-flight transaction rolled back cleanly |
| Duplicate event processing | idempotent consumer; no double state transition |

---

## 7. Data the engine owns

`nexus_runs`, `nexus_run_tasks`, `nexus_run_reports`, `nexus_evidence`,
`nexus_executor_stops`, `nexus_task_executions`, `nexus_task_handoffs`,
`nexus_lab_workers`, `nexus_conversation_cleanups`. The engine is the **only**
writer of terminal run/task state.

---

## 8. Acceptance criteria

The Rust engine is accepted when all eight scenarios in §6 pass on a staging
deployment with a real PostgreSQL and the stand-in OpenHands/GitHub/LLM
servers, **and** the at-least-once safeguards (§4.17) hold under failure
injection. See [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md) §4.
