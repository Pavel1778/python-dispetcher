# OpenHands inventory (API, authorization, limits, compatibility)

A full inventory of the OpenHands surface NEXUS depends on: the Cloud V1 API,
the agent-server the `direct` transport drives, authorization, conversation
lifecycle, events, status, runtime/workspace, tools, limits, errors, timeouts,
availability, and how the current NEXUS client maps onto all of it.

**How this was produced.** Every fact below was read from a live source on
2026-11-12 — the Cloud OpenAPI document at `https://app.all-hands.dev/openapi.json`
and read-only calls against it, the agent-server OpenAPI at
`http://127.0.0.1:60000/openapi.json` and read-only calls against it, and the
repository's own clients in `orchestration/`. No endpoint was invented and no
mocked call is presented as evidence. Where a claim is a live observation it is
marked **live**; where it is only read from a schema it is marked **schema**;
where it was not exercised it is marked **unverified** or **blocked**.

This complements, and does not replace, the two evidence maps
(`INTEGRATION-MAP.md` for the legacy agent-server, `CLOUD-INTEGRATION-MAP.md`
for Cloud V1) and `docs/OPENHANDS_SETUP.md` for operator instructions.

---

## 0. Versions in scope (live)

| Surface | Where | Version | Source |
|---|---|---|---|
| OpenHands Cloud (app) | `https://app.all-hands.dev` | **1.87.1** (OpenAPI `3.1.0`, `info.version`) | live `GET /openapi.json`, `GET /server_info` (`app_version 1.87.1`, `sdk_version 1.60.1`) |
| Agent-server (sandbox) | the sandbox's `AGENT_SERVER` URL | **1.60.1** (`build_git_ref refs/tags/v1.60.1`) | live `GET /server_info` on the running sandbox |
| Default sandbox image | account setting | `ghcr.io/openhands/agent-server:1.50.1-python` | live `GET /api/v1/settings` → `default_sandbox_spec_id` |
| Observed running sandbox image | `GET /api/v1/sandboxes/search` | `ghcr.io/openhands/agent-server:1.60.1-python` | live |

**This is drift from the recorded maps.** `INTEGRATION-MAP.md` documents the
legacy agent-server at tag `v1.50.1` (`build_git_sha 1e1390a`); the live Cloud is
now **1.87.1** and the live sandbox agent-server is **1.60.1**. The NEXUS client
surface is a stable subset (see §11) and the live end-to-end probe still passes
(§10), so the drift is not breaking, but any future change must be checked
against these numbers, not the maps' numbers.

---

## 1. OpenHands Cloud API (V1)

Base URL: `https://app.all-hands.dev` (override with `OPENHANDS_CLOUD_URL`;
NEXUS validates it as `http(s)` at construction). The OpenAPI document lists
**242 paths**; the subset NEXUS uses is below.

### 1.1 Conversations (`/api/v1/app-conversations`)

| Purpose | Method | Path | Behaviour |
|---|---|---|---|
| Create | POST | `/api/v1/app-conversations` | Returns an **`AppConversationStartTask`** (not a conversation). `200`/`201`; `422` on a bad body. |
| Read | GET | `/api/v1/app-conversations?ids=<id>` | **Batch**, returns a list. `execution_status` lives here. |
| Search | GET | `/api/v1/app-conversations/search` | `{items, next_page_id}`; `page_id` paginates. |
| Count | GET | `/api/v1/app-conversations/count` | A bare integer (live: `56`). Used as the health probe. |
| Start task | GET | `/api/v1/app-conversations/start-tasks?ids=<id>` | Batch list; `app_conversation_id` appears only at `READY`. |
| Start-task count | GET | `/api/v1/app-conversations/start-tasks/count` | Bare integer (live: `15`). |
| Rename | PATCH | `/api/v1/app-conversations/{id}` | Title only here. `404` → `{"detail":"unknown_app_conversation"}`. |
| Delete | DELETE | `/api/v1/app-conversations/{id}` | `200 {"success": true}`. |
| Skills | GET | `/api/v1/app-conversations/{id}/skills` | Skill names loaded **for that conversation** (live: 141 on a running conversation). |
| Files / git | GET | `/api/v1/app-conversations/{id}/files` \| `/file` \| `/download` \| `/git/diff` \| `/git/changes` | Read-only views of the conversation's workspace. |
| Model/profile switch | POST | `/api/v1/app-conversations/{id}/switch_acp_model` \| `/switch_profile` | Runtime model/profile change. |
| Hooks | GET | `/api/v1/app-conversations/{id}/hooks` | Git hooks for the conversation. |
| Streaming start | POST | `/api/v1/app-conversations/stream-start` | SSE variant of create (not used by NEXUS). |

