# Failover and context hand-off

What happens when an OpenHands account fails while a task is in flight, and how
the work continues on another account without restarting it.

## The hard constraint

**A Cloud conversation cannot be moved between accounts.** A different account is
a different sandbox and a different key. So "continue on account B" means: start
a *new* conversation on B and tell it what has already happened. That message is
the hand-off context; it is built from durable run rows, not from anything the
old conversation said.

## The failover loop

1. A task is claimed and dispatched. The router selects an account (the pool
   policy; see `OPENHANDS_ACCOUNTS.md`).
2. The start fails (a 5xx, a dropped connection, a rejected key) or the
   conversation later vanishes (the executor is gone).
3. NEXUS records the failed account on the task's `tried_accounts` and in the
   pool's health, then requeues the task — **excluding that account**.
4. The next tick selects a different usable account and starts a new
   conversation there.
5. The new conversation receives the hand-off message and continues.

The switch is recorded durably in `nexus_task_handoffs` (from account, to
account, reason, context), so the history of an executor change is auditable.

## What the hand-off context carries

Built by `AccountRouter.handoff_context` from the run's rows:

- project id, task id, task key, run id;
- the **original goal text** (the task the plan was made from);
- the **plan**: every task key and title with its state;
- which tasks are already `completed_verified` (do **not** redo these) and which
  remain;
- the repository and branch;
- the **last known commit** (from the task's recorded evidence);
- the previous executor's last report and the error that caused the switch;
- the acceptance criteria.

The message (`handoff_message`) states plainly that this is a continuation, that
the workspace is fresh (a new sandbox), and that the repository must be cloned
at the named branch first.

## Guarantees

- **The old executor is not reattached.** `tried_accounts` excludes it, and a
  poll only ever talks to the account the task is bound to. A conversation whose
  account is gone is never polled again.
- **Completed work is not repeated.** The hand-off names the verified tasks.
- **No infinite loop.** When the pool has nothing left for a task (every account
  tried or unusable), the run is **blocked** with the reason as evidence — never
  spun.
- **A failed start does not increment `attempt` against the pool.** It is a
  transport problem, so the task is requeued for another account; the attempt
  budget still bounds the total.

## Manual switch during a run

The operator controls on the studio's «Агент» tab (cancel / resume / retry)
still apply. To force a switch, disable the current account
(`POST /api/openhands/accounts/{id}/enable {"enabled": false}`) and retry the
task; the pool will select another. A switch is only safe when the task is not
mid-write; the workspace lock (`uq_tasks_workspace_active`) and the lease keep
two executors off one checkout.

## Verification

`tests/test_openhands_failover.py` is the load-bearing test: a real task starts
on account #1, #1 fails, the scheduler switches to #2, and #2 receives the
hand-off context. The assertions are on real PostgreSQL rows and on the messages
the clients actually received.
