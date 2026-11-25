# RECOVERY AND FAILURE TESTS — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED (deterministic fault injection).** Every failure mode the
mission lists has a real test; recovery never duplicates an irreversible
operation, never loses a task, and never marks unfinished work complete.

---

## 1. The twelve scenarios the mission names

| # | Scenario | Test | Behaviour |
| --- | --- | --- | --- |
| 1 | NEXUS restart during execution | `test_start_handover.py`, `test_lab_worker_process.py` | recorded start followed by the next worker; no second conversation |
| 2 | Worker crash (SIGKILL) | `test_lab_worker_process.py`, `test_dispatch_durability.py` | a second process continues the run |
| 3 | OpenHands restart | `test_store_reconnect.py`, `test_config_reconcile.py` (pending) | reconnects; a comparison that cannot run is `pending`, not `drift` |
| 4 | PostgreSQL disconnect | `test_store_reconnect.py` | connection replaced on evidence; `StoreUnavailable` → 503 |
| 5 | GitHub token expiry | `test_git_credential.py`, `test_config_reconcile.py` (blocked) | no credential → task runs unverified, never with another identity |
| 6 | Model provider outage | `test_provider_diagnostics.py` | `BLOCKED`/`FAILED`, never a fabricated plan |
| 7 | LLM timeout | `test_provider_diagnostics.py`, `test_connectivity.py` | `CloudUnavailable`, bounded retry |
| 8 | Session creation failure | `test_openhands_failover.py` | `DispatchError`, requeue, failover |
| 9 | Session-id persistence failure | `test_dispatch_durability.py` (9 crash windows) | intent journal reconciles; one external start |
| 10 | Evidence persistence failure | `test_lab_evidence.py` (broken signer) | logged and ignored; the task verdict stands |
| 11 | Completion then backend crash | `test_dispatch_durability.py` (`verdict_before_signature`, `terminal_before_acceptance`) | signed exactly once; no duplicate |
| 12 | Notification delivery failure | `test_run_reports.py` (outbox) | delivered on the next pass, marked in one transaction |

## 2. Fencing tokens and leases

`tests/test_race_protection.py` (13 cases): a stale holder's
`mark_running`/`record_verification`/`release_task` matches no row (fenced by
`lease_token`); an expired-but-unreclaimed lease still belongs to its worker;
reclaim/switch/resume bump the token. `tests/test_concurrent_claims.py` proves
1–128 workers claim distinct tasks (mutation-proven: re-allowing `dispatched` in
`task_due.sql` makes it fail with 3× claims).

## 3. Bounded retries

`nexus_retry` is the whole policy as pure functions: cycle waits (a minute
doubling to an hour), refused-start waits (`Retry-After`, 15 min for a daily
limit), `NEXUS_RETRY_MAX_CYCLES` as the ceiling. A 429 is never an attempt.

## 4. Honest unrecoverable states

- A kill **before** the service answered a start: recovered by the 900 s lease
  (slower than the recorded-start path, bounded).
- A GitHub that does not answer during completion: completion is **postponed**,
  never asserted.
- An account whose stored key was removed: the conversation reads `missing` and
  the task is not reported as success.

## 5. This session's additions

The reconciler adds two honest failure states for configuration: `pending`
(OpenHands unreachable — nothing compared) and `blocked` (key rejected). Both
are recorded on the account row and never masquerade as `synced`
(`tests/test_config_reconcile.py`).
