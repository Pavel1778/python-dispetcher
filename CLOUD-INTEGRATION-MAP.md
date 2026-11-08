# Cloud integration map: Sr/Jr dispatcher ↔ OpenHands Cloud V1

> Historical observations below are not a current production verification. In
> particular the default `direct` transport does not establish that a requested
> repository/branch was selected, global sandbox reuse is not project-scoped,
> and workspace claim checks can race across processes. A nonempty result is
> not independent QA evidence. See [docs/NEXUS-SPEC.md](docs/NEXUS-SPEC.md).

Every fact below was read from the live OpenAPI document at
`https://app.all-hands.dev/openapi.json` and confirmed by making real calls,
not inferred from other versions' docs. Facts that cost a real request to learn
are marked **trap**.

## Confirmed API surface

| Purpose | Method | Path | Confirmed behaviour |
|---|---|---|---|
| Create conversation | POST | `/api/v1/app-conversations` | Returns an **`AppConversationStartTask`**, not a conversation. `200` on success, `422` on a bad body. |
| Poll start task | GET | `/api/v1/app-conversations/start-tasks?ids=<id>` | Batch. Returns a **list**. `app_conversation_id` appears only once `status` is `READY`. |
| Read conversation | GET | `/api/v1/app-conversations?ids=<id>` | Batch. Returns a **list**. `execution_status` here. |
| Search | GET | `/api/v1/app-conversations/search` | `{items, next_page_id}`. `page_id` paginates. |
| Send message | POST | `/api/v1/app-conversations/{id}/send-message` | Body `{role, content:[text], run}`. `{success, sandbox_status}`. |
| Delete | DELETE | `/api/v1/app-conversations/{id}` | `200 {success: true}`. |
| Pause / resume | POST | `/api/v1/sandboxes/{id}/pause` \| `/resume` | Sandbox-scoped, not conversation-scoped. |
| Read events | GET | `/api/v1/conversation/{id}/events/search` | `{items, next_page_id}`. **`limit` > 100 is a 422.** |
| Auth | — | header | `Authorization: Bearer <OPENHANDS_API_KEY>`. |

There is **no `agent_final_response` endpoint** on Cloud V1 (the local
agent-server has one). The final answer is read from the last assistant
`MessageEvent` in the event stream.

### Two traps found by running against the live server

1. **`limit` is capped at 100 on `events/search`.** A request with `limit=200`
   is rejected: `422 ... Input should be less than or equal to 100`. The client
   caps it.
2. **Tag keys must be lowercase alphanumeric.** `{"orchestrator_task_id": ...}`
   is rejected: `422 ... Tag key 'orchestrator_task_id' is invalid: keys must be
   lowercase alphanumeric`. The dispatcher uses `orchestratortaskid`.

### `AppConversationStartTaskStatus` enum (live schema)

`WORKING`, `WAITING_FOR_SANDBOX`, `PREPARING_REPOSITORY`, `RUNNING_SETUP_SCRIPT`,
`SETTING_UP_GIT_HOOKS`, `SETTING_UP_SKILLS`, `STARTING_CONVERSATION`, `READY`,
`ERROR`. The conversation id is present from the start task's payload, but is
only meaningful at `READY`.

### `ConversationExecutionStatus` enum (live schema)

`idle`, `running`, `paused`, `waiting_for_confirmation`, `finished`, `error`,
`stuck`, `deleting`. Terminal-good = `finished`. Terminal-bad = `error`,
`stuck`. `waiting_for_confirmation` is **blocked**, not completion.

## Two-phase creation (the important difference from the local API)

`POST /api/v1/app-conversations` returns a start task. The conversation id
arrives later. The dispatcher therefore:

1. POSTs the start request and records the returned **start-task id** in
   `conversations.external_start_task_id` before polling, so a crash between
   create and ready is recoverable.
2. Polls `start-tasks?ids=` until `READY` (or `ERROR`).
3. Records `app_conversation_id` and moves the task to `running`.

A restart finds the recorded conversation and reuses it. `conversations.task_id`
is UNIQUE, so a second conversation is never created for the same task.

## Repository selection

`selected_repository` (`"owner/name"`) and `selected_branch` are real, documented
fields on the start request. **The dispatcher only dispatches a repository that
is present and enabled in the `repositories` allowlist**, and a branch that the
allowlist permits. A task with no repository runs repository-less (analysis-only
work). A task that names a non-allowed repository is dead-lettered with evidence
before any conversation is created.

### Live environment blocker (recorded, not worked around)

