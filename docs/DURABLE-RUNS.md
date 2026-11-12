# Durable autonomous runs, reports, stopping, executors and repositories

What an autonomous run does when things go wrong, what it owes the person who
started it, and which OpenHands connections and GitHub repositories a project
works with. Everything here is state in PostgreSQL: nothing depends on a browser
being open or on the process that started the work still being alive.

## Two kinds of run

| | `durable` | `bounded` |
|---|---|---|
| Started by | the chat ("Автономно"), or `POST …/goals/<id>/run` without budgets | `POST …/goals/<id>/run` with `max_iterations` or `deadline_seconds`, or `"policy": "bounded"` |
| A task used its attempts | waits, then starts the next work cycle | `failed`; the run is `blocked` |
| A result did not verify | the agent is told what is missing; the task is at work again | `completed_unverified`; the run is `blocked` |
| The deadline passed | not applied | `failed` ("deadline passed") |
| `max_iterations` spent | the run pauses and goes on with the budget restored | `failed` |

A bounded run is exactly what a run was before; callers that named budgets keep
the behaviour they asked for. `LabStore.start_run` defaults to `bounded`, so
code that creates runs directly is unchanged.

## What a durable run does instead of stopping

- **Attempts are counted inside a work cycle.** A task has `max_attempts`
  (three) per cycle. When they are used up the task becomes `waiting` with a
  `retry_at` and a `wait_reason`, and then starts cycle N+1 with the attempts
  restored and no executor ruled out. The wait is
  `NEXUS_RETRY_BASE_SECONDS * 2**(N-1)`, capped at `NEXUS_RETRY_MAX_SECONDS`
  (a minute doubling to an hour), with a small spread derived from the task so
  that tasks which failed together do not come back in the same second.
- **A refused start (HTTP 429) is not an attempt.** The task waits as long as
  `Retry-After` asks (30 s … 1 h); without it the wait doubles with each
  refusal in a row, from a minute, or from 15 minutes for a daily limit.
- **A result that does not verify is reworked, never accepted.** The task's
  `verification` keeps the verifier's reason. If the conversation finished, the
  next attempt is a message *in that conversation* saying what is missing (the
  commit is not on the branch, no commit was named, …); a conversation that
  was already told once and still did not deliver is told to stop and replaced
  by a new one.
- **A check that could not be made is made again.** When GitHub cannot be
  asked, the result is held and looked at a minute later. Nothing is spent and
  nothing is said to the agent.
- **A time limit or a hung conversation stops that conversation, not the
  task.** A conversation over `timeout_seconds`, or one that shows no activity
  for `NEXUS_TASK_STALL_SECONDS` (30 minutes; the Cloud is asked for its newest
  event once a minute), is told to stop and the task is started again.
- **No usable executor, or a repository the project may not use right now**:
  the task waits and asks again, each time after a longer wait.
- **A restart changes nothing.** Waits are timestamps, claims are leases with a
  fencing token, stop signals and reports are rows. Another process picks the
  run up where the first one left it. A task claimed by a worker that died
  before starting it is taken back when the lease runs out; a conversation that
  such a worker did start is adopted, not started twice.
- **It stays governable.** `NEXUS_LAB_MAX_ACTIVE_TASKS` (8) bounds how many
  tasks are at work at once over all runs; one task at a time works on a branch
  of a repository; `NEXUS_RETRY_MAX_CYCLES` (0 = none) is the operator's
  ceiling on cycles per task, after which the task stops and the run waits for
  a person. Pause and cancel work at any moment.

A run that is between two tries is still `running`. `GET …/runs/<id>` shows
each waiting task as `waiting` with its `retry_at` and `wait_reason`, and the
run's own `resume_at` and `wait_reason` when the run as a whole pauses.

## The report in the chat

When a run completes, its report is appended to the conversation it was started
from, as an assistant message (`meta.report`). Nobody has to open the runs tab.

- The report row (`nexus_run_reports`, one per run) is written **in the
  transaction that completes the run**, and a run completes only after every
  task verified and the repositories were looked at once more: each accepted
  commit must still be in its branch. A commit that left the branch sends its
  task back to work before any report exists.
- Delivery appends the chat message and marks the row delivered in one
  transaction, on a locked row. A failed delivery is tried again (5 s doubling
  to 10 min) by the worker loop, also after a restart; two workers deliver one
  message.
- A run that was cancelled, failed or is still at work has no report.
- The report says what was asked (quoted; a long text by its beginning, size
  and SHA-256), what was done task by task, the branch and commits per
  repository, the files GitHub lists as changed, what the repository's own
  checks say for the head commit, what remains, and the status. It separates
  what NEXUS observed itself from what the agents reported.

