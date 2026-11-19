# NEXUS — Final Release Report

**Date:** 2026-11-16 (release/acceptance) · reconstructed and revalidated 2026-11-17
**Repository:** `Pavel1778/python-dispetcher`
**Base `main` SHA:** `6e5c2b410d531379c3599baa53492c83244551e5` (`6e5c2b4`, merge of PR #49)
**This integration branch:** `recovery/nexus-integration-revalidated` (reconstructed — see §18)
**Verdict:** **RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS** — plus the integration gates in §15/§18.

NEXUS is a working autonomous AI Development Lab. It starts reliably, applies
its schema, accepts a natural-language task, plans it into a dependency DAG,
dispatches the tasks to live OpenHands, verifies results independently, reports
honestly and notifies the owner. This was exercised end to end against the live
OpenHands Cloud in this session (§16, release/acceptance).

Two acceptance items were outstanding before the release-acceptance pass (a
second live OpenHands account; a live account with a GitHub provider token).
The GitHub-credential blocker was **fixed and live-verified** by PR #49 — NEXUS
now hands the sandbox a Git credential itself and a full autonomous run reached
`completed` with both tasks `completed_verified` (§16). The two-live-account
failover item remains external (§12).

> **Session 2026-11-16 (release/acceptance, PR #49).** This pass drove a
> **full autonomous run against live OpenHands** and found and fixed two real
> defects that had blocked live execution: the sandbox was given no Git
> credential (so no push could succeed), and a run started without a named
> branch ignored the project's bound branch. It also added a deterministic
> Self-QA engine and a user guide. The new sections are §16 (this session's
> live acceptance) and §17 (the incident and its clean-up); §14 lists the
> session's commits.
>
> **Reconstruction 2026-11-17.** The previously consolidated PR #46 → PR #47
> integration on `arena/71f72168-python-dispetcher` lost its commits when the
> sandbox was re-created; this branch rebuilds it on current `main` from the
> surviving working-tree changes, preserving everything PR #49 landed. The
> rebuilt integration is described in §18 and audited in
> `reports/INTEGRATION_CODE_REVIEW.md`; fresh verification evidence is in
> `reports/INTEGRATION_RECOVERY_EVIDENCE.md`.

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

42 migrations, `0001`–`0041` (`0041_branch_claim_indexes.sql` added 2026-11-18).

## 2. Implemented features

Projects; natural-language tasks; AI planning (Gemini→Groq→OpenRouter→OpenHands
cascade); dependency-aware autonomous runs; live OpenHands dispatch; independent
verification; durable retries and cycles; cancellation with stop signals;
multi-account pool with failover; project isolation and RBAC; the File
Laboratory; the Evidence Trust Engine; an OpenHands inventory engine; the
run-time profiler; notifications; the Studio; the CLI; MCP (stdio + HTTP);
backup/restore and safe restore points; **restart a finished run** and
**duplicate a finished run** (PR #47, see §18).

## 3. Completed workflows

**The core workflow works end to end, live** (release/acceptance): project
bound to a real GitHub **branch** → goal submitted in plain language →
planning produced a two-task DAG → dispatched to live OpenHands conversations →
the agent cloned, committed and pushed to the bound branch → both tasks
**`completed_verified`** → the run **`completed`** with a report and
notifications (§16). A persistence/restart drill over HTTP (graceful restart
and SIGKILL) preserved project/conversation/goal (§16).

## 4. Remaining limitations

See `COMPLETION_LEDGER.md` and `docs/KNOWN_LIMITATIONS.md`. The material ones:
no studio screen for File Lab/evidence/allowlist (routes exist); no MCP
client; PDF/DOCX/XLSX/PPTX are downloads, not previews. **Self-QA is implemented
as a deterministic engine** (`nexus qa run`, PR #49) — it is not yet an
automatic run gate and does not judge intent. One previously documented
concurrency inversion (cancel↔worker-settlement, executions-before-tasks) is
structurally closed in this integration; the system-wide ordering argument is
documented in §18 and `reports/INTEGRATION_CODE_REVIEW.md`, not claimed globally.

## 5. Security findings and fixes

Audited: authn/authz, project isolation, secret handling, SSRF, archive
extraction, path traversal, SQL injection, session handling. **Defects found
and fixed during this program:** the `direct` transport accepted `secrets` and
silently dropped them (PR #48) — now forwarded, with a regression test; a live
run could push to `main` when no branch was named (release/acceptance) — a run
with no named branch now uses the project's bound branch (§3, §17). The SQL
interpolation audit confirmed every interpolated fragment is a column-list
constant, never user input. 196 security tests pass.

The two run-management endpoints from PR #47
(`POST …/runs/{id}/restart`, `POST …/runs/{id}/duplicate`) are project-scoped
like every other run route (a non-member gets 404 — asserted in
`tests/test_run_history.py::test_restart_and_duplicate_are_member_only`), both
POST routes are registered in `tests/test_route_auth.py::PROTECTED_POST`, and
the two new MCP tools carry `SCOPE_RUN_CONTROL`. No protection was weakened.

## 6. Migration status

42 files, `0001`–`0041`, applied idempotently by the process on first connect
(recorded in `schema_migrations`). Gaps `0012`/`0013` are reserved for the
deferred File Lab and are intentional. An edited, applied migration is refused
(`MigrationError`); the one registered production correction (0037) converges
via `MIGRATION_CORRECTIONS`. Fresh application of the full chain under this
recovery: see `reports/INTEGRATION_RECOVERY_EVIDENCE.md`.

## 7. Full test results

See §13 for the attributed numbers, `reports/INTEGRATION_RECOVERY_EVIDENCE.md`
for the reconstructed tree's fresh run (this branch), and
`NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md` for the verdict.

## 8. Browser E2E results

`tests/test_browser_e2e.py` — 37 Chromium journeys (the full journey, hostile
text, failure and retry, phone and five other widths): **37 passed**
(release/acceptance session, PostgreSQL 17.11 + Chromium 159.0.8154.6). Not
re-run on this reconstructed tree unless a Chromium binary is available; see
the evidence report for exactly what was run.

## 9. OpenHands live test results

- Cloud reachable: `health()=True`, `ping()` 200, version **1.92.0**,
  `compatibility: compatible`.
- LLM cascade (live probe): Gemini `ok`, OpenRouter `ok`, Groq/OpenHands
  `blocked` (no key).
- **First autonomous run**: conversation created `READY` on a live sandbox, ran
  `running → finished` with 280+ events; the verifier recorded
  `completed_unverified` (no GitHub credential was being handed to the
  sandbox yet — the honest verdict at that moment); a durable retry was
  scheduled; cancellation stopped the run.
- **Release-acceptance run** (after the credential and bound-branch fixes):
  full success, both tasks `completed_verified`, run `completed` with a report
  (§16).

## 10. Performance results

`tools/perf_bench.py` against a real local PostgreSQL 17: DB p99 **69 ms** and
**0 errors** at 100 concurrent clients (the pool serves them and returns every
connection); claim throughput **1345/s** at 50 workers. Absolute numbers differ
from the historical machine; no regression introduced. Not re-measured on the
reconstructed tree (no new load was applied); new observations, if any, are in
the evidence report. No claim beyond the historical measurement is made in this
document.

## 11. Deployment instructions

`DEPLOY.md` (Render blueprint, Docker Compose, Dockerfile, systemd worker unit),
`docs/ENVIRONMENT.md` (every variable), `DEPLOYMENT_CHECKLIST.md` (PR #47's
operator checklist) and `docs/USER_GUIDE.md` (PR #49). Minimal local start:

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
2. ~~Section F live development task~~ — **VERIFIED (2026-11-16)** by PR #49: a
   full autonomous run reached `completed` with both tasks
   `completed_verified` against live OpenHands; NEXUS injected its own Git
   credential into the sandbox (no provider token on the account needed) and
   the bound-branch fix removed the push-to-default-branch risk. See §16.

Neither item is faked anywhere in this repository.

## 13. Exact commit SHAs and test results — attributed per environment

Nothing in this section is cross-foldable. Each block names its environment.

Historical, `fix/approval-notification` session (now part of `main`):

- Base `main`: `42824896e388c97e218a8915cab56a8498a28473`.
- This session's branch: `fix/approval-notification` @ `0065406` (5 commits).
- Full suite (fresh PostgreSQL 17): **1976 passed, 9 skipped** — environmental
  (3 mingw, 4 Android SDK, 1 pooler DSN, 1 live).
- `ruff check .`: clean. Studio render test: passed. Browser E2E: 37/37.

Historical, PR #47 branch session (`feat/run-restart-duplicate` @ `b30aba9`,
before integration), PostgreSQL 17.11 + Chromium 159.0.8154.6:

- Full suite: **1980 passed, 9 skipped, 0 failed** (31:40). Reference clean
  `main` @ `4282489`: **1973 passed, 9 skipped, 0 failed**.
- `ruff check .`: All checks passed. Studio render test: passed. E2E: **37 passed**.
- Deadlock stress (cancel test ×12): **0/12 failed** (was 1/6 on clean `main`).

Historical, PR #46 / PR #47 integration on `arena/71f72168-python-dispetcher`
(local **PostgreSQL 16.2**; the integration commits `ec0d553…baa3251` were
**lost with a re-created sandbox and never existed on the remote**):

- Full suite (`pytest tests/ -q`, fresh database): **1951 passed, 49 skipped,
  0 failed** (35:16). The evidence log `/home/user/pytest_integration_final.log`
  and the stress logs were **destroyed with that sandbox** and cannot be
  verified — treat these as recorded claims, not reproducible evidence.
- 24-test cancel/reclaim/race/deadlock selection × 30 reps: **720 runs, 0
  failures** (same caveat). 37 of the 49 skips were browser-E2E (no Chromium).

Fresh, this reconstruction (`recovery/nexus-integration-revalidated`):

- See `reports/INTEGRATION_RECOVERY_EVIDENCE.md` for the exact commands,
  environment and per-gate results, and
  `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md` for the verdict. Numbers there
  are the only ones attributable to this branch.

## 14. This session's commits (historical)

| SHA | Change |
|---|---|
| `4aedcf8` | `approval_required` notification when a plan awaits confirmation |
| `c843ec6` | `task_started` notification when a task actually starts |
| `c3e9eea` | `run_blocked` notification when a run needs a person |
| `6de51dc` | fix: the direct transport forwards `secrets` to the sandbox create |
| `0065406` | test: poll for the rework's running state (determinism) |
| `6629161` | merge: PR #46 — F-01 `file://` `NEXUS_URL` guard + F-06 bounded transaction-level retry for transient `DeadlockDetected` in `set_run_state`/`set_run_state_internal` |
| `6e5c2b4` | merge: PR #49 — release acceptance (live-verified autonomous run, Git credential injection, bound-branch dispatch, Self-QA engine, USER_GUIDE) |

Commits of the reconstructed integration are listed in §18.

## 15. Release readiness verdict

**RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS.**

The product is usable today: it starts, plans, dispatches to live OpenHands,
verifies, reports and notifies. The remaining blocked item requires a credential
this environment does not have (§12) and is documented with exact acceptance
steps. Readiness of the *integrated* tree additionally requires the gates in
§18: combined CI (owner-pushed), and the fresh evidence in this branch.

---

## 16. Live acceptance (2026-11-16, release/acceptance — PR #49)

A **full autonomous run through NEXUS against the live OpenHands Cloud**:

1. A project was created and bound to a real GitHub repository and a **non-default
   branch** (`nexus-live-acceptance`).
2. A goal was submitted in plain language; the run **planned** it into a
   **two-task DAG**.
3. Each task was dispatched to a **live OpenHands conversation** on a real
   sandbox.
4. The agent **cloned** the repository, made the change, **committed and pushed**
   to the bound branch (verified on GitHub: `main` untouched, the branch moved).
5. NEXUS **verified** both tasks — the branch head was the commit the agent
   named — recording **`completed_verified`**.
6. The run reached **`completed`** and a **report** was written (`outcome:
   success`) and a `run_report` notification raised. Task/run notifications
   (`task_started`, `task_completed_verified`, `task_retry`, `goal_completed`)
   were persisted.

**Persistence / restart drill** (real HTTP, no SQL): a project, a conversation
and a goal were created, the server was stopped **gracefully** and then
**SIGKILLed**, and each restart preserved all three; the store reconnected and a
new write succeeded after the kill.

**Self-QA**: `nexus qa run --repo . --commit HEAD` → `passed` (real `ruff` and
`pytest` in an isolated clone).

**Studio**: Chromium E2E **37/37** (phone 390×844 touch, tablet, desktop,
keyboard-up, project switching, chat, run monitoring, notifications).

## 17. Incident and clean-up (PR #49)

Before the branch fix (§3), a live run pushed a one-line file
(`NEXUS_ACCEPTANCE.md`) to **`main`** (commit `198d5e22`, parent `25bd484`)
instead of the bound branch, because the run's task branch was empty and the
agent was told the default branch. The file is **removed with a normal forward
commit**; **no history was rewritten**. After the fix, a re-run pushed to the
bound branch and `main` was verified unchanged. The cause is fixed and covered
by two regression tests.

## 18. Reconstructed integration — PR #46 / PR #47 revalidation (2026-11-17)

**What was lost.** The consolidated integration on
`arena/71f72168-python-dispetcher` (recorded as commits `ec0d553` → `2fbdcad`
→ `e2c7575` → `ce21e47` → `baa3251`) was carried only by a pre-reset sandbox;
the clone was re-created shallow from `main` and every integration SHA is
absent (locally and on GitHub — HTTP 422). Recovery from Git objects was
impossible; no commit with those hashes was ever pushed.

**What was reconstructed.** On this branch, from current `main`
(`6e5c2b4`):

1. `merge` of PR #47's head (`f6e77c9`, fetched from `refs/pull/47/head`,
   byte-verified against `gh`) — restart/duplicate runs, the PR #47
   `reclaim_expired` run-row-first `FOR UPDATE`, Studio controls, MCP tools,
   CLI commands, Studio buttons; conflict resolution preserved the union of
   both sides (PR #49's Self-QA/live-acceptance content and PR #47's sprint
   content) and is documented in `reports/INTEGRATION_CODE_REVIEW.md`.
2. Route-authorization regression: restart/duplicate POST routes added to
   `tests/test_route_auth.py::PROTECTED_POST` (unauthenticated → 401).
3. Structural close of the cancel↔worker-settlement inversion (F-06): the
   cancel settlement in `_apply_run_state` blocks unfinished task rows
   **before** closing their executions — the order every worker-side settle
   takes; pinned by `tests/test_cancel_lock_order.py`.
4. Documentation reconciliation (this file, `COMPLETION_LEDGER.md`,
   `docs/CLI_REFERENCE.md`, `docs/CRUD-MATRIX.md`, `DEPLOYMENT_CHECKLIST.md`)
   including the drift corrections listed in §19.

**Provenance, stated plainly:** this is a **reconstruction**, not a recovery of
the original commit objects. Its commits have new, real SHAs (see
`git log --graph`); content is derived from the surviving working-tree
snapshot (SHA-256 manifest in `reports/INTEGRATION_RECOVERY_BASELINE.md` and
the preservation snapshot). Every environment-specific number from the lost
sandbox remains labeled historical and unverifiable.

**Lock order (from the code, not from a claim).** Every audited write path
acquires locks in the order **run → task → execution** (FK-shared task row
last inside executions via new-row inserts only). No unobserved ordering is
claimed eliminated; PostgreSQL's deadlock detector plus the PR #46 bounded
retry remain the backstop. No global deadlock-freedom is claimed.

**Fresh verification:** see `reports/INTEGRATION_RECOVERY_EVIDENCE.md`
(commands, environment, per-gate results, stress rounds, migration results)
and `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md` (verdict). Combined CI has
**not** been run — that requires an owner-authorized push.

**What the reconstruction does not prove:** PostgreSQL-17-specific behaviour
unless PG 17 was actually used here; Chromium E2E unless a binary was present;
cross-project/duplicate/restart behaviour beyond the recorded tests; or the
absence of defects outside the audited paths.

## 19. Reconciliation record — what was retained, updated, discarded, and why

Baseline for this reconciliation: `main` @ `6e5c2b4` (post PR #49). PR #47's
document was read as a source of facts, not as structure.

**RETAINED (main baseline, incl. PR #48 and PR #49):** the architecture,
feature, security, migration, live-OpenHands, performance and deployment
narratives; the release/acceptance §16 live-acceptance record and §17 incident
record verbatim; the performance §10 historical numbers (labeled historical);
the historical §13/§14 tables (extended, not rewritten); the §15 verdict
wording (qualified by the integration gates).

**RETAINED (from PR #47):** the restart/duplicate definitions and semantics
(§2, §5, §18); the limits and blockers that remained true; the deadlock
root-cause description (as complemented by #46's retry and this integration's
task-before-executions swap); PR #47's own test evidence (labeled as that
branch's environment in §13); `DEPLOYMENT_CHECKLIST.md`.

**UPDATED:** header metadata (base `25bd484` → `6e5c2b4` for this branch;
recovery banner added); §2 (restart/duplicate); §4 (pointer to §18 + the
deterministic Self-QA correction); §5 (the two new endpoints' posture); §7/§8
(evidence pointers); §9 (release-acceptance outcome kept, first-attempt
account retained honestly); §12 (Section F flipped to VERIFIED — PR #49);
§13 (all numbers attributed per environment; the lost integration labeled
unverifiable; fresh numbers live in the evidence report); §14 (PR #46 and
PR #49 merges recorded); §15 (qualified by §18's gates); new §18 and this
section.

**Drift corrected during the reconstruction (stale even before this branch):**

- `docs/KNOWN_LIMITATIONS.md` and `COMPLETION_LEDGER.md` already reflect PR
  #49 (Self-QA implemented-deterministic; Section F verified); this report's
  §4 and §12 were inconsistent with both — corrected above and listed here.
- The earlier claim that the integrated tree had ever run on PostgreSQL 17 —
  it had not (local 16.2; PG 17 only in CI for other branches).

**DISCARDED (stale or unsupported):** PR #47's `Base: 4282489` banner; the
`1980 passed` suite total as if it applied to the merged tree (kept only as
labeled branch evidence); the implication that the deadlock was eliminated by
the reorder alone (one structural fix plus one bounded retry — plus, in this
reconstruction, the cancel-path structural fix); any framing that the lost
integration had been fully verified (it had not, and its artifacts are gone);
the phrase "no Self-QA engine" anywhere in this file (PR #49 landed one).

**NEVER MARKED COMPLETE:** two-live-account failover; production deploy; MCP
client; Self-QA as an automatic run gate; studio screens without a live pass;
combined CI; the release tag.
