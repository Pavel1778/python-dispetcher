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

A short order is not a task by itself: «Делай» says to act, and what to do was
said before it. Such a message (600 characters or fewer) takes the conversation
with it into the plan request and into the stored task of the run
(`nexus_chat.task_with_context`), exactly as it does when Auto starts a run; the
plan message says that it did.

A go-ahead that is not one of the launch commands below («Делай», «Сделай»,
«Ладно, запускай») while a plan is waiting for its confirmation is neither a
launch nor a new order: the plan stays as it is, no model is asked, and the
reply names the command. A message that names work («Сделай иначе: …») is a
change of requirements and gets a new plan.

When the last thing said was the router's clarifying question ("explain it in
the chat, or do the work and start a run?"), the next message answers that
question, and is read as its answer before anything else - also when it is
«Запускай». Choosing the work gives the message that was asked about its plan;
it does not start the plan that was on offer before the question.

The confirmation is bound to the plan's **version**. The plan's hash is stored
with it (`plan_hash`); a launch records the approved version
(`plan_approved_hash`). If the plan changes (a requirement change produces a new
plan), the stored approval no longer matches and the new version must be
approved on its own — a replan can never silently keep an old confirmation.

### Chat

`ask` and `analysis` only answer. No run and no OpenHands conversation is ever
started. `analysis` reads the project's real runs, tasks and reports from the
database (`App._run_facts`) so answers are grounded, not guessed.

Every answer - in these modes, and to a question in Auto or Manual - is written
with the project's repository in view (`App.repository_access`,
`orchestration/nexus_repo_reader.py`) and is told how work starts in the mode
the conversation is in (`nexus_chat.WORK_STARTS`): with an order in Auto, with a
plan and «Запускай» in Manual, and by switching the mode in a mode that only
answers.

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
- `migrations/0035_chat_workflow.sql` — the conversation workflow columns and
  the `manual` mode.

## Tests

`tests/test_chat_workflow.py` covers the classifier, the state machine, the
approval/version binding and the observable behaviour over real HTTP and
PostgreSQL: an order in Manual plans and starts nothing, an explicit launch
starts exactly one run, a prohibition and a condition do not, a replan needs a
new confirmation, Auto still runs, a completed run moves to the archive, and a
cancelled/failed/blocked run is reported honestly. `tests/test_run_reports.py`
covers the terminal-run reports.

`tests/test_intent_router.py` covers the classifier table, the shape rules and
a report/history request reading the database instead of launching anything.
`tests/test_regression_p0_suite.py` covers every scenario of the chat-safety
regression suite below (A–N), including double-submit, an empty provider
response, a provider outage, a clarifying follow-up that stays on the run
being discussed, and a report surviving a backend restart.

## The 2026-11 conversational-intelligence fix

A production incident (reproduced in `tests/test_intent_router.py` and
`tests/test_regression_p0_suite.py`) showed three real defects in the router
above, all in `orchestration/nexus_intent.py`:

1. **A report/history request started a new run.** «Дай отчёт последнего
   успешно **завершившегося** прогона» was read as an order to "finish"
   something, because the Russian passive participle of an order verb
   («завершившегося», «выполненной», «исправленной», …) shares the verb's own
   stem with its imperative («заверши», «выполни», «исправь», …), and the
   order regex matched the stem as a raw substring with no word-boundary or
   grammatical-form check. Each affected stem now carries a negative lookahead
   that blocks only the participle's suffix, not the imperative.
2. **An explicit prohibition was read as the order it forbids.** «Не
   **запускай** ничего, просто ответь» was read as an order to launch, because
   the general order check had no negation guard (unlike the narrower
   "asks for a run as a run" check, which already had one). The same guard is
   now applied to every order match.