`GET /api/v1/projects/<id>/runs/<run>/report` returns the same report;
`GET …/runs/<run>` includes it together with `executions` (which connection
ran which attempt, in which conversation) and `stops`.

## Cancelling

`POST …/runs/<run>/cancel` is one transaction: the run becomes `cancelled`
and hidden (`hidden_at`), its unfinished tasks are `blocked`, their leases are
released, and a stop signal is recorded for every conversation that was at work.

- **Nothing is deleted.** Tasks, evidence, executions, messages, events and the
  cancellation itself (`cancelled_at`, `cancelled_by`) stay. `GET …/runs`
  leaves hidden runs out; `GET …/runs/<run>` still answers for one by its id.
  `POST …/runs/<run>/hide` hides any other run that is over.
- **The executor is really told to stop**, and the signal is owed until it is
  seen to have worked (below).
- **It is idempotent and wins races.** Cancelling twice changes nothing. A
  cancel and a completion at the same moment end one way; a conversation started
  between the claim and the cancel is stopped all the same.
- A cancelled run does not hold its goal: a new run of the same goal can start
  at once. It does hold its branch until its conversations are stopped.

### How a conversation is stopped

`stop_conversation(conversation_id) -> bool` on every transport, `True` only
when the stop was taken:

| Transport | Call |
|---|---|
| `app` (OpenHands Cloud) | pauses the conversation's sandbox: `POST /api/v1/sandboxes/<id>/pause`. The Cloud has no conversation-scoped stop and its own client does the same. A conversation started through this transport has a sandbox of its own. |
| `direct` | `POST /api/conversations/<id>/interrupt` (where the server has it), then `/pause` on the sandbox's agent-server; other conversations on the sandbox go on. |
| `local` | `POST /api/conversations/<id>/pause`. |

`OpenHandsExecutor.stop(account_id, conversation_id, project_id)` is the one
place the loop stops a conversation through. A stop that was not taken is not
final: a conversation that is gone, or already in a state in which it does
nothing more, counts as stopped.

Two records say what is still owed, for two situations:

- **`nexus_run_tasks.abandoned`** - a conversation a *switch* or a *failover*
  left behind while its task goes on. Drained by the run's own pass; for a run
  that has ended meanwhile, by the worker loop.
- **`nexus_executor_stops`** - every other stop: a cancelled run, a time limit,
  a hung conversation, a conversation replaced after rework, a start made by a
  worker that had lost the task. Delivered by the worker loop whatever the run
  is doing; **confirmed** only when the executor reports the conversation in a
  state in which it does nothing more (or gone). Until then the signal is sent
  again with a growing pause. After 40 tries it is given up (`abandoned`) and
  says why; that takes hours, both for a signal that cannot be sent (about six)
  and for one that was taken while the conversation still reports itself at
  work (about five: an agent in the middle of a long step lets go when the step
  ends). A stop that is given up no longer holds the branch.

While either record holds a conversation, its repository and branch are given
to no other task.

## OpenHands connections

A connection is a row of `nexus_openhands_accounts`: a name, an API URL, a
transport, and a key encrypted with `NEXUS_SECRET_KEY`. The key is never
returned, logged or shown; views carry a masked hint.

| Request | Does |
|---|---|
| `GET /api/openhands/accounts` | list (no keys) |
| `POST /api/openhands/accounts` | add: the URL is checked, then the key is verified against the service before anything is stored |
| `POST …/accounts/<id>/edit` (alias `/rename`) | change the name, URL, model, limits - and, with `api_key`, the key (verified first) |
| `POST …/accounts/<id>/verify` | is the key accepted; records the API version and whether the routes NEXUS needs exist (`compatibility`) |
| `POST …/accounts/<id>/connection` | is the endpoint reachable (an answer, even `401`, is "reachable") |
| `POST …/accounts/<id>/enable` | switch it on or off: `{"enabled": false}` switches it off, the key stays stored |
| `POST …/accounts/<id>/remove` | remove; refused (409, with `dependencies`) while a project uses it or a task is at work on it; `{"force": true}` overrides |
| `GET …/accounts/<id>/dependencies` | what uses the connection: projects, tasks at work |

**The URL is checked against server-side request forgery** (`net_guard`): only
`http`/`https`, no credentials, query or fragment in the URL, and every address
the host resolves to must be public. The check is repeated when the socket is
opened, so a name that later resolves to `127.0.0.1` is refused then; redirects
are never followed. A private address is allowed only by the operator:
`NEXUS_OPENHANDS_ALLOWED_HOSTS`, `NEXUS_OPENHANDS_ALLOW_PRIVATE`.

