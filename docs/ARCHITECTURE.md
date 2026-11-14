# NEXUS Architecture

NEXUS is the durable control plane for orchestrating OpenHands agent
conversations. It owns task state, leases, assignment and recovery; it does not
own the runtime — conversations execute in the OpenHands agent-server (Cloud).

This document is the map: what the components are, what each is responsible
for, and how they interact. For the invariants each module must keep, read
`AGENTS.md`; for the request paths, read `README.md`; for the operator view,
read `docs/OPERATIONS.md`.

## 1. The whole system

```
                 browser (studio)            machine client (CLI / MCP)
                        │                              │
                        ▼                              ▼
        ┌───────────────────────────────────────────────────────────┐
        │  orchestration/web_server.py  (stdlib HTTP, one process)    │
        │   auth (session + bearer) · routes · the lab loop thread    │
        └───────────────────────────────────────────────────────────┘
             │                 │                 │              │
             ▼                 ▼                 ▼              ▼
     nexus_intent      nexus_conversations   nexus_lab       providers
     nexus_commands    (chat, messages,      (scheduler,     (Gemini →
     (router)           pins, generations)    executor,       Groq →
                                             verifier)       OpenRouter →
             │                 │                 │           OpenHands)
             ▼                 ▼                 ▼
        nexus_specs       nexus_generation   nexus_lab_store ── cloud_store
        nexus_spec_planner  (stop token)      (runs, tasks)     (PostgreSQL)
             │                                   │
             ▼                                   ▼
        providers.py                     openhands_router / pool
        (LLM planning)                   (account selection, failover)
                                                 │
                                                 ▼
                                     CloudClient / CloudDirectClient
                                     (OpenHands Cloud + sandbox agent-server)
```

State lives in PostgreSQL. Conversations run on OpenHands Cloud. The `direct`
transport provisions a sandbox and drives its agent-server; the `app`
transport uses the legacy app-conversation endpoint. `build_cloud_client`
picks between them from `OPENHANDS_TRANSPORT` (default `direct`).

## 2. Layers and responsibilities

### 2.1 HTTP surface — `web_server.py`

One `ThreadingHTTPServer` handler. It owns:

- **Authentication**: a browser session (PBKDF2 + server-side sessions +
  double-submit CSRF, `web_auth.py`) and a machine bearer token. Every route
  below `/` requires one of them (`_require_read` / `_require_write`).
- **Routing**: a long `do_GET` / `do_POST` / `do_PATCH` / `do_DELETE`, keyed by
  path. Project-scoped routes resolve `{project_id}` and authorize membership;
  a non-member gets 404, never 403.
- **The chat turn** (`_chat_turn`): the single place a message becomes a reply,
  a plan or a run. It is the integration point for the command parser, the
  intent router, the conversation service, the generation registry and the
  dispatch hooks.
- **The lab loop** (`App._run_lab`): a thread that advances autonomous runs,
  unless `NEXUS_LAB_INLINE` switches it off (then `lab_worker.py` does it).
- **The MCP Streamable HTTP endpoint** (`POST /mcp`).

### 2.2 The chat decision path

Order matters, and each step is a separate, tested module:

1. **`nexus_commands.parse`** reads slash commands, hashtags and typed
   parameters into a pure `Parsed` decision. `#read-only` downgrades an execute
   command; an impossible restriction is refused. A UI button posts
   `{"action": "/plan"}` and goes through the same parser.
2. **`nexus_intent.classify`** reads the *message* (a question is never an
   order; an order is only an order when it names work). `effective_mode` turns
   the intent into the mode the turn runs in. `_continues_analysis` keeps a
   bare follow-up question on the run state the previous answer read.
3. **`nexus_chat.ChatResponder.respond`** produces the reply: `ask`/`analysis`
   answer in prose (reading the repository through `nexus_repo_reader`),
   `plan` returns a validated plan, `code`/`autonomous` call the dispatch hooks.
4. **`nexus_conversations`** stores the turn, the title, the pins, the
   generations and the workflow state.
5. **`nexus_generation`** carries a cancellable token for an answer in flight;
   "stop generation" trips it and aborts the live provider call.

### 2.3 The autonomous pipeline

