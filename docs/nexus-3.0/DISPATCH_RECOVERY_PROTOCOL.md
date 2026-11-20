# DISPATCH RECOVERY PROTOCOL — the Python dispatch path

**Date:** 2026-11-20 · **Status: VERIFIED for Python · BLOCKED for Rust**

The brief asks for a durable external-execution protocol and a crash-point
analysis A–G. The Python implementation already has one, and it was tested.
There is no Rust worker or dispatcher to analyse (see `FORENSIC_BASELINE.md`).

---

## 1. The honest delivery guarantee

**This is at-least-once dispatch with tag-based deduplication and reconciliation
— not exactly-once.**

Why exactly-once is impossible here: `CloudClient.start_conversation` posts
`POST /api/v1/app-conversations` with a body that has **no idempotency-key
field** (`cloud_client.py:332-400`; the fields are `initial_message`,
`selected_repository`, `selected_branch`, `title`, `parent_conversation_id`,
`tags`, `secrets`, `client_tools`, `agent_profile_id`, `agent_type`, `plugins`).
A request whose response is lost therefore has an **unknown** outcome: the
conversation may or may not exist.

The protocol does not pretend otherwise. It:

1. tags every start with `nexusrun`, `nexustask`, `nexusattempt`
   (`OpenHandsExecutor._tags`);
2. before starting anything for a reclaimed task, **searches for an existing
   conversation with those tags and adopts it** (`_adopt`);
3. only starts a new one when no orphan exists.

So a lost response leads to an *adopted* conversation, not a second one.

---

## 2. States

The brief's model (`pending → dispatching → dispatched → running → collecting →
settled`) maps onto what exists:

| Brief state | Actual field(s) | Notes |
| --- | --- | --- |
| pending | `nexus_run_tasks.state='pending'` | claimable when due and deps verified |
| dispatching | *implicit* — claim taken, lease held, no conversation yet | represented by `state='dispatched'` + `conversation_id IS NULL` + a live `lease_expires` |
| dispatched | `pending_start` jsonb (the accepted start task) | the start exists on the service, the conversation does not yet |
| running | `state='running'` + `conversation_id` | bound |
| collecting | `in_flight_tasks` / poll | result being read |
| settled | `completed_verified` / `completed_unverified` / `failed` / `blocked` | verdict recorded + evidence |

**There is no explicit `dispatching` row state.** The brief's state is
represented by the *combination* above rather than a distinct value, and that is
deliberate: `mark_running` sets `conversation_id` and moves `dispatched` →
`running` in **one statement**, so a crash cannot leave a "bound but not running"
row. Adding a separate `dispatching` state would introduce exactly the window
that is avoided today.

---

## 3. Crash points A–G

Each row states what is persisted, what is retried, what is reconciled, and
whether a conversation can be duplicated. All are tested in
`tests/test_dispatch_durability.py` (9 cases) and, for the later points,
`tests/test_start_handover.py` (9 cases).

| Point | Persisted | Retried? | Reconciled? | Duplicate external conversation? |
| --- | --- | --- | --- | --- |
| **A. Before the intent/request leaves** | claim + lease only | yes, a fresh start is correct | n/a — nothing exists | **no** — nothing was created |
| **B. After intent commit, before the HTTP request** (intent = the claim/lease) | claim + lease | yes | n/a | **no** |
| **C. Request reached OpenHands, response lost** | claim + lease; conversation exists on the service; **no** `pending_start` | **no blind retry** | **yes** — `_adopt` finds it by tag | **no**, provided the tag search succeeds |
| **D. Conversation id returned, not yet persisted** | as C | no | yes — same adoption path | no |
| **E. Agent finished, result not ingested** | conversation id bound, task `running` | poll again on the next pass (stateless) | yes — any worker can poll by id | **no** — a bound conversation is not restarted |
| **F. Evidence collected, not settled** | verdict in memory only; task still `running`/`verifying` | re-poll and re-verify | yes (idempotent verification) | no |
| **G. Settled, notification not sent** | task verdict + evidence durable | the report outbox redelivers (`deliver_due`) | yes | no |

### The one window that is *not* fully closed

If the tag search itself fails (service unavailable) at point C, `_adopt` logs
and returns `None`, and the executor **starts a new conversation**. That is a
possible duplicate. It is a deliberate trade-off — refusing to start would stall
the task for ever — and it is the reason the guarantee is stated as at-least-once
with deduplication, not exactly-once. A future fix is: persist the intent *with a
locally-generated correlation id* before the request, and on a search failure
park the task in an `uncertain` state for reconciliation rather than starting.

**That fix is not implemented.** Recorded as a known gap.

---

## 4. Stale workers

Every write after the claim is fenced on `lease_token`:

- `record_start`, `mark_running`, `record_verification`, `release_task`,
  `postpone_task`, `requeue_for_failover`, `retry_task`, `wait_task` take a
  `lease_token` and add `AND lease_token=%s` to the `UPDATE`;
- `LabStore.holds_lease(task_id, token)` is checked by the scheduler immediately
  before starting a conversation;
- a stale holder's write matches no row and is a no-op (not an error).

Proven by `tests/test_race_protection.py` (13 cases) and
`test_a_stale_lease_cannot_bind_the_conversation_it_started`.

---

## 5. What Rust must reproduce

If a Rust dispatcher is ever built, it must implement all of the above:
the same tag scheme, adoption-before-start, the single-statement bind, and the
`lease_token` fence on every write. A Rust dispatcher that starts before
searching would create duplicates at point C — the exact failure this protocol
exists to prevent.

**No Rust dispatcher exists**, so this is a design requirement, not a verified
property.
