# NEXUS Ultimate — delivery report

One increment of four independent, tested phases on top of `main`. Every change
is additive: no existing behaviour was removed, no user data is lost, no
existing test was weakened. Each phase has its own migration, its own routes and
its own tests.

Base commit: `42609a4` (the merge of PR #37). A safe restore point was taken
before any change (`nexus restore-point create`, HEAD `42609a4`, bundle
SHA-256 recorded under `.restore-points/`, gitignored).

## A. Architecture — what changed

| Phase | Module | Migration | What it does |
| --- | --- | --- | --- |
| 4 | `orchestration/nexus_commands.py` | — | One pure parser + registry for slash commands, hashtags, typed params |
| 3 | `orchestration/nexus_generation.py` | `0037` | Cancellable AI generation (stop generation) |
| 3 | `orchestration/nexus_conversations.py` | `0037` | Pins, soft delete/restore, content search, export, generations |
| 6 | `orchestration/nexus_lab_store.py` | — | `retry_failed`, `delete_run` |
| 2 | `tools/benchmark.py`, `tools/benchmark_compare.py` | `0037` (indexes) | Reproducible measurements |
| — | `orchestration/web_server.py` | — | Routes and wiring for all of the above |
| — | `orchestration/studio/*` | — | Stop button, pin/export/delete, trash, run actions |

No component was rewritten and **no language migration was done**. The measured
profile (`docs/PERFORMANCE.md`) shows the read paths are single-digit
milliseconds of database time and the slow paths are network-bound (the LLM
provider, OpenHands Cloud, GitHub). A Go/Rust rewrite would not move those
numbers and would split the invariants documented in one place; a hybrid would
only pay off under a load this deployment does not have. The evidence is the
benchmark table, not an assertion.

## B. User features

- **Command system** — `/chat /plan /autorun /audit /fix /test /review /deploy
  /research /status /help`, four hashtag groups, typed parameters. Restrictions
  win: `#read-only` downgrades an execute command; `/deploy #no-deploy` is
  refused. Buttons and slash commands share one parser. `GET /api/commands`.
  Reference: `docs/COMMANDS.md`.
- **Conversation management** — pin/unpin (durable, a pinned section), archive,
  soft delete/restore (a real, reversible delete), search by title *and* message
  text, JSON export. The sidebar has a pinned section, an archive and a trash.
- **Stop generation** — the answer in flight is stopped at once; the partial
  text is kept with a "stopped" mark; the chat stays usable. Separate from
  cancelling a run.
- **Run history** — hide/show a finished run, remove it from history (its own
  record only), retry *only* the failed tasks.

## C. Performance

Measured with `tools/benchmark.py` against a real server and PostgreSQL (the
full table is in `docs/PERFORMANCE.md`). Baseline (p50, ms): studio status 0.82,
list projects 1.11, open project 1.72, list conversations 1.42, open
conversation 1.44, fetch history (20 messages) 1.74, commands registry 0.77,
search by content 1.71, export 2.38, chat turn 23.30, start run 47.35, store
counts 0.61, file upload 3.14.

The read paths are already sub-millisecond of database time; the chat turn and
the run start are dominated by the provider and the planning. Two indexes were
added for the new conversation-list order. **No speedup is claimed**: the
numbers are a baseline, and `tools/benchmark_compare.py` is the instrument for
any future claim.

## D. Testing

- New tests: `tests/test_commands.py` (34), `tests/test_conversation_management.py`
  (9), `tests/test_generation_cancel.py` (7), `tests/test_run_history.py` (7),
  `tests/test_benchmark_tool.py` (4), and two Chromium journeys in
  `tests/test_browser_e2e.py`.
- Route-auth coverage extended to every new endpoint.
- The full suite is run against a disposable PostgreSQL. **One regression was
  found and fixed before release**: the command parser's whitespace handling
  changed the bytes of a stored specification, caught by
  `tests/test_long_task.py` / `tests/test_long_task_cases.py` (58 cases). The
  fix makes `_remove_tokens` return the text byte for byte when it recognises
  nothing, and `_chat_turn` parse the raw message. Those 58 cases pass again.

## E. Git

- Branch `main`. Commits: command system; conversation management + stop
  generation; run history; UI + benchmark + docs; the long-task whitespace fix;
  browser journeys; this documentation.
- No force push. No remote branch deleted. The `arena/*` and `nexus/*` branches
  are untouched.

## F. Blockers / not done

- **No language migration** — a deliberate decision with the benchmark as the
  evidence, not a blocker (`docs/PERFORMANCE.md`).
- **Live services were not exercised** for this increment. OpenHands, GitHub and
  the LLM provider are the local stand-in servers in every test; the existing
  live-verification notes in `AGENTS.md` (an OpenHands key, a real GitHub token)
  are unchanged by this work.
- **The 39-failure regression run** was the whitespace regression above; after
  the fix the affected modules pass. A full clean run is recorded in the
  session's terminal log.
- Not implemented in this increment (independent future phases, unchanged from
  the roadmap): global search across every entity, a notification centre with
  categories, a prompt-template library, customisable hotkeys, a multi-tab
  workspace, and a native iOS client. These are additive and do not block the
  phases delivered here.