`nexus_specs` (the task text, any size) → `nexus_spec_planner` (a plan, in
parts if large) → `nexus_lab_store.start_run` (the run and its DAG) →
`nexus_lab.LabScheduler` (reclaim → claim → dispatch → poll → verify → record →
decide) → `nexus_lab.OpenHandsExecutor` (dispatch and poll; never decides
completion) → `nexus_lab.GitVerifier` (the real remote branch head) →
`nexus_evidence` (the signed record) → `nexus_reports` (the report, delivered
to the chat).

`openhands_router` and `openhands_pool` choose which OpenHands account runs a
task and hand off context on a switch. `openhands_diagnostics` proves each
integration with an earned status.

### 2.4 Storage — `cloud_store.py` and `nexus_lab_store.py`

`PostgresStore` holds one connection, reconnects on evidence, and applies
migrations (refusing an edited applied migration). `nexus_lab_store.LabStore`
owns the run/task state machine, atomic claim with a lease and a fencing token,
dependency gating (`completed_verified` only), branch exclusivity and recovery.

### 2.5 Front end — `orchestration/studio/`

Static files (no framework, no build step). `web_ui.py` serves a closed mapping
of URL → bytes. Events are delegated from `document` through `data-action` and
the `ACTIONS` table, so a missing element never aborts the interface. Untrusted
text reaches `innerHTML` only through `markdown.js` / `highlight.js`, which
escape by construction.

### 2.6 Security

`nexus_policy` (SSRF/redaction), `net_guard` (outbound URL check),
`secrets.py` (Fernet at rest), `archive_safety.py` (no member name ever
becomes a path), `web_auth.py` (sessions, CSRF, throttling),
`nexus_binding.authorize_dispatch` (the single dispatch gate).

## 3. Data model (the load-bearing tables)

| Area | Tables |
| --- | --- |
| Dispatcher | `workflows`, `tasks`, `conversations`, `task_events`, `sr_tool_calls` |
| Auth | `sessions`, `login_attempts` |
| Projects | `nexus_projects`, `nexus_project_members`, `nexus_project_repositories`, `nexus_project_executors`, `nexus_project_events` |
| Chat | `nexus_conversations`, `nexus_conversation_messages`, `nexus_conversation_attachments`, `nexus_agent_sessions`, `nexus_generations` |
| Runs | `nexus_runs`, `nexus_run_tasks`, `nexus_run_reports`, `nexus_executor_stops`, `nexus_task_executions`, `nexus_task_handoffs`, `nexus_conversation_cleanups` |
| Specs/plans | `nexus_goals`, `nexus_specs`, `nexus_spec_sections`, `nexus_spec_requirements`, `nexus_spec_links`, `nexus_spec_stages`, `nexus_plans` |
| Evidence | `nexus_evidence` |
| Context/memory | `nexus_context`, `nexus_memory` |
| Files | `nexus_files`, `nexus_workspaces`, `nexus_workspace_files`, `nexus_voice_notes` |
| Accounts | `nexus_github_accounts`, `nexus_openhands_accounts`, `nexus_openhands_account_events`, `nexus_openhands_pool`, `nexus_cloud_sandboxes` |
| Workers | `nexus_lab_workers` |
| Actions | `nexus_pending_actions` |

Migrations are `0001`–`0011`, `0014`–`0037`. `0012`/`0013` are reserved. The
runner applies every `*.sql` in lexical order.

## 4. Request lifecycle (one autonomous run)

1. `POST …/conversations/{id}/messages` with a work order.
2. `_chat_turn` parses commands, classifies intent, picks `autonomous`.
3. The text is stored as a specification (`nexus_specs`), planned
   (`nexus_spec_planner`, inline or in the background), and `start_run` creates
   the run and its tasks in one transaction.
4. `LabScheduler.tick` (from the loop thread or `lab_worker`) claims a task
   (lease + fencing token), dispatches it to OpenHands (the account the pool
   chose), and records the start durably.
5. On the next passes it polls the conversation, verifies the real commit with
   `GitVerifier`, records evidence, and decides the run's fate.
6. A completed run's report is written and delivered to the originating chat;
   its OpenHands conversations are queued for deletion.

## 5. What is not in this process

- The runtime (OpenHands) — reached over HTTP.
- The database — PostgreSQL, reached over one connection.
- The LLM providers — reached over HTTP through the cascade.

Nothing else is required to run NEXUS; see `DEPLOY.md` and `docs/OPERATIONS.md`.
