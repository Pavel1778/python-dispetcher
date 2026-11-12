# OpenHands acceptance — test suite (sections A–G)

This records the acceptance suite added for the request
"11. Создай полноценный набор тестов" (section A–G of the task text), what each
section proves, and the honest status of the one thing that cannot be run in
this sandbox.

Run it with:

```bash
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus_test" \
  python -m pytest tests/test_acceptance_*.py -q
```

The suite is 50 tests across seven files plus a shared kit
(`tests/acceptance_kit.py`). Every test uses real code: real HTTP servers over a
socket, real PostgreSQL, real `git`, real `pytest`. No client is mocked.

## Section by section

### A. Connectivity — `tests/test_acceptance_connectivity.py` (15 tests)

- a working endpoint connects (`health()` true) and the request really went out
  with the Bearer key;
- a good key is accepted and `account_capabilities()` reads the account;
- a wrong key is a `CloudAuthError`, and `health()` is false while `ping()` still
  reports the endpoint *reachable* (it answered "no");
- a missing key is refused before any socket is opened;
- an unreachable endpoint (a closed port) and a 5xx are `CloudUnavailable`, and
  `ping()["reachable"]` is false;
- a timeout is a transport failure (`TimeoutError`), never a success;
- a dropped connection fails one call and the *next* call reconnects;
- reachability and authorization are reported as separate answers (including a
  429, which is an answer);
- a non-`http(s)` base URL is refused at construction; a redirect is refused and
  the key is never sent to the redirect target (two real listeners).

### B. Account Pool — `tests/test_acceptance_account_pool.py` (15 tests)

- one, two, three and twelve accounts are all supported and independently
  verified (each against its own endpoint, with its own identity);
- a disabled account is never selected; re-enabling restores it;
- the primary failing falls over to the backup; with two down it reaches the
  third;
- `manual` refuses rather than switching;
- a manual switch (a new default) is honoured;
- the fallback order is stored and reported;
- automatic distribution picks the least-busy account and skips one at its
  concurrency limit; priority orders the choice;
- removing an account with active tasks is refused, then forced.

### C. Context Handoff — `tests/test_acceptance_handoff.py` (5 tests)

- the hand-off context carries everything the task text lists: project, task,
  goal, plan, completed steps, remaining steps, repository, branch, last commit,
  acceptance criteria, previous report, last error;
- the hand-off message tells the new executor this is a continuation, names the
  completed step as "do NOT redo", and says the checkout must be cloned;
- a real two-task run: #1 fails to start T1, T2 is handed to #2, T1 is verified
  and never restarted, the run reaches `completed`;
- the switch is recorded durably (`nexus_task_handoffs`) with the context.

### D. Autonomous Worker — `tests/test_acceptance_worker.py` (3 tests)

The real `WebServer` on real PostgreSQL with its own loop **off**
(`NEXUS_LAB_INLINE=0`) and a real `python -m orchestration.lab_worker` **child
process**:

- the worker registers (a fresh heartbeat) and takes a run from pending to
  completed with no client connected;
- after the run finishes, a **restarted** server process reads the same completed
  run from the database;
- stopping the server entirely does not stop the worker: the run still finishes.

### E. Recovery — `tests/test_acceptance_recovery.py` (7 tests)

- a worker `SIGKILL`ed mid-run is replaced by a new process that continues the
  run and starts no task twice;
- a gracefully stopped worker is replaced and the run finishes;
- an expired lease with no conversation is reclaimed and re-claimed; the fencing
  token moves forward, so the dead worker's writes are no-ops;
- an expired lease **with** a conversation is not requeued (that would be a
  duplicate) and no second task is claimable;
- a task out of attempts is `failed`, not requeued for ever;
- a lost connection requeues the task for failover with the failed account
  excluded and the abandoned conversation recorded;
- a stale worker cannot settle a task another worker has taken.

### F. Real development task — `tests/test_acceptance_dev_task.py` (3 tests)

A real development task driven through NEXUS against a **real git repository**:

- the agent creates a small Python project, writes a function and unit tests,
  runs them (they fail on a controlled bug), fixes it, re-runs them (they pass),
  and commits;
- NEXUS's real `GitVerifier` reads the branch head from a real HTTP server that
  answers from `git rev-parse`, so the verified commit is a genuine one;
- the work leaves a real, inspectable `git diff --stat`;
- the live OpenHands path is asserted BLOCKED without a key (see below).

### G. End-to-end failover — `tests/test_acceptance_e2e_failover.py` (2 tests)

The task text's ten steps, on real state:

1. a real task is submitted; 2. it starts on #1 and work really begins (a file
is written in #1's real clone); 3. the run advances; 4. #1 fails (its
conversations vanish); 5. the task moves to #2; 6. the context is handed over
(the message and a durable `nexus_task_handoffs` row); 7. #1 is **stopped** — its
conversation is stopped for real and its checkout shows only the artefact it
wrote before the failure, and a stopped client refuses to write; 8. #2 commits
and pushes, and the run completes; 9. an independent check reads the real commit
from #2's checkout; 10. no duplication — each account started exactly one
conversation.

A second test proves the branch stays held while the abandoned conversation
cannot be stopped, so two writers never share one checkout.

## What is BLOCKED here, and why

- **The live OpenHands Cloud run.** This sandbox has no second Cloud account and
  the local agent-server on `127.0.0.1:60000` requires a `SESSION_API_KEY` that is
  not present, so an unauthenticated request is refused. The live variant of
  sections F and G is therefore **BLOCKED**, not faked:
  `test_the_live_openhands_path_is_blocked_without_a_key` asserts exactly that
  refusal. The tests above prove the same logic end to end against real HTTP
  servers, real PostgreSQL, real `git` and real `pytest`.
- **A two-live-account failover.** Only one OpenHands key is available, so the
  switch is proven against real PostgreSQL and honest transport fakes, as
  `tests/test_openhands_failover.py` already did.

Nothing above claims a live run that did not happen. A section that cannot run
is reported BLOCKED; a check that did not run says so.
