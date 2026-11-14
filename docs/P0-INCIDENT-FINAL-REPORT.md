# P0 incident — final report (section 14)

## What happened

NEXUS's Intent Router misclassified report/history requests and explicit
"don't launch" prohibitions as orders to start an autonomous run. In
conversation `2547fea6` on the Klyrshift project this produced 7 runs in 7
minutes: distinct, mostly harmless-looking chat messages were each read as a
fresh work order.

## What was done, in two pull requests

### PR #37 (merged, `main` at `42609a4`) — the root cause

Three real defects in `orchestration/nexus_intent.py`, all now fixed and
covered by `tests/test_intent_router.py` (63 tests):

1. **A report/history request started a new run.** A Russian passive
   participle of an order verb («завершившегося», «выполненной», …) shares a
   stem with its imperative («заверши», «выполни», …); the order regex
   matched the shared stem with no grammatical-form check. Fixed with a
   negative lookahead per affected stem.
2. **An explicit prohibition was read as the order it forbids.** «Не
   запускай ничего, просто ответь» had no negation guard on the general
   order check (only the narrower "run as a run" check had one). The guard
   now applies everywhere.
3. **"Автономно" mode ignored history/prohibition requests.** That
   conversation mode treated any unclassified reply as a stated goal.
   `Intent.history_request` and `Intent.forbids_run` now hold in every
   conversation mode.

### PR #38 (open, this session) — one more real bug plus the acceptance suite

4. **A bare follow-up question lost its context.** «А что в нём исправлено?»
   asked right after an `analysis` turn was classified as a context-free
   `ask` (no database facts in the prompt), because the router classifies
   each message in isolation with no memory of the previous turn's mode.
   Fixed with `_continues_analysis()` in `orchestration/web_server.py`: a
   bare `CHAT`/`"question"` follow-up right after an `analysis` turn stays in
   `analysis`. A real topic change is unaffected (different `intent.reason`).

   `tests/test_regression_p0_suite.py` (new, 8 tests) covers the full §10
   acceptance list together with `test_intent_router.py`:

   | # | Scenario | Test |
   | --- | --- | --- |
   | A, B, H | greeting / status question / explicit order in auto mode | `test_intent_router.py` |
   | C, D | historical report request / explicit prohibition, end to end | `test_intent_router.py` |
   | E | analysis turn reads real state, changes nothing | `test_intent_router.py` |
   | F | plan request shown in chat, 0 runs | `test_regression_p0_suite.py::test_f_*` |
   | G | ambiguous "let's continue" with no prior topic | `test_regression_p0_suite.py::test_g_*` |
   | I | double-submit of the same message, no second run | `test_regression_p0_suite.py::test_i_*` |
   | J | every provider returns empty text: clear error, no run | `test_regression_p0_suite.py::test_j_*` |
   | K | provider outage mid-turn: recorded failure, not a hang | `test_regression_p0_suite.py::test_k_*` |
   | L | report follow-up stays on the named run (bug 4 above) | `test_regression_p0_suite.py::test_l_*` |
   | M | prohibition, then an informational question: still no launch | `test_regression_p0_suite.py::test_m_*` |
   | N | a completed run's report survives a backend restart | `test_regression_p0_suite.py::test_n_*` |

   `docs/CHAT-FIRST.md` was extended with a consolidated write-up of all four
   bugs and this table, so a future session does not have to re-derive the
   history from the code.

### Verification performed this session

- `test_regression_p0_suite.py` alone: 8/8.
- `test_intent_router.py` + the new suite together: 71/71 (unrelated to the
  §4 investigation below).
- Regression slice over chat/workflow/conversation/report/lab code
  (`test_chat_repository`, `test_chat_task_context`, `test_chat_workflow`,
  `test_conversation_cleanup`, `test_conversations`, `test_workflow_actions`,
  `test_run_reports`, `test_lab_http`, `test_lab_loop`, `test_sr_lifecycle`):
  356/356.
- Full non-browser suite (two halves, two databases): 1742 passed, 10
  skipped.
- Browser e2e suite (`test_browser_e2e.py`, a real unpacked Chromium 131, run
  three times across this session in isolation): 35/35 each time.
- `ruff check` on all changed files: clean.

No regressions anywhere; PR #38 is additive. It is open at
<https://github.com/Pavel1778/python-dispetcher/pull/38> and not merged
without explicit permission.

## Sections investigated this session but not changed, and why

The original master task's exact wording for sections 4, 5-6 and 7-9 was
never recorded verbatim in this session's history — only inferred
descriptions survived. Rather than build against a guess, each was checked
against the real, tested behaviour of the codebase before writing anything.

### Section 4 — "an idempotency/active-task guard distinct from the
spec-level one in `nexus_specs.py`"

**Investigated and found to be already correctly handled — no change made.**
The working hypothesis was that nothing stops a *second, distinct* order
from starting a *second, concurrent* run in a conversation that already has
one active (as opposed to the existing guard, which only catches an
identical retried message). A guard along those lines was implemented and
initially passed its own new tests and the full regression slice above.

It was reverted after the full browser e2e suite caught it breaking a real,
intentional, already-tested feature:
`test_browser_e2e.py::test_a_phone_scrolls_the_list_of_runs_to_the_last_one_with_a_finger`
sends four distinct goals in one conversation and asserts the "Агент" tab
shows **four concurrent runs** — multiple concurrent autonomous runs per
conversation are by design, not a bug. The guard's premise was wrong, so it
was removed rather than force a fix the product's own tests reject. The
existing per-goal idempotency (an identical resubmitted text never starts a
second run of the same work) is confirmed to already be the correct and
complete guard for section 4's actual concern; no code change was needed.

### Sections 5-6 — report generation extensions

**Not pursued.** `orchestration/nexus_reports.py` is already extensive and
carefully specified in its own module docstring (exactly-once delivery
transactionally tied to a verified run state, a clear separation between
what NEXUS itself confirmed on GitHub and what an agent merely claimed, and
a durable retry schedule for delivery). It is covered end-to-end by
`tests/test_run_reports.py`, included and passing in every regression run
this session. Without the original task's concrete list of "additional
facts/cases" to add, making speculative changes here carries the same risk
section 4 just demonstrated: a plausible-sounding addition that turns out to
conflict with an already-correct, already-tested design. No change was made.

### Sections 7-9 — frontend/response-delivery

**Not pursued.** The current transport is confirmed to be a single JSON
response per chat turn over a stdlib HTTP server (documented explicitly in
`Handler._chat_turn`'s own docstring), not a token-level stream or
WebSocket. Whether sections 7-9 actually required changing this is unknown:
no verbatim requirement survived, and switching transports is a
cross-cutting architecture change (server, client JS, and the whole test
suite's HTTP assumptions) that should not be undertaken on an inferred
guess. No change was made.

## Recommendation

Sections 4's actual concern is already satisfied by existing code; no
further action is needed there. For sections 5-6 and 7-9, if there is a
concrete requirement behind them, restating the original section text (even
approximately) would let a future session implement and test against a real
target instead of guessing. Otherwise, the P0 incident itself — the
misclassification that caused the 7-runs-in-7-minutes event — is fully
fixed, tested, and merged (PR #37) or ready to merge (PR #38).
