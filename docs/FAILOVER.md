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

## Checking the new account before trusting it (step 6)

A switch does not assume the account it moves to still works. Before the start,
`OpenHandsExecutor.dispatch` calls `AccountRouter.ensure_authorized`, which makes
one read-only call to that account's own API (`account_capabilities`) through the
*same client* the task will be dispatched with. A rejected key raises
`CloudAuthError`, which becomes a `DispatchError` naming the account, so the
scheduler excludes it and fails over again — rather than starting a conversation
that would only fail inside the sandbox.

The check runs only on a *switch*: a task that already has a `tried_accounts`
entry, or an operator's `preferred_account_id`. A first start on the account the
pool picked is an ordinary dispatch and is not probed, so an ordinary task pays
no extra round trip. A transient transport failure during the probe is not read
as a rejected key: it propagates as `CloudUnavailable` and the normal retry
applies, so a blip never writes off a working account.

## Confirming the new executor really works (step 10)

A switch is not treated as successful because a conversation started. The
account is marked healthy (`AccountRouter.note_success`) only when a poll
observes a terminal conversation on it (`finished` / `error` / `stuck`) — the
observed evidence that the new executor is reachable and running, not a
guess. Until then the task's result is whatever the verifier makes of the real
report and the real branch head.

## A start the service refuses (429)

A rate limit or a daily quota is not a failure of the account: it answered, and
it will take work again. Nothing was started, so the task keeps its attempt and
the run its iteration. The account is marked `degraded`, which leaves it
selectable for other work, and then:

- if another account can take the task, it does, at once, as a hand-off;
- if none can, the task waits (`Retry-After` when the service sent one, 15
  minutes for a daily limit, otherwise a minute) and is then offered to the
  whole pool again. The run stays `running`; the task's line says what it is
  waiting for.

## Another attempt on the same account

A task whose result did not verify, or that ran out of time, is started again
when the run is resumed. The pool may choose the account it ran on - with one
account there is no other - and that start is an ordinary one, not a hand-off:
the agent gets the task's own instructions.

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
  tried or unusable), a run with explicit budgets is **blocked** with the reason
  as evidence — never spun. A durable run (one started from the chat) does not
  stop: the connections that were written off are probed again, the task waits
  — each time longer, up to an hour — and is offered to every executor again
  (`docs/DURABLE-RUNS.md`). A refused start (above) is not either case: it
  waits at the pace the service asked for.
- **Only the project's own executors.** A project that was given a list of
  connections (a primary and backups) fails over inside that list and nowhere
  else; work returns to the primary once it is usable again.
- **A failed start does not increment `attempt` against the pool.** It is a
  transport problem, so the task is requeued for another account; the attempt
  budget still bounds the total.

## Manual switch during a run

An operator can hand a running task to a named account directly:

```
POST /api/v1/projects/{project}/runs/{run}/tasks/{task}/switch
{"account_id": "<uuid>"}
```

The task must be in flight (`dispatched`/`running`/`verifying`); a pending task
has no executor to switch from and a terminal one is done, so both are a `409`.
The target must be the principal's active, enabled account. The switch records
the current account as tried and stores the target as the task's preference; the
lab worker performs the move on its next tick, starting a **new** conversation on
the target with the hand-off context. The old conversation is **stopped** (best
effort, transcript preserved) and recorded in the task's evidence; until the
stop is confirmed the task's branch stays held (`_BRANCH_FREE`), so no second
writer starts on the same checkout. The preference applies to exactly one
hand-off and is cleared once the task is dispatched, so a switch never pins a
task to an account for good.

`stop_conversation` is **transport-honest**: the `direct` and local transports
pause the conversation (`POST /api/conversations/{id}/pause`, keeping the
transcript) and return `True`; the `app` transport returns `False`, because the
Cloud V1 surface only pauses at the *sandbox* level. A `False` must not be read
as "stopped" — the branch lock, not the stop, is what guarantees two executors
do not push to one branch at once.

The operator controls on the studio's «Агент» tab (cancel / resume / retry) still
apply. To force a switch without the endpoint, disabling the current account
(`POST /api/openhands/accounts/{id}/enable {"enabled": false}`) and retrying the
task also works, because the pool then selects another. Either way a switch is
only safe when the task is not mid-write; the workspace lock
(`uq_tasks_workspace_active`) and the lease keep two executors off one checkout.

## Verification

`tests/test_openhands_failover.py` is the load-bearing test: a real task starts
on account #1, #1 fails, the scheduler switches to #2, and #2 receives the
hand-off context. The assertions are on real PostgreSQL rows and on the messages
the clients actually received.

## The pool learns from success, not only failure

A dispatch failure records the account as `degraded` (retryable) or
`unavailable` (a rejected key or unreachable endpoint). A **conversation that
reaches a terminal state** on an account — `finished`, `error` or `stuck` — is
evidence the account is reachable again, so the scheduler calls
`AccountRouter.note_success` and returns it to `healthy`. Without that, an
account excluded by one transient failure would never re-enter the pool.

Load balancing reads in-flight tasks from `nexus_run_tasks` rather than a
counter, so the number it balances on cannot drift after a crash (see
`OPENHANDS_ACCOUNTS.md`).
