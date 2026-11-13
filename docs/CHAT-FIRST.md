# Chat-first: Auto, Manual and Chat

NEXUS is an intelligent development environment and a technical interlocutor.
It does not run a job because a message contained a verb, and it does not ask
permission for everything either. Three ways of working sit on one router.

## The one router

`orchestration/nexus_intent.py` reads the *shape* of each message and returns
one intent: `chat` (answer), `analysis` (read the real state, change nothing) or
`autonomous` (the work a message asks for). It never decides from a single
keyword — "проверь", "проект" and "код" appear in both questions and orders.
The caller then turns that intent into the mode the turn runs in
(`effective_mode`). The conversation's `mode` is a *default*, not a decision.

## The modes

A conversation is in one mode; a new one is `auto`.

| Mode | What it does | Starts a run? |
| --- | --- | --- |
| `auto` (default) | Decides per message: answer, analyse, or do the work. | Yes, when the message asks for work. |
| `ask` | Answers a question. | No. |
| `analysis` | Reads the real project/run state and answers. Read-only. | No. |
| `plan` | Produces a validated plan. | No. |
| `code` | Starts one OpenHands workflow. | Yes (a workflow). |
| `autonomous` | Plans a goal and runs it. | Yes. |
| `manual` | Discusses, proposes a plan, waits for an explicit launch. | Only on an explicit command. |

### Auto

Auto is the autonomous mode. It is **not** a confirmation mode:

- it decides per message whether a reply is enough or OpenHands is needed;
- it plans understandable work and starts it **without asking**;
- it answers a question and never starts an agent for a conversation;
- it asks one clarifying question when a message is genuinely ambiguous;
- it respects the project's bound repository and the existing safety policies
  (the dispatch gate `authorize_dispatch` runs before any conversation).

Because Auto runs by itself, a project must be bound to a repository and the
provider cascade must have a usable provider; without either, the turn says so
in the chat instead of failing silently.

### Manual

Manual is the deliberate mode. The dialogue is a small state machine, stored on
the conversation (`nexus_conversations.workflow_state`) so it survives a
restart:

```
DISCOVERY → CLARIFICATION → PLANNING → AWAITING_APPROVAL → EXECUTING → REPORTING → COMPLETED
                                   ↘ CANCELLED / FAILED
```

`EXECUTING` is reachable **only** from `AWAITING_APPROVAL`. A discussion cannot
become a run. In Manual:

1. a question is answered; nothing is planned;
2. an order to do work produces a **plan** (`nexus_chat.plan_for_approval`),
   shown in the chat and stored as the plan on offer;
3. an **explicit launch command** starts exactly one run and moves the state to
   `EXECUTING`.

The confirmation is bound to the plan's **version**. The plan's hash is stored
with it (`plan_hash`); a launch records the approved version
(`plan_approved_hash`). If the plan changes (a requirement change produces a new
plan), the stored approval no longer matches and the new version must be
approved on its own — a replan can never silently keep an old confirmation.

### Chat

`ask` and `analysis` only answer. No run and no OpenHands conversation is ever
started. `analysis` reads the project's real runs, tasks and reports from the
database (`App._run_facts`) so answers are grounded, not guessed.

## Launch commands

`orchestration/nexus_workflow.classify_launch` recognises an explicit command
and, just as important, the three shapes that only *look* like one:

| Message | Recognised as |
| --- | --- |
| «Запускай», «Начинай», «Выполняй», «Запусти задачу» | command |
| «Start», «Execute», «Run it», «Proceed with implementation» | command |
| «когда запустишь?», «Запустить?», «Ты уже запустил?» | question — not a command |
| «Не запускай пока», «Don't start yet» | prohibition — not a command |
| «Запускай только после проверки», «Запусти, когда тесты пройдут» | condition — not a command |

## Runs in the interface

- A run that **completed** and was accepted leaves the active list by itself.
  Its row, tasks, report and evidence all stay; it is read again by its id or in
  the **archive** (`GET /api/v1/projects/{id}/runs?archived=1`, the «Архив
  прогонов» button on the Agent tab).
- A **failed**, **blocked** or **paused** run stays visible: it needs attention.
- A run that ended **without** completing (cancelled, failed) writes an honest
  report to the chat it came from, at once, through the same outbox as a
  success report — so it is delivered once and survives a restart. It never
  presents itself as a success.
- A **blocked** run tells its chat why, immediately: a blocked run cannot go on
  without a person.

## Where the code lives

- `orchestration/nexus_intent.py` — the router (intent per message).
- `orchestration/nexus_workflow.py` — the dialogue state machine and the launch
  classifier.
- `orchestration/nexus_chat.py` — the chat brain; `plan_for_approval` produces
  the plan a person approves.
- `orchestration/nexus_conversations.py` — the workflow state, the plan on
  offer and the approval bound to its version.
- `orchestration/web_server.py` — the Manual turn (`_manual_turn`), the launch
  (`_launch_initial`), the run/archive routes.
- `migrations/0034_chat_workflow.sql` — the conversation workflow columns and
  the `manual` mode.

## Tests

`tests/test_chat_workflow.py` covers the classifier, the state machine, the
approval/version binding and the observable behaviour over real HTTP and
PostgreSQL: an order in Manual plans and starts nothing, an explicit launch
starts exactly one run, a prohibition and a condition do not, a replan needs a
new confirmation, Auto still runs, a completed run moves to the archive, and a
cancelled/failed/blocked run is reported honestly. `tests/test_run_reports.py`
covers the terminal-run reports.
