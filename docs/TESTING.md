# OpenHands and integration testing

How the OpenHands integration is tested, what is real in each test, and how to
run it. The guiding rule: a test asserts on a real code path, and a diagnostic
that did not run says so.

## Running the suite

```bash
# Dependencies (needs the venv with pytest; the system python may not have it).
python -m pytest -q

# Cloud/DB tests need a disposable PostgreSQL. Without it they skip cleanly.
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus_test" \
  python -m pytest tests/ -q -p no:cacheprovider

# Lint (the version CI pins).
python -m ruff check .
```

The end-to-end Cloud scripts are **not** collected by pytest
(`python_files = test_*.py`) and need a live key:

```bash
python tests/e2e_cloud_dispatcher.py   # real Cloud conversation, then delete it
python tests/e2e_local_sr_jr.py        # real Sr→Jr on a local agent-server
```

## What is real in each layer

| layer | real | faked |
| --- | --- | --- |
| Cloud client | `CloudClient` speaks HTTP over a socket | the fake Cloud server's *LLM* behind a conversation |
| Account pool | the real service, real PostgreSQL | — |
| Diagnostics | the real service, real PostgreSQL, real fake Cloud | the live probe's conversation (opt-in) |
| Failover | the real scheduler, pool, store, router | the transport clients (small honest fakes of the client surface) |
| Lab worker | real `LabWorker`, real PostgreSQL, real leases | the executor/verifier in loop tests |

`tests/fake_cloud.py` and `tests/fake_runtime.py` are **real HTTP servers**, not
mocks of the client: the client under test makes real requests over a socket,
and the server enforces the same shapes the live service does (including the
two-phase start). Only the LLM behind a conversation is simulated.

## The OpenHands test files

- `tests/test_openhands_multi_account.py` — encryption at rest, isolation, the
  key never returned, an arbitrary number of accounts.
- `tests/test_openhands_pool.py` — the four selection modes, enable/disable,
  remove-with-active-tasks, priority, fallback order; and the diagnostics
  report's four statuses (including that the live probe is `UNVERIFIED` unless
  requested, and `BLOCKED` — not `FAILED` — when no account exists).
- `tests/test_openhands_failover.py` — the load-bearing end-to-end: a real task
  starts on account #1, #1 fails, the scheduler switches to #2, and #2 receives
  the hand-off context. Also: a vanished conversation fails over instead of
  dead-lettering, and no usable account blocks the run with a reason.
- `tests/test_openhands_pool_http.py` — the HTTP surface for the pool,
  per-account controls and the diagnostics report; asserts the API key never
  appears in a response. Also the `/api/providers` route (configuration, and a
  real live probe of a fake provider over a socket) and the provider checks in
  the report.
- `tests/test_provider_diagnostics.py` — the AI provider cascade: configuration
  presence, key checking, model availability, authorization errors, timeouts,
  failover switching, context preservation across a switch, and return to the
  primary provider; plus the two provider diagnostic checks (``providers`` for
  configuration by presence only, ``provider_probe`` earned from a real
  read-only call and ``UNVERIFIED`` unless requested). Each provider is a real
  local HTTP server the client speaks to over a socket.
- `tests/test_lab_store.py` — worker heartbeat (live/old/stopped), lease
  reclaim, attempt budgets.
- `tests/test_route_auth.py` — every new endpoint requires auth.

## The diagnostics report

`GET /api/openhands/diagnostics` (or `POST` with `{"live": true}`) returns a
check per integration point. Each status is earned:

- **VERIFIED** — the check ran against the real thing and it worked.
- **FAILED** — the check ran and it did not work.
- **BLOCKED** — the check could not run: no credential, no resource (a 429), no
  permission. Nothing is claimed about whether it would work.
- **UNVERIFIED** — the check was not run (e.g. the live probe was not
  requested). Absence of evidence, distinct from FAILED.

`report.ok` is true when nothing FAILED; BLOCKED and UNVERIFIED do not make it
false.

The checks map one-to-one onto section 13 of the request:

| check | what it proves | needs `live` |
| --- | --- | --- |
| `database`, `schema` | PostgreSQL round-trip; every required table/column | no |
| `accounts` | each configured account's key is accepted | no |
| `account_pool` | the selection mode picks a usable account | no |
| `cloud_api` | the OpenHands endpoint answers | no |
| `queues_leases` | claim → release → re-claim on a throwaway run | no |
| `recovery` | an expired lease is reclaimed and re-claimed with a higher fencing token; a bound conversation is not re-claimed | no |
| `lab_worker` | a fresh worker heartbeat, or FAILED when work is waiting and none is fresh | no |
| `providers`, `provider_probe` | the LLM orchestrator cascade (configuration; then a real probe) | probe only |
| `openhands_tools`, `mcp_configuration`, `nexus_tools` | the agent's tool catalog, the account's MCP names, the NEXUS surfaces | catalog only |
| `conversation_create`, `message_and_events`, `run_command`, `workspace_rw`, `task_completion` | create a conversation, deliver a message, read events, run a shell command, read/write the sandbox, reach the terminal state `finished` | yes |

