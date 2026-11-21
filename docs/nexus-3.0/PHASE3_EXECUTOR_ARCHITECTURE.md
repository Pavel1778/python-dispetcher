# PHASE 3 EXECUTOR ARCHITECTURE — design only

**Date:** 2026-11-20 · **Status: NOT IMPLEMENTED (design)** — and deliberately so:
the mission says "Do not implement a fake production executor."

This document records the *real* OpenHands contracts that a Rust executor must
reproduce, taken from the working Python implementation, and the architecture
that follows from them.

---

## 1. The contracts that must be reproduced

Read from `orchestration/cloud_client.py`, `cloud_direct.py`, `local_runtime.py`,
`openhands_router.py`, `openhands_pool.py` and `nexus_lab.py`.

### 1.1 Two transports, one client surface

The dispatcher does not know which transport it is using
(`build_cloud_client` picks from `OPENHANDS_TRANSPORT`):

| Transport | How a conversation is started | Two-phase start |
| --- | --- | --- |
| `app` (Cloud V1) | `POST /api/v1/app-conversations` (Bearer) | **yes** — returns a start task; `app_conversation_id` appears only at `READY` |
| `direct` | provision a sandbox, then `POST /api/conversations` on its agent-server (`X-Session-API-Key`) | no start task for the local agent-server; the id is immediate |

A Rust executor must implement the `Executor` trait against **both**, because a
deployment may switch.

### 1.2 What `start_conversation` accepts — and what it does not

Accepted fields (from the Python adapter, which mirrors the documented schema):
`initial_message`, `selected_repository`, `selected_branch`, `title`,
`parent_conversation_id`, `tags`, `secrets`, `client_tools`,
`agent_profile_id`, `agent_type`, `plugins`.

**Not accepted: any idempotency key.** This is why the delivery guarantee is
at-least-once (see `DISPATCH_RECOVERY_PROTOCOL.md`).

### 1.3 Polling and the answer

- `events/search` **caps `limit` at 100** (a larger value is `422`);
- `GET …/conversations/{id}/events` needs a body and returns 422 otherwise;
- `?kind=ConversationErrorEvent` returns nothing even when the event exists — the
  client matches `classification` itself;
- the agent's final answer is either a `MessageEvent` or a `finish` tool
  `ActionEvent`, **whichever is newest** (the SDK's own rule);
- `execution_status` `finished` is success; `error`/`stuck` are failure **even
  if a long final answer is present**; an **empty** status means "still at work",
  never "settled".

A Rust executor that treats an empty status as finished, or reads the answer for
a non-`finished` state, would produce false completions. These five rules are the
core of the contract.

### 1.4 Account pool

`AccountPool` selects among stored accounts in four modes (`manual`, `auto`,
`failover`, `load_balancing`). "Usable" = `active` AND `enabled` AND health not
`unavailable`; a `degraded` account stays selectable. Load is **derived** from
in-flight task rows, not from a counter.

A Cloud conversation **cannot move between accounts**: a switch is a *new*
conversation on the new account, with the context passed in the first message
(`handoff_message`). The old conversation is never polled again.

This is a real architectural constraint that a Rust executor must respect, not
an implementation detail.

---

## 2. Proposed architecture

```
nexus-executor (trait)            implemented by
  start(task) -> Dispatch                app_transport   direct_transport
  poll(task, conversation) -> Report     (fake, for tests)
  stop(conversation) -> bool
  follow_start(start_task) -> Option<Conversation>
  remove(conversation) -> bool
```

- **Leave the state machine in `nexus-storage`.** The executor reports facts
  (`started`, `finished`, `error`, `stuck`, `uncertain`); it never decides a
  task's verdict. That is the Python design and it is what keeps a false
  completion impossible.
- **An `uncertain` outcome is a first-class result.** When a start's response is
  lost, the executor returns `Uncertain` and the caller reconciles by tag — never
  `Err` that a retry loop would turn into a second start.
- **Multiple accounts from the start.** Even with one key configured, the
  selection layer and the hand-off path must exist, so a second key is a
  configuration change, not a code change. Do **not** treat single-key operation
  as proof of multi-account failover.
- **Transport fakes must reproduce the real contract**, including the
  `limit=100` cap, the two-phase start (id withheld until READY), and the empty
  status. Fakes that are easier than reality hide exactly the bugs that matter.

---

## 3. What is blocking this phase

| Requirement | Status |
| --- | --- |
| An `Executor` trait in Rust | **not written** |
| A Cloud V1 transport in Rust | **not written** |
| A direct (sandbox) transport in Rust | **not written** |
| Account pool / router in Rust | **not written** |
| Live acceptance against real OpenHands | **not attempted** — no key is used by CI, and the mission forbids spending account resources without authorisation |

The Python executor implements all of the above and is the reference. A Rust
executor must match it, and its test suite must include the five polling rules
from §1.3 as explicit cases.