On the account used for verification, **no GitHub provider is connected**
(`GET /api/v1/settings` → `provider_tokens_set: {'github': None}`). Starting a
conversation with `selected_repository` therefore depends on a GitHub token.
The integration path is implemented and validated end-to-end **without** a
repository (the repository-less path ran `running → finished` against real
Cloud). The repository path is validated by unit tests against a real local
HTTP server; it needs a connected provider to run against live Cloud.

## Where state lives

- Durable task state, leases and assignment live in Supabase PostgreSQL
  (`tasks`, `conversations`, `task_events`).
- `conversations.task_id` is UNIQUE and `tasks.idempotency_key` is UNIQUE, so
  both dispatch and submission are idempotent across restarts.
- Claim is atomic via `SELECT ... FOR UPDATE SKIP LOCKED`, so two dispatchers
  can run concurrently without taking the same task.

## Failure classification

| Signal | Class | Action |
|---|---|---|
| Transport error, 5xx | transient | keep lease, retry next tick; never "completed" |
| 401/403 | auth | **dead-letter now**, keep evidence (a bad key cannot fix itself) |
| 422 | validation | **dead-letter now**, keep evidence (a bad request cannot fix itself) |
| 404 on conversation | gone | requeue within the attempt budget |
| `error` / `stuck` | worker failed | `failed` (bounded retry), then dead-letter |
| `waiting_for_confirmation` | ambiguous | `blocked`, needs a human |
| `finished` but empty answer | unverified | `blocked`, **not** completed |
| `finished` with answer | verified | `verifying` → `completed` |

## Invariants kept from the local dispatcher

- A missing answer is never completion.
- One action per task per tick (`_touched`).
- `assigned` has no direct edge to `queued` or `dead_letter`; recovery walks a
  legal path (`force_transition`) instead of forcing an impossible move.
- The Cloud key is read from the environment and never logged or persisted.

## Client tools: how Sr actually acts (verified live)

A Cloud conversation is not, by itself, able to call back into our system. The
documented way to let the agent act is the `client_tools` field on
`POST /api/v1/app-conversations` (schema `ClientToolSpec`: `name`,
`description`, `parameters`, `annotations`). Verified against the live API:

1. Start a conversation with `client_tools`.
2. When the agent calls one, the server emits an **`ActionEvent`** in the
   conversation's event stream with `tool_name` set and
   `action.kind == "ClientAction_<name>"`. The agent receives only an
   acknowledgment `ObservationEvent` and continues.
3. The client polls `GET /api/v1/conversation/{id}/events/search`, executes the
   call, and delivers the result back with `send-message`.

Confirmed live: a probe conversation called `jr_probe` and produced exactly
`ActionEvent(tool=jr_probe, action={"value":42,"kind":"ClientAction_jr_probe"})`
plus an `ObservationEvent` acknowledgment.

### Trap: a client tool name that collides with a built-in is a 422

A `ClientToolSpec` whose `name` equals a built-in tool name is rejected by the
agent server with **422** during conversation start (the start task reaches
`ERROR`). Confirmed live: `create_task` and `list_child_tasks` both collide (the
SDK registry contains `task`/`task_tool_set` and `task_tracker`), while the same
schemas under unique names start fine. The Sr tools are therefore named with an
`sr_` prefix (`sr_create_task`, `sr_list_child_tasks`, ...) and the prefix is
unwrapped in `sr_runtime`.

### Trap: the model invents `workflow_id`

Observed live: given a `workflow_id` parameter, the model passed a made-up string
(`"sr-delegation-single-unit"`), which the store rejected as a bad uuid. The
bridge therefore removes `workflow_id` from the tool schemas and always
substitutes the real one from context; the model never supplies it.

### Daily conversation quota

The account enforces a **daily conversation limit (20/day)**; exceeding it is a
**429** `daily_conversation_limit_reached`. Schema probing consumed the quota for
the day, so the full live Sr->Jr chain could not be re-run after the fixes. The
chain is covered by tests against a real local HTTP server that reproduces the
`client_tools`/`ActionEvent` behaviour, and the earlier live probe confirms the
mechanism itself.

## Web control panel

`orchestration/web_server.py` is a stdlib-only HTTP server. `POST /api/workflows`
creates a workflow and starts a real Sr conversation with the Sr client tools
attached; a background loop runs `SrRuntime.tick()` (Sr tool calls -> Jr tasks)
and `CloudDispatcher.tick()` (Jr tasks -> Cloud conversations). The page lists
workflows and shows each workflow's tasks and Sr tool calls. The Cloud key is
read from the environment and never sent to the browser.