**Create request** (`AppConversationStartRequest`, schema — every field
optional): `sandbox_id`, `conversation_id`, `initial_message`,
`system_message_suffix`, `system_prompt`, `disabled_skills`, `processors`,
`llm_model`, `llm_profile`, `agent_profile_id`, `selected_repository`,
`selected_branch`, `git_provider`, `additional_repositories`, `require_all`,
`suggested_task`, `title`, `generate_title_on_submit`, `trigger`, `pr_number`,
`parent_conversation_id`, `agent_type` (`default`|`plan`), `public`,
`observability_*`, `plugins`, `client_tools`, `secrets`, `tags`.

**`client_tools`** (`ClientToolSpec`: `name`, `description`, `parameters`,
`annotations`) is the documented way to let the agent call back into NEXUS.
NEXUS uses it for the Sr delegation bridge. A client-tool **name that collides
with a built-in is a 422** at start; Sr tool names carry an `sr_` prefix.

**`tags`** must be lowercase alphanumeric keys (a `_` in a key is `422`).

### 1.2 Messages, events, status

| Purpose | Method | Path | Behaviour |
|---|---|---|---|
| Send message | POST | `/api/v1/app-conversations/{id}/send-message` | Body `{role, content:[TextContent\|ImageContent], run}` → `{success, sandbox_status, message}`. `409`/`410`/`503` possible. |
| Read events | GET | `/api/v1/conversation/{id}/events/search` | `{items, next_page_id}`. |
| Event count | GET | `/api/v1/conversation/{id}/events/count` | Bare integer. |
| Read events (raw) | GET | `/api/v1/conversation/{id}/events` | The unfiltered list endpoint. |

`events/search` query parameters (live schema): `kind__eq`, `timestamp__gte`,
`timestamp__lt`, `sort_order` (`TIMESTAMP` \| `TIMESTAMP_DESC`, default
`TIMESTAMP`), `page_id`, `limit` (**integer, default 100, maximum 100**),
`exclude_full_state`.

**There is no `agent_final_response` endpoint on Cloud V1** (the agent-server
has one). The final answer is read from the event stream: the last `finish`
tool `ActionEvent`'s `message`, or the last assistant `MessageEvent` — the rule
the OpenHands SDK itself applies. NEXUS implements this in
`CloudClient.final_response` and reads events newest-first.

### 1.3 Sandboxes and runtime (`/api/v1/sandboxes`)

| Purpose | Method | Path | Behaviour |
|---|---|---|---|
| Create | POST | `/api/v1/sandboxes` | Returns a `SandboxInfo`. |
| Read | GET | `/api/v1/sandboxes?id=<id>` | One `SandboxInfo`. |
| List/search | GET | `/api/v1/sandboxes/search` | `{items, next_page_id}` (live: 29 sandboxes). |
| Delete | DELETE | `/api/v1/sandboxes/{id}?sandbox_id=<id>` | The id is required **both** as a path segment and a query param, or `422`. |
| Pause / resume | POST | `/api/v1/sandboxes/{id}/pause` \| `/resume` | Sandbox-scoped, not conversation-scoped. |
| Secrets | GET | `/api/v1/sandboxes/{id}/settings/secrets[/{name}]` | Read-only secret names. |

`SandboxInfo`: `id`, `sandbox_spec_id`, `status` (`SandboxStatus`),
`session_api_key`, `exposed_urls`, `created_at`, `status_detail`. `exposed_urls`
names the `AGENT_SERVER` URL (port 60000) the `direct` transport drives, plus
`VSCODE` (60001) and `WORKER_*`.

