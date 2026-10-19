# Cloud integration map: Sr/Jr dispatcher ↔ OpenHands Cloud V1

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