3. **A history/report request could still default to work in an explicit
   "Автономно" conversation**, because that conversation mode treated *any*
   unclassified chat reply as a stated goal. A request to read the past
   (`Intent.history_request`) and an explicit prohibition (`Intent.forbids_run`)
   now hold in *every* conversation mode, including "Автономно": reading
   history, or refusing to launch, is never "the work" that mode is for.

A fourth, narrower gap was found and fixed while writing the section-8
("remembers which report was requested") regression test: a plain follow-up
question with no run named in it («А что в нём исправлено?») right after a
turn that read the run state fell back to `ask` (no database facts in the
prompt) because the Intent Router, by itself, has no memory of what "в нём"
refers to — each message is still classified on its own. `Handler._chat_turn`
now checks whether the conversation's last assistant turn was itself an
`analysis` turn before it accepts a bare `ask` for a plain question with no
topic of its own; if so, the follow-up stays in `analysis` and is given the
same run facts. An actual topic change (a greeting, a discussion, an order, a
plan request) is a different classification and is not affected.

### The no-unsolicited-launch guarantee, restated

- `classify()`'s intents (`chat`/`analysis`/`autonomous`) and the
  `Intent.starts_work`/`forbids_run`/`history_request` flags are the single
  place that decides whether a message is an order; `effective_mode()` is the
  single place that turns that decision into a turn mode, for every
  conversation mode including "Авто" and "Автономно".
- A request to read the past never reaches `autonomous`, `plan` or `code`,
  regardless of conversation mode (`Intent.history_request`).
- An explicit prohibition never reaches `autonomous`, `plan` or `code` for the
  rest of that turn, regardless of conversation mode (`Intent.forbids_run`).
- `_chat_turn` calls `self.app.chat.respond(mode=turn_mode, ...)` exactly once
  per message; `respond()` only ever starts a run when `mode` is `code` or
  `autonomous`, and only `effective_mode()` produces those two values.
- Reading history (`Handler._run_facts`) only ever calls `lab.list_runs`,
  `lab.count_tasks` and `reports.for_run` — all read-only queries; it never
  calls `LabStore.start_run`, the planner or OpenHands.

### Section 10 regression scenarios (A–N)

| # | Message / scenario | Expectation | Test |
| --- | --- | --- | --- |
| A | «Привет» | reply, 0 runs | `test_intent_router.py::test_the_requirement_table` |
| B | «Что уже готово?» | text reply, 0 runs | `test_intent_router.py::test_the_requirement_table` |
| C | «Дай отчёт последнего успешно завершившегося прогона» | historical report, 0 runs | `test_intent_router.py::test_report_request_with_a_completed_run_reads_history_not_a_new_run` |
| D | «Не запускай ничего, просто ответь» | no launch | `test_intent_router.py::test_an_explicit_prohibition_is_honoured_end_to_end_even_in_autonomous_mode` |
| E | «Почему прогон остановился?» | analysis of stored data, 0 runs | `test_intent_router.py::test_an_analysis_turn_reads_the_real_state_and_changes_nothing` |
| F | «Составь план исправлений» | plan shown in chat, 0 runs | `test_regression_p0_suite.py::test_f_*` |
| G | «Давай продолжим» | clarification/dialogue | `test_regression_p0_suite.py::test_g_*` |
| H | «Запусти автономный прогон» | exactly 1 run | `test_intent_router.py::test_an_order_in_auto_mode_starts_a_run` |
| I | double-submit of one message | second run NOT created | `test_regression_p0_suite.py::test_i_*` |
| J | empty model response | clear error, no empty state | `test_regression_p0_suite.py::test_j_*` |
| K | provider outage mid-turn | graceful, recorded failure | `test_regression_p0_suite.py::test_k_*` |
| L | report request → clarifying follow-up | stays on the same run, 0 new runs | `test_regression_p0_suite.py::test_l_*` |
| M | forbade launch, then an informational question | no launch | `test_regression_p0_suite.py::test_m_*` |
| N | backend restart | prior reports still accessible | `test_regression_p0_suite.py::test_n_*` |