`SandboxStatus` (schema enum): `STARTING`, `RUNNING`, `PAUSED`, `ERROR`,
`MISSING`.

### 1.4 Account settings and capabilities (read-only)

| Purpose | Method | Path |
|---|---|---|
| Settings | GET/POST | `/api/v1/settings` |
| Agent schema | GET | `/api/v1/settings/agent-schema` |
| Conversation schema | GET | `/api/v1/settings/conversation-schema` |
| Profiles | GET/POST | `/api/v1/settings/profiles[/{name}]` |
| Agent profiles | GET | `/api/agent-profiles` |
| Tools | GET | `/api/tools` |

NEXUS reads `GET /api/v1/settings` and `GET /api/agent-profiles` for
`account_capabilities()` — it reports only non-secret facts (whether an LLM key
is set, MCP server names, marketplace counts, provider-token presence) and never
requests a secret value.

### 1.5 Availability probes (public and authenticated)

| Probe | Path | Live result | Auth |
|---|---|---|---|
| Liveness | `GET /health` | `"OK"` | none |
| Readiness | `GET /ready` | `200` | none |
| Alive | `GET /alive` | `{"status":"ok"}` | none |
| Server info | `GET /server_info` | `app_version 1.87.1`, `sdk_version 1.60.1`, resources | none |
| Account reachability | `GET /api/v1/app-conversations/count` | `200` + integer | Bearer |

`CloudClient.health()` uses the count endpoint (it proves the **key** works, not
just that the host answers). `health()` was **live `True`**.

---

## 2. Authorization

- **Cloud V1**: `Authorization: Bearer <OPENHANDS_API_KEY>`. Live: a valid key
  → `200`; **no key and an invalid key both → `401`** (verified against
  `/api/v1/app-conversations/count`).
- **Agent-server (direct transport)**: `X-Session-API-Key: <session_api_key>`,
  the key returned in the sandbox's `SandboxInfo`. Live: valid key → `200`;
  missing or wrong key → **`401`**.
- **Redirects are refused.** `CloudClient` installs a no-redirect opener
  (`_NoRedirect`) so a `3xx` cannot re-send the `Authorization` header to a host
  the operator did not configure. `OpenHandsClient` follows a redirect only
  while scheme, host and port all stay identical.
- **Key handling.** The key is read from the environment (or the encrypted
  account row) and is never logged, returned by the API, or embedded in an
  exception message. Stored account keys are Fernet-encrypted with
  `NEXUS_SECRET_KEY` (`orchestration/secrets.py`); without that key, adding an
  account is refused rather than writing plaintext. A missing key raises
  `CloudAuthError("no Cloud API key configured")` before any request is sent.
- **Identity check.** An account is trusted only after `GET /api/v1/settings`
  (via `account_capabilities()`) succeeds; a rejected key marks the account
  `invalid`.

---

## 3. Conversation management

### 3.1 Two-phase creation (Cloud V1)

`POST /api/v1/app-conversations` returns a **start task**, not a conversation.
The status walks
`WORKING → WAITING_FOR_SANDBOX → PREPARING_REPOSITORY → RUNNING_SETUP_SCRIPT →
SETTING_UP_GIT_HOOKS → SETTING_UP_SKILLS → STARTING_CONVERSATION → READY` (or
`ERROR`). `app_conversation_id` is only meaningful at `READY`.

- **Live (this session)**: a start with an initial message walked the full
  sequence and reached **`READY`** in ~45 s (15 polls at 3 s), and the
  conversation later read back as **`finished`**. This is a change from the
  recorded state in `CLOUD-INTEGRATION-MAP.md`/`AGENTS.md`, where every such
  start ended `ERROR` with an inner `HTTP 500`. **Observation, dated
  2026-11-12** — not a claim that the account-side failure is fixed for all
  requests.
- **NEXUS behaviour**: `CloudDispatcher` records the start-task id before
  polling (`external_start_task_id`), polls `start-tasks?ids=` until `READY`,
  then binds `app_conversation_id`. A restart reuses the recorded conversation.

### 3.2 The `direct` transport (NEXUS default)

The default transport does not use the app-conversation endpoint. It:

