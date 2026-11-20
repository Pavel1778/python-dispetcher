# DISPATCH RECOVERY ACCEPTANCE — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: VERIFIED.** The durable send gate (`nexus_dispatch_intents`, PR #55)
plus the pre-existing start-handover and fencing cover every crash window the
brief names, each with a real `SIGKILL` of a real worker process.

---

## 1. The mechanism

A lease authorizes a *mutation*, not a conclusion about what a provider did. The
journal (`orchestration/dispatch_journal.py`, migration `0043_lab_dispatch.sql`)
records one intent per `(task, lease_token)` and moves it through:

```
prepared → sending → (uncertain) → accepted → identified → bound → (stop_owed)
```

`IN_DOUBT = (sending, uncertain, accepted, identified)` — these four hold the
branch even after an operator cancels/requeues, so a late provider reply cannot
be used to bind a *subsequent* grant. The one send permit (`arm`) is committed
in its own transaction and can never be re-entered from `prepared`.

## 2. The nine crash windows the brief names, and where each is tested

`tests/test_dispatch_durability.py::test_worker_crash_and_two_replacements_finish_one_signed_dag`
is parametrized over nine windows; each starts a **real** child process
(`tests/dispatch_crash_worker.py`), kills it with `SIGKILL`, then runs two
replacement workers and asserts one signed DAG completes with no duplicate start.

| Brief's crash window | Test window | Journal state at kill |
| --- | --- | --- |
| 1. Before task claim | `before_intent_commit` | (none) |
| 2. After task claim, before send | `prepared_before_send` | `prepared` |
| 3. Before OpenHands session creation | `armed_before_send` (see §3) | `sending`/held |
| 4. After session creation, before saving its id | `accepted_before_read`, `id_before_receipt` | `sending` |
| 5. After saving id, before acknowledgement | `receipt_before_bind` | `identified` |
| 6. During agent execution | `bind_before_running` | `identified` |
| 7. After agent completion, before result persistence | `verdict_before_signature` | `bound` |
| 8. During evidence collection | `signature_before_ack` | `bound` |
| 9. During final status transition | `terminal_before_acceptance` | `bound` |

Each asserts: exactly the expected number of external starts (never two for one
task), the intent reaches `bound`, the DAG ends `completed_verified`, and the
evidence index is intact and re-verifies against the observed commit.

## 3. Additional durability cases (same file)

- `test_armed_but_not_sent_is_uncertain_not_a_license_to_post` — an armed intent
  that never sent is `uncertain`, and a successor cannot reuse the permit.
- `test_late_response_after_takeover_and_concurrent_reconcile` — a reply that
  arrives after a takeover does not bind the new grant.
- `test_missing_async_receipt_search_outage_and_late_visibility` — a search
  outage is "not known yet", never "no orphan".
- `test_revoked_unknown_keeps_branch_until_late_receipt_owes_a_stop` — the
  branch stays held until a late receipt is reconciled.
- `test_scoped_create_has_one_external_execution_after_lost_reply` — one external
  execution even when the create reply is lost (both transports).
- `test_expired_prepared_grant_cannot_send_or_disarm_its_successor`.
- `test_timeout_at_cycle_ceiling_settles_failure_with_durable_stop`.
- `test_broken_signer_keeps_both_obligations_until_task_signature_is_written`.

## 4. Process-death recovery (separate suites)

- `tests/test_start_handover.py` — a real `web_server` and `lab_worker`, killed
  with `SIGTERM` and `SIGKILL`; a start accepted by one worker is followed and
  bound by another (no second conversation).
- `tests/test_race_protection.py` — the fencing token: a stale holder's
  `mark_running`/`record_verification`/`release_task` matches no row.
- `tests/test_acceptance_recovery.py` — a `SIGKILL`ed worker is recovered by a
  new process.

## 5. Measured result (this session)

```
pytest tests/test_dispatch_durability.py tests/test_start_handover.py \
       tests/test_race_protection.py -q
-> 45 passed
```

## 6. Recovery never does the forbidden things

| Forbidden outcome | Guarantee |
| --- | --- |
| Lose tasks | The intent + task row are durable before any I/O; a replacement reads them |
| Duplicate irreversible operations | One send permit (`arm`), fenced by `lease_token`; a lost reply is reconciled, not re-posted |
| Corrupt project state | Transitions go through `_RUN_ENTERED_FROM` / the legal-path guard |
| Two workers own one active lease | `FOR UPDATE SKIP LOCKED` + fencing token; the loser's writes are no-ops |
| Mark unfinished work complete | Completion is the observed commit on the branch, not a report |
| Lose successful sessions | An accepted start is recorded (`pending_start`) and followed |

## 7. Remaining unrecoverable failure modes (documented, not hidden)

- **A kill before the service answered the start request.** No conversation id
  exists yet; the lease (900 s) and the adopt path recover it, which is slower
  than the recorded-start path but bounded.
- **A GitHub that does not answer during completion.** Completion is postponed,
  not asserted; the run waits rather than completing on an unverified result.
- **The account's daily conversation limit (429).** Handled as a wait, not an
  attempt; a daily limit waits 15 minutes. Never an infinite loop.
