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
  appears in a response.
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

## What a mock would hide

Do not replace the Cloud client with a mock in an integration test. The value of
`fake_cloud.py` is that it enforces the real request/response shapes (the
two-phase start, the 422 on a missing body, the `agent_settings` requirement).
A mock of the client would assert that the client calls itself, and would not
catch a schema drift.
