# Integration map: control plane ↔ OpenHands runtime

Every fact below was read from the live server at `http://127.0.0.1:60000`
(`build_git_sha 1e1390a`, tag `v1.50.1`) via `GET /openapi.json` and from the
installed source, not from other versions' docs.

## Confirmed API surface

| Purpose | Method | Path | Confirmed behaviour |
|---|---|---|---|
| Create conversation | POST | `/api/conversations` | **201 when created, 200 when `conversation_id` already exists.** Body `StartConversationRequest`; `conversation_id` is client-supplied. **422 unless `agent_settings` (or `agent`/`agent_profile_id`) is present.** |
| Read settings | GET | `/api/settings` | Returns `agent_settings` the server already has. The dispatcher forwards this instead of holding a key. |
| Send task | POST | `/api/conversations/{id}/events` | Body `SendMessageRequest{role, content:[TextContent], run}`. `run=true` starts the loop. |
| Start loop | POST | `/api/conversations/{id}/run` | No body. **409 = already running** (safe to ignore), 429 = capacity full, 404 = unknown. |
| Read state | GET | `/api/conversations/{id}` | `ConversationInfo.execution_status`. |
| Read result | GET | `/api/conversations/{id}/agent_final_response` | `{response: str}`. Empty string when no finish message. |
| Read events | GET | `/api/conversations/{id}/events/search` | `{items, next_page_id}`. **`GET /events` without a body returns 422.** |
| Event count | GET | `/api/conversations/{id}/events/count` | Returns a bare integer, not an object. |
| Pause | POST | `/api/conversations/{id}/pause` | Stops the loop, keeps the conversation. |
| Auth | — | header | `X-Session-API-Key`. No auth bypass used. |

### Two traps found by running against the live server

1. **`agent_settings` is mandatory on create.** Without it the server answers
   `422 ... One of agent, agent_settings, or agent_profile_id must be provided`.
   The dispatcher reads `/api/settings` once and forwards what the server
   already trusts, so it never has to hold an API key of its own.
2. **The `kind` filter on `events/search` does not match event class names.**
   `?kind=ConversationErrorEvent` returns zero items even when such an event
   exists. The adapter therefore fetches events unfiltered and matches the
   `classification` field on the client.


### `execution_status` enum (live schema)

`idle`, `running`, `paused`, `waiting_for_confirmation`, `finished`,
`error`, `stuck`, `deleting`.

Terminal-good = `finished`. Terminal-bad = `error`, `stuck`.
`waiting_for_confirmation` is a **blocked** condition, not completion.

## Idempotency mechanism (the important discovery)

`conversation_service._start_conversation` does:

```python
conversation_id = request.conversation_id or uuid4()
existing = self._conversation_records.get(conversation_id)
if existing is not None or (event_service and event_service.is_open()):
    ... return info, is_new=False   # 200
```

So a **client-chosen `conversation_id` makes creation idempotent on the
server**. The dispatcher therefore derives a deterministic UUIDv5 from the
task id instead of storing a random one. A retry after a crash re-issues the
same id and gets the existing conversation back — no duplicate.

## Where state lives

- Durable assignment `task_id ↔ conversation_id` lives in SQLite
  (`tasks.conversation_id`), not in memory and not in the transcript.
- The conversation id is derivable from the task id, so the mapping survives
  even if the row is lost; the stored column is the fast path and the audit
  trail.

## Failure classification

The runtime classifies its own errors. `ConversationErrorEvent.classification`
carries `{kind, retryable, user_action}` where `kind` is one of `auth`,
`quota`, `rate_limit`, `config`, `transient`, `agent_action`, `internal`,
`unknown`. The dispatcher reads it and **does not retry an error the runtime
marked `retryable: false`** — an expired key cannot start working on its own,
so such a task is dead-lettered immediately with the classification kept as
evidence, instead of burning its attempt budget first.

| Signal | Class | Action |
|---|---|---|
| Timeout, connection refused, 5xx | transient | keep lease, retry next tick; never "completed" |
| 404 on `GET /{id}` | conversation gone | re-queue (bounded by `max_attempts`) |
| 409 on `run` | already running | treat as success |
| 429 | capacity | back off, leave task assigned |
| `error` + `retryable: false` | permanent | **dead-letter now**, keep classification |
| `error` + retryable/unknown | worker failed | `failed` (bounded retry) |
| `waiting_for_confirmation` | ambiguous | `blocked`, needs human |
| `finished` but empty result | unverified | `blocked`, **not** completed |
| `finished` with result | verified | `verifying` → `completed` |

Verified on the live runtime: a task with an invalid LLM key went
`running → dead_letter` in a single tick with
`evidence=[{reason: non-retryable runtime failure, kind: auth,
user_action: settings}]`, `attempt == 1`.

## Scope boundary

Recovering the control plane (leases, assignments, task state) is **not** the
same as recovering the OpenHands runtime process. This integration restores
the former. If the runtime itself is down, the adapter reports transient
unavailability and the control plane waits; it does not restart the runtime.