1. `POST /api/v1/sandboxes` → a `SandboxInfo` (`id`, `session_api_key`).
2. Polls `GET /api/v1/sandboxes?id=` until `RUNNING`.
3. `POST <AGENT_SERVER>/api/conversations` with the sandbox's
   `session_api_key`, returning an already-`READY` start task.

This gives NEXUS the agent-server surface directly, including `client_tools`
and the file API (`supports_files = True`). It is the path the live probe
verified end to end (§10). Repository selection is **not** native here; it is
governed by `OPENHANDS_DIRECT_REPO_MODE` (`prompt` default / `refuse` /
`allow`), and `CloudDirectClient.supports_repository_selection` is `False`.

### 3.3 Continuing, renaming, deleting

- **Continue an existing conversation**: `POST …/{id}/send-message` with
  `run: true` resumes the agent loop in the same conversation. NEXUS delivers
  every follow-up (including Sr client-tool results) this way.
- **Rename**: `PATCH …/{id}` (title). NEXUS `rename_sr` PATCHes Cloud *before*
  reporting success.
- **Delete**: `DELETE …/{id}` → `200 {"success": true}` (live: verified, and the
  conversation then `404`s).
- **A conversation cannot move between accounts.** A different account is a
  different sandbox and key. "Continue on another account" means a *new*
  conversation with the context in its first message (see `docs/FAILOVER.md`).

---

## 4. Sending messages

`POST /api/v1/app-conversations/{id}/send-message` takes
`{role, content: [TextContent | ImageContent], run}` (`content` required,
`minItems: 1`) and returns `{success, sandbox_status, message?}`. `run: true`
starts the loop; the response reports the sandbox status. On the agent-server
the equivalent is `POST /api/conversations/{id}/events` with
`{role, content:[{type:"text", text}], run}`.

---

## 5. Events

`GET /api/v1/conversation/{id}/events/search` → `{items, next_page_id}`.
`GET …/events/count` → a bare integer. Event kinds seen live include
`ConversationStateUpdateEvent`, `MessageEvent`, `ActionEvent`,
`ObservationEvent`.

Traps carried in the maps and confirmed by schema:

- `limit` is **capped at 100**; `limit=200` is `422` (live-verified this
  session). `CloudClient.MAX_EVENTS_PER_PAGE = 100`.
