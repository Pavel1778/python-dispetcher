# Troubleshooting

Symptom-first. Every entry names the cause and the fix.

## OpenHands

### A conversation fails immediately with an LLM auth error

Symptom: `execution_status = error`, an `LLMAuthenticationError` event,
`classification.kind = "auth"`, `retryable = false`.

Cause: a bare sandbox started a conversation on a **default model whose key the
sandbox does not have**.

Fix: set `OPENHANDS_DEFAULT_MODEL` to a model the sandbox can reach
(`openhands/deepseek-v4.1-flash` is known to work). See `OPENHANDS_SETUP.md`.

### `429 DAILY_SANDBOX_LIMIT_REACHED`

Symptom: `POST /api/v1/sandboxes -> 429`, code `daily_sandbox_limit_reached`.

Cause: the account's daily sandbox quota is spent.

Fix: wait for the reset, raise the quota in the Cloud UI, or reuse a running
sandbox (the direct transport reuses one by default). The diagnostics classify
this as **BLOCKED** (a missing resource), not FAILED — the integration is fine.

### `422 ... One of agent, agent_settings, or agent_profile_id must be provided`

Cause: the create call omitted `agent_settings`. The dispatcher fetches it from
`GET /api/settings` once and forwards it. Do not strip it.

### A repository task runs against an empty checkout

Cause: the direct transport has no native `selected_repository` field.

Fix: leave `OPENHANDS_DIRECT_REPO_MODE=prompt` (the default), which prepends a
clone step, or set `refuse` to dead-letter instead of silently ignoring.

### `GET /api/conversations/{id}/events` returns `422`

Cause: that route needs a body. Use `/events/search` or `/events/count` instead.

### `?kind=ConversationErrorEvent` on `events/search` returns nothing

Cause: the filter does not match. Fetch unfiltered and match `classification` on
the client.

## Database

### `503 {"error": "database unavailable"}`

Cause: the store cannot reach PostgreSQL. The store holds one connection and
replaces it on evidence (a dead connection has data waiting to be read), then
backs off for `RECONNECT_BACKOFF_SECONDS` (2 s). A call in flight when the
connection dies fails once; the next call reconnects.

Fix: check the DSN and the database's health. `GET /readyz` does a real
round-trip; `GET /healthz` does not.

### `DuplicatePreparedStatement: prepared statement "_pg3_0" already exists`

Cause: server-side prepared statements are **on** behind a transaction-mode
pooler. They are off by default (`prepare_threshold=None`); do not re-enable
them behind a pooler. Reproduce only on a warm schema (a fresh schema hides it).

## Workers and runs

### An autonomous run stays `queued` and never advances

Cause: no worker is running (and `NEXUS_LAB_INLINE` is off).

Fix: start `python -m orchestration.lab_worker`. Check
`GET /api/openhands/diagnostics`: `lab_worker` reports FAILED when active runs
exist but no worker heartbeat is fresh.

### A run is `blocked`

Possible causes: a dependency ended unverified/failed/blocked (a deadlock, not
progress); every account for a task is unusable; a dispatch was not authorized.
The reason is in `last_error` and the task's evidence. Resume the run (retry) or
cancel it.

### A task was dispatched twice

It should not happen: claims are leases and `mark_running` binds one
conversation. If a worker died after claiming but before starting, the lease
expires and the task is requeued — the conversation id is cleared, so a second
conversation is not started behind the first. Check `nexus_task_handoffs` and
the task's `tried_accounts`.

## Accounts and failover

### A task dead-letters instead of failing over

Cause: the pool has nothing left for the task (every account tried or
unusable). In `manual` mode there is never a fallback. The reason is the
task's evidence ("no usable OpenHands account").

Fix: add a usable account, re-enable one, or change the selection mode to
`auto`/`failover`.

### The default account is down but `manual` mode will not switch

That is by design: `manual` uses the selected account and only that one.

### "the sandbox holds N bytes ... M bytes were sent"

Cause: file delivery verification failed — the sandbox's copy does not match
the sent bytes by size and SHA-256. The conversation is not created.

Fix: check sandbox disk space and the `NEXUS_SPEC_SANDBOX_DIR` setting.

## Diagnostics

### `live_conversation: UNVERIFIED`

That is the default: the live probe is opt-in. Pass `--live` (CLI) or
`{"live": true}` (HTTP) to run it. `UNVERIFIED` means "not run", not "broken".

### `live_conversation: BLOCKED`

The probe could not run: no usable account, or a resource limit (a 429). Nothing
is claimed about whether it would work.
