# NEXUS — Final Release Report

**Date:** 2026-11-16 (reconciled 2026-11-16 for the PR #46 → PR #47 integration)
**Repository:** `Pavel1778/python-dispetcher`
**Base `main` SHA:** `66291614ac2c47b33e825312c9f165f7b38de5cd` (`6629161`, merge of PR #46)
**This integration branch:** `arena/71f72168-python-dispetcher`
**Verdict:** **RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS**

NEXUS is a working autonomous AI Development Lab. It starts reliably, applies
its schema, accepts a natural-language task, plans it into a dependency DAG,
dispatches the tasks to live OpenHands, verifies results independently, reports
honestly and notifies the owner. This was exercised end to end against the live
OpenHands Cloud in this session (§9).

Two acceptance items need credentials this environment does not have (a second
live OpenHands account; a live account with a GitHub provider token). They are
named, not faked (§12). They remain blocked after the PR #46/#47 integration.

---

## 1. Final architecture

A stdlib HTTP server (`orchestration/web_server.py`) plus a durable PostgreSQL
store (`orchestration/cloud_store.py`, a `psycopg_pool` connection pool) form a
control plane that orchestrates OpenHands agent conversations:

- **Intake & planning** — `nexus_chat.py`, `nexus_intent.py`, `nexus_workflow.py`,
  `nexus_spec_planner.py`, `nexus_planner.py` turn a message into a validated
  task DAG.
- **Scheduling** — `nexus_lab.py` (`LabScheduler`) claims, dispatches, polls,
  verifies and settles; `lab_worker.py` is the loop (in-process thread or a
  separate process). Atomic claim, lease tokens/fencing, heartbeats, reclaim.
- **Execution** — `cloud_direct.py` (default) / `cloud_client.py` (app) drive
  OpenHands; `openhands_pool.py` + `openhands_router.py` select and fail over
  between accounts.
- **Verification & evidence** — `GitVerifier` reads the real remote branch;
  `evidence_trust.py` signs records with Ed25519.
- **Security** — `web_auth.py` (sessions), `nexus_policy.py` + `net_guard.py`
  (SSRF), `archive_safety.py` (ZIP), `secrets.py` (Fernet).
- **Interface** — the Studio (`orchestration/studio/`, static, no framework) and
  a thin CLI/MCP client (`cli.py`, `mcp_server.py`).

41 migrations, `0001`–`0040`.

## 2. Implemented features

Projects; natural-language tasks; AI planning (Gemini→Groq→OpenRouter→OpenHands
cascade); dependency-aware autonomous runs; live OpenHands dispatch; independent
verification; durable retries and cycles; cancellation with stop signals;
multi-account pool with failover; project isolation and RBAC; the File
Laboratory; the Evidence Trust Engine; an OpenHands inventory engine; the
run-time profiler; notifications; the Studio; the CLI; MCP (stdio + HTTP);
backup/restore and safe restore points; **restart a finished run** and
**duplicate a finished run** (PR #47, see §16).

## 3. Completed workflows

**The core workflow works end to end, live** (this session): create project →
connect a repository+branch (checked on GitHub) → create a goal → start a run →
planning produces a 2-task DAG → task 1 dispatched to a live OpenHands
conversation → the agent runs (`running`, 280+ events) → the verifier finds no
verifiable commit and records `completed_unverified` → a durable retry is
scheduled (`task_retry` notified) → the run is cancelled on request and stops.

## 4. Remaining limitations

See `COMPLETION_LEDGER.md` and `docs/KNOWN_LIMITATIONS.md`. The material ones:
no studio screen for File Lab/evidence/allowlist/account management (routes
exist); no MCP client; no Self-QA engine; no office-document preview; a live
task that must clone/push ends `completed_unverified` unless the OpenHands
account has a GitHub provider token. One previously documented concurrency
inversion (cancel↔worker-settlement) is closed in this integration; the
system-wide ordering argument is documented in §16, not claimed globally.

## 5. Security findings and fixes

Audited: authn/authz, project isolation, secret handling, SSRF, archive
extraction, path traversal, SQL injection, session handling. **One defect
found and fixed this session:** the `direct` transport accepted `secrets` on
`start_conversation` and silently dropped it, so a caller that supplied a
credential (a private clone, a push) believed the agent had it. It is now
forwarded (the agent-server's `StartConversationRequest` carries `secrets`),
with a regression test. No other P0/P1/P2 defects found; the SQL interpolation
audit confirmed every interpolated fragment is a column-list constant, never
user input. 196 security tests pass.

The two new run-management endpoints from PR #47
(`POST …/runs/{id}/restart`, `POST …/runs/{id}/duplicate`) are project-scoped
like every other run route (a non-member gets 404 — asserted in
`tests/test_run_history.py::test_restart_and_duplicate_are_member_only`), both
POST routes are registered in `tests/test_route_auth.py::PROTECTED_POST`, and
the two new MCP tools carry `SCOPE_RUN_CONTROL`. No protection was weakened.

## 6. Migration status

41 files, `0001`–`0040`, applied idempotently by the process on first connect
(recorded in `schema_migrations`). Gaps `0012`/`0013` are reserved for the
deferred File Lab and are intentional. An edited, applied migration is refused
(`MigrationError`); the one registered production correction (0037) converges
via `MIGRATION_CORRECTIONS`.

## 7. Full test results

See §13 for the numbers, and `PR46_PR47_FINAL_INTEGRATION_REPORT.md` for the
integration branch's fresh verification (including CI status).

## 8. Browser E2E results

`tests/test_browser_e2e.py` — 37 Chromium journeys (the full journey, hostile
text, failure and retry, phone and five other widths): **37 passed**
(`fix/approval-notification` session, PostgreSQL 17.11 + Chromium 159.0.8154.6;
not re-run in the PG 16.2 integration sandbox — see §13 and the integration
report).

## 9. OpenHands live test results

- Cloud reachable: `health()=True`, `ping()` 200, version **1.92.0**,
  `compatibility: compatible`.
- LLM cascade (live probe): Gemini `ok`, OpenRouter `ok`, Groq/OpenHands
  `blocked` (no key).
- **A real autonomous run through NEXUS**: conversation created `READY` on a live
  sandbox, ran `running → finished` with 280+ events; a second task was planned
  and gated on the first; the verifier recorded `completed_unverified` (the
  account has no GitHub provider token, so the agent cannot push — the honest
  verdict); a durable retry was scheduled; cancellation stopped the run.

## 10. Performance results

`tools/perf_bench.py` against a real local PostgreSQL 17: DB p99 **69 ms** and
**0 errors** at 100 concurrent clients (the pool serves them and returns every
connection); claim throughput **1345/s** at 50 workers. Absolute numbers differ
from the historical machine; no regression introduced.

## 11. Deployment instructions

`DEPLOY.md` (Render blueprint, Docker Compose, Dockerfile, systemd worker unit)
and `docs/ENVIRONMENT.md` (every variable). Minimal local start:

```bash
cp .env.example .env            # fill SUPABASE_DB_URL, OPENHANDS_API_KEY, AUTH_*
python -m orchestration.web_server --host 0.0.0.0 --port 8000 --token "$WEB_AUTH_TOKEN"
```

Generate the two auth secrets with `python -m orchestration.web_auth hash` and
`… secret` (run interactively). The schema applies itself on first connect. The
wheel builds and packages all Studio/PWA assets.

## 12. Known blockers (external)

1. **Two-live-account failover** — only one OpenHands key is available here.
   The failover logic is proven against the local fakes + PostgreSQL
   (`tests/test_openhands_failover.py`). To accept live: add a second account in
   the Studio and run `tests/test_acceptance_e2e_failover.py` against it.
2. **Section F live development task** — needs an OpenHands account whose
   sandbox can reach GitHub (a GitHub provider token on the account). To accept
   live: set that token, then run `tests/test_acceptance_dev_task.py` with a real
   repository.

Neither is faked anywhere in this repository. Both remain open after the
PR #46/#47 integration; the integration did not touch them.

## 13. Exact commit SHAs and test results

Historical, from the `fix/approval-notification` session (now part of `main`):

- Base `main`: `42824896e388c97e218a8915cab56a8498a28473`.
- PR #48 (this session) merged main-side; PR #46 then merged as **`6629161`**
  (2026-11-16T22:36:31Z) — current `main` and the base of this integration branch.
- Full suite (fresh PostgreSQL 17, `OPENHANDS_API_KEY` and `AUTH_*` unset so the
  tests exercise the code, not the shell): **1976 passed, 9 skipped** — the 9
  skips are environmental (3 mingw, 4 Android SDK, 1 pooler DSN, 1 live).
- `ruff check .`: clean. Studio render test: passed. Browser E2E: 37/37.

From the PR #47 branch session (`feat/run-restart-duplicate` @ `b30aba9`,
before integration), PostgreSQL 17.11 + Chromium 159.0.8154.6:

- Full suite: **1980 passed, 9 skipped, 0 failed** (31:40). Reference clean
  `main` @ `4282489`: **1973 passed, 9 skipped, 0 failed**.
- `ruff check .`: All checks passed. Studio render test: passed.
- Browser E2E: **37 passed** (Chromium).
- Deadlock stress (cancel test ×12): **0/12 failed** (was 1/6 on clean `main`).

From the PR #46 / PR #47 integration on `arena/71f72168-python-dispetcher`
(local **PostgreSQL 16.2**; PG 17 is exercised by CI, not by this sandbox):

- Full suite (`pytest tests/ -q`, fresh database): **1951 passed, 49 skipped,
  0 failed — 0:35:16 — EXIT=0** (`/home/user/pytest_integration_final.log`).
  37 of the 49 skips are the browser-E2E journeys (no local Chromium).
- Concurrency stress (24-test cancel/reclaim/race/deadlock selection × 30
  reps): **720 runs, 0 failures**.
- `ruff check .`: All checks passed. `git diff --check`: clean.
- Studio render test: passed. `tests/test_studio_ui.py`: 26 passed.
- Route-auth and restart/duplicate invariant suites: 11/11 and 12/12 passed.
- **No combined CI run exists for the integrated tree** (requires a push,
  which the task forbids); PG 17 and Chromium are unverified locally. The
  integrated tree is therefore **NOT FULLY VERIFIED** — see
  `PR46_PR47_FINAL_INTEGRATION_REPORT.md`.

## 14. This session's commits

| SHA | Change |
|---|---|
| `4aedcf8` | `approval_required` notification when a plan awaits confirmation |
| `c843ec6` | `task_started` notification when a task actually starts |
| `c3e9eea` | `run_blocked` notification when a run needs a person |
| `6de51dc` | fix: the direct transport forwards `secrets` to the sandbox create |
| `0065406` | test: poll for the rework's running state (determinism) |
| `6629161` | merge: PR #46 — F-01 `file://` `NEXUS_URL` guard + F-06 bounded transaction-level retry for transient `DeadlockDetected` in `set_run_state`/`set_run_state_internal` (whole transaction, max 1 retry, rollback first, idempotent operations, `DeadlockDetected` only, no other exception retried) with `tests/test_deadlock_retry.py` and `tests/test_cli_client.py` |
| `ec0d553` | merge: PR #47 — restart/duplicate runs (`feat/run-restart-duplicate` @ `f6e77c9`) into this integration branch |
| `2fbdcad` | test(route-auth): restart/duplicate POST routes added to `PROTECTED_POST` |
| `e2c7575` | fix(cancel): lock tasks before execution rows in run settlement (structural close of the cancel↔worker-settlement task↔executions inversion) + `tests/test_cancel_lock_order.py` |

Commits added by the integration (restart/duplicate) are listed in
`PR46_PR47_FINAL_INTEGRATION_REPORT.md`.

## 15. Release readiness verdict

**RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS.**

The product is usable today: it starts, plans, dispatches to live OpenHands,
verifies, reports and notifies. The two blocked items require credentials this
environment does not have and are documented with exact acceptance steps.
Readiness of the *integrated* tree additionally requires the combined CI gate
and the external blockers in §12 to be closed or waived by the owner.

## 16. PR #46 / PR #47 integration record (2026-11-16)

**PR #46** (MERGED 2026-11-16T22:36:31Z, merge commit `6629161`): F-01
`file://` `NEXUS_URL` guard in `orchestration/cli.py`; F-06 bounded
transaction-level retry for transient deadlocks in `LabStore.set_run_state` /
`set_run_state_internal` (`_is_deadlock`; `psycopg.errors.DeadlockDetected`
only; maximum one retry; the whole transaction is retried; rollback happens
before the retry; the operations are idempotent); new tests
`tests/test_deadlock_retry.py` (2 tests) and `tests/test_cli_client.py` (5
tests). The F-01 guard and the retry were merged unchanged by this reconciliation.

**PR #47** (`feat/run-restart-duplicate` @ `f6e77c9`, integrated on
`arena/71f72168-python-dispetcher`): `LabStore.restart_run` — re-runs a
finished run's whole plan as a **new run under the same goal**; every task
re-created `pending` at attempt 0; the old run kept as history. Refused (409)
while the goal has an active run, and while the run is not over.
`LabStore.duplicate_run` — copies a finished run's plan into a **new goal and
run** (a copy cannot share the goal: one active run per goal); idempotent with
an `idempotency_key`; a conflicting key raises `IdempotencyConflict`.
Surfaces: `POST /api/v1/projects/{id}/runs/{run_id}/restart|duplicate`,
`nexus run restart|duplicate`, MCP `restart_run` / `duplicate_run`
(`SCOPE_RUN_CONTROL`), studio «Перезапустить» / «Дублировать» on finished-run
cards. Migrations relevant: `0018_nexus_lab_runs.sql` (`uq_nexus_runs_active_goal`
partial unique index), `0009_nexus_plans.sql` / `0010_nexus_plans.sql`
(idempotency uniques).

**Deadlock status — accurate, do not overclaim:**

- PR #47 fixed the observed cancel↔reclaim inversion **structurally**:
  `reclaim_expired` now locks the run row `FOR UPDATE` before locking task
  rows — the order every other path already used. This fix is retained
  unchanged.
- PR #46 added a **bounded, idempotent, whole-transaction retry** for
  `DeadlockDetected` as a safety net for transient deadlocks (`set_run_state` /
  `set_run_state_internal` only; maximum one retry; rollback first; no other
  exception is retried). Retained unchanged.
- The cancel↔worker-settlement task↔executions inversion **was** structurally
  closed in this integration (commit `e2c7575`): the cancel branch of
  `_apply_run_state` now blocks unfinished tasks **before** closing their
  executions — the same order every worker-side settle takes. The two
  statements are independent inside the same transaction, so the swap
  preserves semantics. A deterministic regression test
  (`tests/test_cancel_lock_order.py`) pins the order: the worker's settle
  succeeds with the fix and fails with a lock timeout on the old order.
  Verified: new order passes; old order fails with `LockNotAvailable`.
- **Not claimed:** elimination of every deadlock in the system. Write paths
  were audited for run → tasks → executions order (see
  `PR46_PR47_FINAL_INTEGRATION_REPORT.md` for the lock-order table); no proof
  of global deadlock freedom is made. PostgreSQL remains the backstop — an
  unobserved ordering would surface as `DeadlockDetected` (or be retried once
  on the two settle paths), never be silently retried in a loop.
- PostgreSQL 17-specific behaviour is exercised by CI (PG 17 service), not by
  this sandbox (local PG is 16.2).

## 17. Reconciliation record — what was retained, updated, discarded, and why

Baseline for this reconciliation: the `main` (post PR #48) version of this
file. PR #47's version was read as a source of facts, not as structure.

**RETAINED (main baseline, PR #48 content):** the architecture/feature/security/
migration/live-OpenHands/performance/deployment narratives; the two external
blockers in §12; the honest `completed_unverified` account of the live run;
§13's fix/approval-notification session numbers (explicitly relabeled as
historical with base SHA 4282489 → `6629161` for the merged main); §14's commit
table (extended, not rewritten); §15's verdict wording.

**RETAINED (from PR #47):** the restart/duplicate feature definitions and
semantics (§16); the REST/CLI/MCP/Studio surface list; the member-only
authorization rule; the plan+DAG preservation, tasks reset to `pending` at
attempt 0, independent goal/run for duplicate, one-active-run-per-goal
(`uq_nexus_runs_active_goal`) and idempotency-key behaviour; the deadlock
root-cause description (complemented, not replaced, by #46's retry and by this
integration's task-before-executions swap); PR #47's
own test evidence (labeled as that branch's environment in §13); the known
limitations and external blockers.

**UPDATED:** header metadata (base SHA `4282489` → `6629161`; integration branch
named; reconciliation date added); §2 (restart/duplicate added to implemented
features); §4 (pointer to the residual F-06 limitation); §5 (route-auth
posture for the two new POST routes); §7 (pointer to the integration report);
§13 (all numbers now attributed per environment; CI/PG17 explicitly not
claimed for the merged tree); §14 (PR #46 merge recorded); §15 (verdict
qualified by the integration gates); §16 (both deadlock protections described
as they actually are).

**DISCARDED (stale or unsupported):** PR #47's `**Base (canonical
main):** 4282489` banner (superseded by the merged `6629161`); PR #47's
`1980 passed` presented as the suite total for the merged tree (kept only as
that branch's labeled evidence); PR #47's implication that the deadlock was
fully fixed by the lock reorder alone (it was one structural fix plus one
bounded retry — and, in this integration, one more structural fix; any
residual ordering is documented in §16, never claimed eliminated); any framing that
integration verification had already happened at the time of writing (it had
not); the claim implied by PR #47's environment line that this file describes
a single `b30aba9` snapshot (it now describes the integrated tree).

**NEVER MARKED COMPLETE:** two-live-account failover; Section F live
development task; MCP client; Self-QA engine; studio screens for File
Lab/evidence/allowlist/accounts; production deploy. All remain BLOCKED or NOT
IMPLEMENTED as stated in §12 and `COMPLETION_LEDGER.md`.