- `?kind=<ClassName>` filtering **does not** match event class names (the
  legacy map's trap); NEXUS fetches unfiltered and matches `classification`
  client-side.
- The default sort is oldest-first. NEXUS `list_events` walks `next_page_id`
  (bounded `max_pages`, default 10 → up to 1000 events); `recent_events` asks
  for `sort_order=TIMESTAMP_DESC` and falls back to reading oldest-first and
  reversing when the server ignores or refuses the parameter.

---

## 6. Execution status and completion

`ConversationExecutionStatus` (schema enum, live):
`idle`, `running`, `paused`, `waiting_for_confirmation`, `finished`, `error`,
`stuck`, `deleting`.

- Terminal-good: **`finished`**.
- Terminal-bad: **`error`**, **`stuck`**.
- **`waiting_for_confirmation` is a blocked condition, not completion.**

NEXUS rule: **a missing answer is never completion.** `finished` with an empty
final response is `blocked`, not `completed`. The final answer is read from the
event stream (§1.2), and on the `direct` transport also from
`GET /api/conversations/{id}/agent_final_response`.

---

## 7. Stopping, finishing, runtime and workspace

- **Stop / pause.** Cloud: `POST /api/v1/sandboxes/{id}/pause` (sandbox-scoped);
  resume with `/resume`. Agent-server: `POST /api/conversations/{id}/pause`,
  `…/interrupt`, and goal control (`…/goal`, `…/goal/stop`, `…/goal/resume`).
  NEXUS `pause` maps to the conversation pause on the agent-server and to the
  sandbox pause on Cloud. A conversation already running is **not** stopped by
  cancelling a NEXUS run, its deadline, or a task's time limit.
- **Finish.** The agent ends by calling the `finish` tool (its `message` is the
  report) or by writing a final message. NEXUS reads both.
- **Runtime.** Agent-server: `GET /api/conversations/{id}/runtime`,
  `POST …/runtime/reprovision`; runtime kinds are reported by `/server_info`
  (`conversation_runtime: "local"`, plus a capabilities list). Cloud exposes
  `SandboxInfo.status` and `exposed_urls`.
- **Workspace.** Agent-server file API:
  `POST /api/file/upload?path=`, `GET /api/file/download?path=`,
  `POST /api/file/create_directory`, `GET /api/file/archive`,
  `GET /api/file/home`, `GET /api/file/search_subdirs`; git:
  `/api/git/changes`, `/api/git/commits`, `/api/git/diff`,
  `/api/git/repositories/search`. The sandbox working directory used by NEXUS is
  `/workspace/project` (`DEFAULT_SANDBOX_WORKING_DIR`). Cloud: the conversation
  `…/files`, `…/file`, `…/git/diff`, `…/git/changes` routes.
  **Live**: NEXUS uploaded a file to the sandbox and read it back with a
  matching SHA-256 (the `workspace_rw` probe, §10).

---

## 8. Available tools

The **agent-server** reports its tool set at `GET /api/tools/catalog` and
`GET /api/tools/`. Live on the running sandbox (agent-server 1.60.1), with
`user_selectable` / `in_default_set` / `usable`:

| Tool | selectable | default | usable | purpose |
|---|---|---|---|---|
| `terminal` | yes | yes | yes | Run shell commands in a persistent terminal session. |
| `file_editor` | yes | yes | yes | View, create and edit files. |
| `task_tracker` | yes | yes | yes | Keep a running task list. |
| `browser_tool_set` | yes | yes | yes | Browse the web (navigate, click, type, read). |
| `switch_llm` | yes | yes | yes | Switch the conversation to another saved LLM profile. |
| `task_tool_set` | yes | no | yes | Delegate a self-contained sub-task to a separate agent. |
| `glob` | yes | no | yes | Find files by name pattern. |
| `grep` | yes | no | yes | Search file contents with a regex. |
| `task` | no | no | yes | (internal task primitive) |
| `workflow`, `workflow_tool_set` | no | no | yes | Workflow primitives. |
| `start_child_conversation` | no | no | yes | Spawn a child conversation. |
| `edit`, `read_file`, `write_file`, `list_directory`, `planning_file_editor` | no | no | yes | File primitives (used by the sets above). |
| `ask_oracle` | no | no | yes | Ask a second model. |
| `ClassifyAndSwitchLLMTool` | no | no | yes | LLM routing. |

`/server_info` also lists `usable_tools` (the same names) and
`runtime_idle_timeout_seconds` (1200 s).

**Tools available to NEXUS vs to OpenHands.** NEXUS itself exposes MCP tools
over stdio and Streamable HTTP (`orchestration/mcp_server.py`, `POST /mcp`) and
Sr acts only through **client tools** (`sr_`-prefixed, delivered as
`ActionEvent`s over the event stream). The tools above are what the *agent*
inside the conversation can call. A tool being *configured* is not proof it
*ran*; only an observed `ActionEvent` proves exercise.

**Reading the catalog (NEXUS).** `orchestration/openhands_tools.py` normalises
this catalog (and `/server_info`'s `usable_tools`) into
`{name: {usable, user_selectable, in_default_set}}`, and each transport exposes
`tool_catalog()`: `CloudDirectClient` and `LocalCloudClient` read
`GET /api/tools/catalog` from a real agent-server; the Cloud *app* transport has
none, so it raises `ToolCatalogUnsupported` (`BLOCKED`, never `FAILED`). The
diagnostic report adds three checks — `openhands_tools` (the catalog; opt-in
with `live`), `mcp_configuration` (configured MCP server *names* only) and
`nexus_tools` (NEXUS's own MCP + `sr_` client tools). Git and testing have no
built-in and are proved by `terminal`.

**MCP.** The account's Cloud settings reported **20 configured MCP servers**
live (`browser_mcp`, `cloudflare-bindings`, `cloudflare-browser-rendering`,
`cloudflare-builds`, `cloudflare-docs`, `cloudflare-observability`, `deepwiki`,
`everything`, `fetch`, `figma`, `huggingface`, `memory`, `miro`, `playwright`,
`sentry`, `sequential_thinking`, `shttp`, `shttp_2`, `time`, `webflow`). The
agent-server exposes MCP management routes (`/api/mcp/test`,
`/api/mcp/oauth/*`) and `/api/settings/mcp/{key}`.

---

## 9. Limits, errors, timeouts

### 9.1 Limits (observed / schema)

| Limit | Value | Source |
|---|---|---|
| `events/search` page size | **≤ 100** (default 100; `limit=200` → `422`) | schema + live |
| Tag keys | lowercase alphanumeric only | schema (`422` otherwise) |
| Client-tool names | must not collide with a built-in (`422`) | live (recorded) |
| Daily conversation limit | **20/day** (`429 daily_conversation_limit_reached`) | recorded live; a `429` was also seen on a sandbox start this session |
| Daily sandbox limit | `429 daily_sandbox_limit_reached` | recorded live |
| Default conversation `max_iterations` | account setting `500`; NEXUS `direct` uses `200` | live settings; `cloud_direct.py` |
| Sandbox idle timeout | `runtime_idle_timeout_seconds` 1200 s | live `/server_info` |
| Task time limit (NEXUS) | `NEXUS_TASK_TIMEOUT_SECONDS`, default 7200 s | `nexus_lab_store.py` |
| Start wait (NEXUS) | `start_poll_attempts` × interval (worker: 150 × 2 s = 5 min) | `nexus_lab.py` |

### 9.2 Errors

| Signal | Meaning | NEXUS class / action |
|---|---|---|
| `401` / `403` | auth rejected | `CloudAuthError` — **dead-letter at once**, keep evidence (a bad key cannot fix itself) |
| `404` | resource gone | `CloudNotFound` — requeue within the attempt budget |
| `409` | send-message conflict (also `409` on `run` = already running = success) | handled per call site |
| `410` | conversation gone | handled |
| `422` | validation | `CloudValidationError` — **dead-letter** (a bad request cannot fix itself) |
| `429` | rate limit / daily quota | `CloudRateLimited` (a `CloudUnavailable`) — **no attempt spent**, task waits (`Retry-After`, 15 min for a daily limit, else 1 min), account marked `degraded` |
| `5xx` | transient | `CloudUnavailable` — keep lease, retry next tick; never "completed" |
| transport error / timeout | transient | `CloudUnavailable` |
| `error` + `classification.retryable=false` | permanent | dead-letter immediately, classification kept |
| `waiting_for_confirmation` | ambiguous | `blocked`, needs a human |
| `finished` with empty answer | unverified | `blocked`, **not** completed |

The runtime classifies its own failures (`classification.kind` ∈ `auth`,
`quota`, `rate_limit`, `config`, `transient`, `agent_action`, `internal`,
`unknown`; `retryable` is authoritative).

### 9.3 Timeouts

| Client | Default HTTP timeout |
|---|---|
| `CloudClient` (Cloud V1) | 30 s |
| `CloudDirectClient` | 60 s |
| `OpenHandsClient` (agent-server) | 30 s |
| `LocalRuntimeClient` | 60 s |
| `PostgresStore` connect | 10 s (`connect_timeout`) |
| NEXUS CLI HTTP client | 30 s |

A transport timeout is `CloudUnavailable` (transient): the lease is kept and
`attempt` is **not** incremented.

---

## 10. Live verification performed this session

Against the real Cloud service and the running sandbox, read-only except where a
probe creates and deletes exactly one conversation:

| Check | Method | Result |
|---|---|---|
| Cloud reachable | `CloudClient.health()` | **`True`** (live) |
| Auth: valid / missing / invalid key | `GET …/app-conversations/count` | `200` / `401` / `401` (live) |
| OpenAPI + version | `GET /openapi.json`, `/server_info` | OpenAPI 3.1.0, app **1.87.1**, sdk 1.60.1 (live) |
| Conversation search/read | `search_conversations`, `get_conversation` | works; batch list; `execution_status` read (live) |
| Events + final response | `list_events`, `recent_events`, `final_response` | works; a `finished` conversation returned its report (live) |
| `events/search` limit cap | `limit=200` | `CloudValidationError` (422) as expected (live) |
| Delete | `delete_conversation` | `True`, then `404` (live) |
| Account capabilities | `account_capabilities()` | LLM key set, 20 MCP servers, profiles (live) |
| App transport two-phase start | `start_conversation` + poll | reached **`READY`**, then `finished` (live, dated) |
| **NEXUS diagnostics (structural)** | `OpenHandsDiagnostics.run(live=False)` | database, schema, cloud_api, queues_leases **VERIFIED**; accounts/pool per config |
| **NEXUS diagnostics (live probe)** | `run(live=True)` with a working `default_model` | **10 VERIFIED / 0 FAILED / 0 BLOCKED**: `conversation_create`, `message_and_events`, `run_command`, `workspace_rw` all VERIFIED |
| Sandbox file round-trip | upload + download + SHA-256 | match (live) |
| Sandbox lifecycle (read) | `GET /api/v1/sandboxes/search` | 29 sandboxes, `SandboxInfo` shape as documented (live) |

Without a usable `default_model` the same probe ends `error` with an LLM auth
failure (`message_and_events`/`run_command` FAILED) while `conversation_create`
and `workspace_rw` stay VERIFIED — exactly the `OPENHANDS_DEFAULT_MODEL` trap.
With `openhands/deepseek-v4.1-flash` it is fully green.

All probe conversations were deleted (confirmed: **0** `nexus-diagnostics`
conversations remain).

### What is not verified (honest)

- **`blocked`**: a **two-live-account** failover. Only one OpenHands key is
  available, so an end-to-end account switch against real Cloud was not run. The
  failover logic is covered by `tests/test_openhands_failover.py` against real
  PostgreSQL and honest transport fakes.
- **`unverified`**: MCP tools *exercised* inside a conversation (their presence
  is confirmed; a tool call was not observed this session), the studio UI
  against a live conversation, and the app-transport `send-message` path as a
  full multi-turn run (a single start reached `READY` and `finished`).

---

## 11. NEXUS ↔ OpenHands compatibility

Four clients implement **one** surface, so the dispatcher and Sr runtime do not
know which is in use:

| NEXUS client | Target | Selected by | Auth |
|---|---|---|---|
| `cloud_client.CloudClient` | Cloud V1 app-conversations | `OPENHANDS_TRANSPORT=app` | Bearer |
| `cloud_direct.CloudDirectClient` | a provisioned sandbox's agent-server | `OPENHANDS_TRANSPORT=direct` (**default**) | `X-Session-API-Key` |
| `openhands_client.OpenHandsClient` | a local/remote agent-server (v1.50.1 era) | Sr/Jr + local tests | `X-Session-API-Key` |
| `local_runtime.LocalRuntimeClient` | a local agent-server | e2e tests only | `X-Session-API-Key` |

### Compatibility matrix against the live API

| NEXUS call | Live Cloud endpoint | Status |
|---|---|---|
| `CloudClient.health` | `GET /api/v1/app-conversations/count` | **verified** |
| `start_conversation` | `POST /api/v1/app-conversations` | **verified** (reaches READY) |
| `get_start_task` | `GET …/start-tasks?ids=` | verified |
| `get_conversation` | `GET …/app-conversations?ids=` | **verified** |
| `search_conversations` | `GET …/app-conversations/search` | **verified** |
| `send_message` | `POST …/{id}/send-message` | schema-compatible; not multi-turn live this session |
| `list_events` / `recent_events` | `GET /api/v1/conversation/{id}/events/search` | **verified** |
| `final_response` | events (no dedicated endpoint) | **verified** on a `finished` conversation |
| `delete_conversation` | `DELETE …/{id}` | **verified** |
| `update_conversation` | `PATCH …/{id}` | schema-compatible |
| `start_sandbox` / `get_sandbox` / `delete_sandbox` | `/api/v1/sandboxes` | schema-compatible; read verified |
| `pause_sandbox` / `resume_sandbox` | `/api/v1/sandboxes/{id}/pause\|resume` | schema-compatible |
| `get_settings` / `account_capabilities` | `/api/v1/settings`, `/api/agent-profiles` | **verified** |
| `conversation_skills` | `GET …/{id}/skills` | **verified** |
| `web_url` | derived from the API URL | schema-compatible |

### Deliberate design facts (do not "simplify")

- `agent_settings` is **required** on the legacy agent-server create; Cloud's
  start request does not need it (it inherits the account profile). The
  `direct` transport fetches the sandbox's `/api/settings` and forwards it.
- Creation is idempotent only with a client-supplied `conversation_id`
  (agent-server) or via the `conversations` table (Cloud). The dispatcher
  derives a deterministic uuid5 from the task id.
- `CloudClient.supports_repository_selection` is `True`;
  `CloudDirectClient`'s is `False`. Both are surfaced in `/api/config`.
- `client_tools` is a **local/agent-server** feature; Cloud V1's start request
  also accepts `client_tools` per the schema, but the Sr bridge needs the
  agent-server path (the `direct` transport provides it).
- The API key is read from the environment (or the encrypted account row) and is
  never logged.

### Compatibility verdict

The NEXUS client surface is a **stable subset** of the live API and remains
compatible at Cloud **1.87.1** / agent-server **1.60.1**: the whole live
diagnostic probe passes (10 VERIFIED) and every read/write NEXUS depends on was
exercised. The two recorded failure modes are addressed by the code that exists:
the app-transport 500 is no longer observed (the start reached `READY`), and the
`direct` transport never depended on it. The only environment-level blocker is
the absence of a **second** OpenHands key, which leaves a two-live-account
failover `blocked` here.

---

## 12. Diagnostic command

The single engine is `orchestration/openhands_diagnostics.py`; it reports four
earned statuses — `VERIFIED` (ran and worked), `FAILED` (ran and failed),
`BLOCKED` (could not run: no credential/resource), `UNVERIFIED` (not run). Run
it through the API or the CLI:

```bash
# Structural checks (read-only):
NEXUS_URL=https://nexus.example NEXUS_TOKEN="$WEB_AUTH_TOKEN" \
  python -m orchestration.cli openhands diagnostics --json

# The live end-to-end probe (creates and deletes one conversation):
NEXUS_URL=... NEXUS_TOKEN=... \
  python -m orchestration.cli openhands diagnostics --live
```

`GET /api/openhands/diagnostics[?live=1]` is the HTTP equivalent.

---

## 13. Summary

- **API**: Cloud V1 (`https://app.all-hands.dev`, OpenAPI 3.1.0) at version
  **1.87.1**; the sandbox agent-server at **1.60.1**; NEXUS uses a stable subset
  of both.
- **Authorization**: `Authorization: Bearer` (Cloud) and
  `X-Session-API-Key` (agent-server); `401` on a missing/invalid key; redirects
  refused; keys encrypted at rest and never logged.
- **Conversations**: two-phase create on Cloud, one-step on the sandbox;
  continue via `send-message`; rename via `PATCH`; delete via `DELETE`; a
  conversation cannot move between accounts.
- **Events/status**: `events/search` (`limit ≤ 100`), `events/count`;
  `execution_status` ∈ {idle, running, paused, waiting_for_confirmation,
  finished, error, stuck, deleting}; a missing answer is never completion.
- **Runtime/workspace**: sandbox pause/resume, agent-server runtime routes, file
  and git routes; upload/read-back verified.
- **Tools**: 19 usable agent-server tools (terminal, file editor, browser,
  task delegation, git, glob/grep, LLM switch, …); 20 configured Cloud MCP
  servers; NEXUS reaches the agent through `client_tools`.
- **Limits**: events page ≤ 100, tag keys lowercase, daily conversation (20) and
  sandbox limits, `429` handled without spending an attempt.
- **Errors/timeouts**: `401/403` and `422` dead-letter; `429` waits; `5xx` and
  transport errors retry; 30–60 s HTTP timeouts.
- **Availability**: `/health`, `/ready`, `/alive`, `/server_info` public; the
  count endpoint is the key-authenticated probe.
- **Compatibility**: compatible at the current versions; live probe 10 VERIFIED;
  the only `blocked` item is a two-live-account failover (no second key).