`recovery` and `queues_leases` are real database round trips on a throwaway
project the check deletes; they need no Cloud key, so they earn VERIFIED even on
a keyless deployment. `task_completion` follows the conversation's real terminal
state: `finished` is VERIFIED, any other terminal state (`error`, `stuck`) is
FAILED — never a success the probe did not observe.

## Live verification (recorded)

Verified against the real Cloud key on 2026-11-11:

- `account_capabilities` — VERIFIED (20 MCP servers discovered).
- `cloud_api.health()` — VERIFIED.
- The live conversation probe, reusing a running sandbox and with
  `OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash` — **VERIFIED**:
  conversation created, message delivered, events read, a shell command run and
  its output echoed, and a file uploaded to and read back from the sandbox with
  a matching SHA-256. The probe deletes its conversation.

Blocked live: creating a *new* sandbox returned `429
DAILY_SANDBOX_LIMIT_REACHED` (the account's daily quota). That is reported
`BLOCKED`, and the probe reused a running sandbox instead.

## Live verification (2026-11-12, this session)

Against the real Cloud key and a local PostgreSQL:

- `cloud_api`, `accounts`, `queues_leases` — **VERIFIED**.
- The live probe on the `direct` transport with the account's own
  `default_model` (`openhands/deepseek-v4.1-flash`) — **VERIFIED** for
  `conversation_create`, `message_and_events`, `run_command` and `workspace_rw`.
  This is what caught the defect the probe was supposed to catch: the probe used
  to build the *process-wide* client, so with no `OPENHANDS_DEFAULT_MODEL` in the
  environment the conversation ended `error` (LLM auth) even though the stored
  account had a working model. The probe now builds the client from the selected
  account. `tests/test_openhands_pool.py::test_live_probe_uses_the_selected_account_config`
  fails if that regresses.
- A real autonomous run through NEXUS (worker + account pool): the agent created
  a Python module, ran pytest, introduced a controlled bug, observed the failure,
  fixed it, re-ran the tests and committed. NEXUS recorded `completed_unverified`
  because no repository was bound — an honest verdict.
- `tests/test_backup_restore.py` and `tests/test_browser_e2e.py` were run with
  the PostgreSQL client tools and Chromium installed (previously skipped here).

## New regression tests (2026-11-12)

| test | pins |
| --- | --- |
| `test_openhands_pool.py::test_execution_client_for_account_honours_transport_and_model` | an account's own transport and `default_model` build its execution client |
| `test_openhands_pool.py::test_live_probe_uses_the_selected_account_config` | the live probe speaks to the selected account, not the process env |
| `test_openhands_pool.py::test_load_balancing_reads_in_flight_tasks_from_the_store` | load balancing reads load from `nexus_run_tasks`, not a counter nobody maintains |
| `test_openhands_pool.py::test_a_transient_failure_is_forgotten_when_the_account_works_again` | a finished conversation returns an account to `healthy` |
| `test_openhands_failover.py::test_manual_switch_hands_a_running_task_to_a_named_account` | an operator can switch a running task's executor; the hand-off is used once |
| `test_lab_http.py::test_manual_executor_switch_over_http` | the switch route: 409 for a non-in-flight task, 400 for a bad id |

Each fails if its fix is reverted.

## The acceptance suite (sections A-G of the request)

`tests/test_connectivity.py` and `tests/test_comprehensive_suite.py` are the
"Создай полноценный набор тестов" delivery, organised the way the request lists
it. Every assertion is on a real effect; a test that cannot run here says so.

| section | file | what it proves |
| --- | --- | --- |
| A. Connectivity | `test_connectivity.py` | `health()`/`ping()` over a real socket; a good key accepted; a rejected key is 401 → `CloudAuthError`, never a silent success; an unreachable endpoint and a timeout are `CloudUnavailable`; a client and an account recover after an outage (`check_connection` false → true) |
| B. Account Pool | `test_comprehensive_suite.py` | one/two/three accounts; disabling an account; the primary failing over to the reserve; manual mode refusing to switch; manual switch of a running task; automatic (load-balancing) distribution |
| C. Context Handoff | `test_comprehensive_suite.py` | a task starts on #1, fails over to #2 and completes; the hand-off message carries the task, plan, repository, branch and acceptance criteria; a finished stage is not repeated and its observed commit is preserved; unfinished work continues to completion |
| D. Autonomous Worker | `test_comprehensive_suite.py` | API + a separate `lab_worker` process; a task is submitted, the client disconnects, the run completes, and a fresh client reads the result |
| E. Recovery | `test_comprehensive_suite.py` | an expired lease is reclaimed and re-claimed; a worker killed before starting does not duplicate work; a bound conversation is never reclaimed into a second start; a lost OpenHands connection fails over |
| F. Real Development Task | `test_comprehensive_suite.py` | **BLOCKED** here (no live OpenHands key): the real dev task is skipped with the reason, and the honest half — the diagnostics report `BLOCKED`, not `VERIFIED`, without a key — is asserted |
| G. End-to-End Failover | `test_comprehensive_suite.py` | a real task starts on #1, #1 fails, the task moves to #2 with the context, #1 is never asked to start again, and the **real** `GitVerifier` reads the branch head on GitHub and accepts the commit the agent named |

The `fake_cloud.py` server gained two controls these use, and nothing else
changed for the existing tests: `rejected_keys` (a listed Bearer key is answered
`401`, as the live service rejects a revoked key) and `delay` (a real, slow
response for a real timeout test). A client that times out and hangs up no longer
prints a broken-pipe traceback from the server thread.

Section F needs a live OpenHands key and a sandbox, which this environment does
not have (AGENTS.md: the runtime here has no working LLM key). It is marked
BLOCKED rather than simulated — the request is explicit that a real switch must
not be faked.

## What a mock would hide

Do not replace the Cloud client with a mock in an integration test. The value of
`fake_cloud.py` is that it enforces the real request/response shapes (the
two-phase start, the 422 on a missing body, the `agent_settings` requirement).
A mock of the client would assert that the client calls itself, and would not
catch a schema drift.

## The extended acceptance suite (sections A-G)

`tests/test_acceptance_*.py` are a **second, more end-to-end** pass over the same
sections A-G the request lists ("Создай полноценный набор тестов"). Where
`test_connectivity.py` and `test_comprehensive_suite.py` (above) exercise the
services, these drive the whole integration over real sockets, real PostgreSQL,
real `git` and real `pytest`. They share `tests/acceptance_kit.py` (a
configurable Cloud HTTP server and a redirect server). Run them with:

```bash
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus_test" \
  python -m pytest tests/test_acceptance_*.py -q
```

| file | section | what it proves |
| --- | --- | --- |
| `test_acceptance_connectivity.py` | A | connect, authorize, a rejected key, an unreachable endpoint, a timeout, reconnection; reachability and authorization are separate answers; a redirect never carries the key |
| `test_acceptance_account_pool.py` | B | one, two, three and many accounts; disable; the primary failing; the move to the backup; a manual switch; automatic (load-balanced) distribution; priority and fallback order; remove-with-active-tasks |
| `test_acceptance_handoff.py` | C | a real task started on #1 and handed to #2: the task, the context, the Git state, the completed step not redone, the run completed, the switch recorded |
| `test_acceptance_worker.py` | D | the API and a **separate** `lab_worker` process: submit, close the client, the run continues, the result is readable after reconnecting, and the worker outlives the server process |
| `test_acceptance_recovery.py` | E | a SIGKILLed worker recovered by a new process; a lost connection requeueing for failover; an expired lease reclaimed; the fencing token stopping a stale worker; no duplicates |
| `test_acceptance_dev_task.py` | F | a real development task through NEXUS against a **real git repository**: create, test, break, fix, re-test, report, and a real diff |
| `test_acceptance_e2e_failover.py` | G | the full failover: a real task on #1, #1 fails, the switch to #2, the context handed over, #1 stopped (its real checkout shows no further write), the result verified, nothing duplicated |

Two properties are asserted the way the task text demands:

* **A transport is not mocked.** Section A, B, D and E speak to real HTTP servers
  over a socket; the store is real PostgreSQL. Sections F and G go further: the
  "agent" runs real `git` and real `pytest` in real directories, and the Git
  verifier reads a real branch head from a real repository.
* **The live OpenHands Cloud is reported BLOCKED, not faked.**
  `test_acceptance_dev_task.py::test_the_live_openhands_path_is_blocked_without_a_key`
  asserts that the local agent-server refuses an unauthenticated request — the
  reason the live path cannot be run here. See
  `docs/ACCEPTANCE-OPENHANDS.md` for the section-by-section status.

Nothing in these files weakens the invariant that a diagnostic which did not run
says so: a section that needs a second live account or a live key is marked
BLOCKED in the report rather than passing on a fake.

