# OPENHANDS EXECUTION ACCEPTANCE — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: VERIFIED WITH CONDITIONS.** Real OpenHands executed real work through
NEXUS in this session (17 diagnostic checks VERIFIED, including a conversation
that ran a shell command and round-tripped a workspace file). A push-verified
repository change is **BLOCKED** on this account (no GitHub provider token).

---

## 1. What actually ran (not a mock)

`OpenHandsDiagnostics.run('machine:default', live=True)` against the live Cloud
(`https://app.all-hands.dev`), `direct` transport, stored account with
`openhands/deepseek-v4.1-flash`:

```
VERIFIED  database            postgres round-trip ok
VERIFIED  schema              all required tables and columns present
VERIFIED  accounts            1 account(s) probed
VERIFIED  account_pool        mode auto selects live-verify
VERIFIED  cloud_api           reachable
VERIFIED  queues_leases       claim → release → re-claim round trip ok
VERIFIED  recovery            expired lease reclaimed with a higher fencing token
UNVERIFIED lab_worker         no live worker, but no active run needs one
VERIFIED  providers           configured: openhands
VERIFIED  provider_probe      1 provider(s) probed: openhands ok
VERIFIED  openhands_tools     25 tool(s): terminal, workspace, git, testing
VERIFIED  mcp_configuration   21 MCP server(s) configured (configuration only)
VERIFIED  nexus_tools         19 MCP + 7 client tools
VERIFIED  conversation_create conversation created
VERIFIED  message_and_events  message delivered; events read; conversation finished
VERIFIED  run_command         the agent echoed the marker
VERIFIED  task_completion     conversation reached the terminal state 'finished'
VERIFIED  workspace_rw        file uploaded and read back with a matching SHA-256
OK: True
```

Each `VERIFIED` here is a real HTTP call to the live service:
`conversation_create` provisions a sandbox and creates a conversation;
`message_and_events` sends a message and reads the event stream; `run_command`
asks the agent to run a shell command and checks the echo; `task_completion`
waits for the terminal state `finished`; `workspace_rw` uploads a file and reads
it back with a matching SHA-256. The probe creates and deletes exactly one
conversation.

## 2. The lifecycle this proves

```
user prompt → NEXUS project → task → assignment → OpenHands session
→ model configuration → agent execution → tool usage → result
→ evidence → report
```

| Stage | Proven by |
| --- | --- |
| Session creation with the stored model | `conversation_create` VERIFIED + Defect-2 fix |
| Agent receives the task | `message_and_events` VERIFIED |
| Agent uses a tool (shell) | `run_command` VERIFIED |
| Agent touches the workspace | `workspace_rw` VERIFIED |
| Conversation reaches `finished` | `task_completion` VERIFIED |
| Evidence recorded | `tests/test_lab_evidence.py` + live run wrote a record |
| Truthful report | a text answer naming no commit → `completed_unverified` |

## 3. What is BLOCKED, and why (not a defect)

A **push-verified repository change** (`completed_verified`) needs the OpenHands
account to have a GitHub provider token so its sandbox can clone and push. The
live account reports `provider_tokens_set: {'github': False}`. Therefore:

- A real autonomous run on the live service dispatches, the agent works, and
  NEXUS records **`completed_unverified`** ("no repository bound") and blocks the
  run — the correct, honest outcome. A text answer is never accepted as proof.
- This is a **capability** limit of the account, not a NEXUS defect.

Reproduce the BLOCKED gate:

```bash
# with a real key and no GitHub provider token on the account
python -m pytest tests/test_comprehensive_suite.py -k section_f -q
# -> skipped: this environment has no working push-capable OpenHands account
```

## 4. What would make it VERIFIED

1. An OpenHands account with a GitHub provider token the sandbox can push with,
   bound to a repository in the project (`connect_repository`).
2. A real small change: e.g. add a line to a README and push to a task branch.
3. `GitVerifier` reads the branch head and accepts the named commit →
   `completed_verified`; the evidence record signs `remote_sha`.

The pieces exist and are tested against the fakes + PostgreSQL
(`tests/test_openhands_failover.py`, `tests/test_pipeline_e2e.py`,
`tests/test_comprehensive_suite.py`). Only the live credential is missing.

## 5. Model/Base URL in the executed session

The session above used the **stored** account's model
(`openhands/deepseek-v4.1-flash`) and base URL, not an environment default —
which is exactly what Defects 1 and 2 (see
`OPENHANDS_CONFIGURATION_AND_PERSISTENCE.md`) made reliable. Without the stored
model a bare sandbox ends `error` (LLM auth), the documented trap.