## Executors of a project

`nexus_project_executors` is a project's own list of connections, in order.
Position 0 is the **primary**, the rest are **backups**.

- A project with a list uses **only** the connections on it that are switched
  on for it. Another project's connections are never chosen for it. A project
  without a list keeps the earlier rule (its owner's pool, or the server's own
  key).
- `executor_strategy`: `failover` (the first usable one in order; work returns
  to the primary when it is usable again) or `distribute` (the usable one with
  the fewest tasks at work, so independent tasks run side by side).
- A task that is running stays on its connection: a conversation cannot move.
  A connection that failed a task is ruled out for that task until its next
  cycle.
- Two agents never write to one checkout: one task per repository and branch,
  across runs and connections.

| Request | Does |
|---|---|
| `GET /api/v1/projects/<id>/executors` | the list, the strategy, each connection's health |
| `POST …/executors` | set the whole list in order: `{"executors": [id \| {"account_id", "enabled"}, …], "strategy"}` (or `{"primary", "fallbacks"}`) |
| `POST …/executors/check` | verify every connection on the list |
| `POST …/executors/<account>/primary` \| `enable` \| `disable` \| `remove` | one entry |

Only an owner of the project changes the list, and only with connections of
their own. Which connection ran which attempt is in `nexus_task_executions` and
in the run card.

## Several repositories in one project

A link (`nexus_project_repositories`) is what authorizes work on a repository.
Each link now also carries:

- **`github_account_id`** - the GitHub account that reads and writes this
  repository. Repositories of one project may belong to different accounts; a
  repository without one uses the project's account, and then the server's
  token.
- **`is_primary`** - the main repository (one per project).
- **`working_branch`**, **`role_note`** (what the repository is for, told to
  the planner and the agents), **`enabled`**.

| Request | Does |
|---|---|
| `GET /api/v1/projects/<id>/repositories` | the links with their accounts |
| `POST …/repositories` | link one more repository (`{"full_name"}`) |
| `POST …/repositories/remove` | unlink; refused (409) while work is under way on it |
| `POST …/repository` | link and bind as the main one, as before |
| `POST …/repositories/<owner>/<name>/primary` \| `account` \| `update` \| `enable` \| `disable` \| `check` | one link: `update` takes `branch`, `role_note`, `label`, `enabled`, `account_id` |
| `POST …/repositories/check` | ask GitHub what each link's own identity may do with it |

The planner is given the list and assigns each task a repository; a task's
agent is told every repository of the project, which one is the main one, and
which one is its own. A task works in its own repository and branch; the
verifier reads that repository with that link's account; the report lists
commits, files and checks per repository. A run cannot complete while a task
of any repository is unverified.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_RETRY_BASE_SECONDS` | 60 | wait before the second cycle |
| `NEXUS_RETRY_MAX_SECONDS` | 3600 | longest wait between cycles |
| `NEXUS_RETRY_MAX_CYCLES` | 0 | ceiling on cycles per task (0 = none) |
| `NEXUS_TASK_TIMEOUT_SECONDS` | 7200 | time limit of one conversation |
| `NEXUS_TASK_STALL_SECONDS` | 1800 | no activity for this long = hung (0 = off) |
| `NEXUS_LAB_MAX_ACTIVE_TASKS` | 8 | tasks at work at once (0 = no limit) |
| `NEXUS_OPENHANDS_ALLOWED_HOSTS` | - | hosts that may be private |
| `NEXUS_OPENHANDS_ALLOW_PRIVATE` | off | any private address may be used |

## Migrations

`0030_durable_runs.sql`, `0031_openhands_connections.sql`,
`0032_project_repositories.sql`. Additive; existing rows are kept:

- a run that is alive becomes `durable` and reports to the chat it came from;
  a finished run is left as it was; a run cancelled earlier is hidden;
- the connection a project was bound to becomes its primary executor;
- the repository a project was bound to becomes its main one, with the
  project's account.

Each can be applied twice. `tests/test_durable_migrations.py` applies them to a
database that already holds runs, projects and links.

## What this does not do

- A run that waits costs nothing while it waits, but it does start an agent
  again each cycle. There is no budget in money or tokens.
- On Render's free plan the process sleeps after about 15 minutes without a
  request, and the loop with it; waits, stops and reports resume when it wakes.
- A stop is a request to the executor. Until it is confirmed the conversation
  may still push; that is why its branch stays held.
- The agents are told which repository is theirs; nothing technical prevents
  an agent with access to several repositories from writing to another one.
  The verifier accepts only the commit on the task's own repository and branch.
