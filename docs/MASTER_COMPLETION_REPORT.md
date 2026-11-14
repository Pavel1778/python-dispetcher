# NEXUS — master completion, synchronization and release (2026-11-14)

The engineering report for the request "NEXUS — Master Engineering Completion,
Repository Synchronization & Production Release". Sections A–I follow the
request's own lettering. Every claim has an artefact; nothing is asserted that
was not measured.

---

## A. Git

- **Initial commit:** `42609a4` (the merge of PR #37) — this is also what
  `origin/main` was at the start, and what the running deployment still serves.
- **Final commit:** `c2b5fe8` (see the push status below; the report's own commit
  is added after it).
- **Commits added this session:** 17 on top of `42609a4` (the pre-existing 8
  from the "Ultimate increment" plus 9 from this session).
- **Push:** `git push origin main` succeeded, `42609a4..f3767c9` then
  `f3767c9..c2b5fe8`. No force push. Local `HEAD` and `origin/main` both read
  `c2b5fe8` (verified by `git rev-parse` and the GitHub commits API).
- **Changed files (this session's own commits):** migration
  `0038_project_lifecycle.sql`; `nexus_projects.py`, `nexus_conversations.py`,
  `nexus_specs.py`, `cloud_store.py`, `nexus_lab_store.py`, `web_server.py`,
  `studio/app.js`; tests `test_project_lifecycle.py`,
  `test_regression_p0_suite.py`, `test_concurrent_runs_policy.py`,
  `test_schema_bootstrap.py`, `test_generation_cancel.py`, `test_chat_workflow.py`;
  docs `ARCHITECTURE.md`, `OPERATIONS.md`, `PRODUCTION_READINESS.md`,
  `BRANCHES.md`, `INTEGRATION_TESTING.md`, `KNOWN_LIMITATIONS.md`,
  `GAP-ANALYSIS.md`, `GROUND-TRUTH-BUILD-LINT-TEST.md`,
  `P0-INCIDENT-FINAL-REPORT.md`, `MASTER_COMPLETION_REPORT.md`; `AGENTS.md`,
  `README.md`, `CHANGELOG.md`, `docs/PERFORMANCE.md`,
  `tools/verify_build_lint_test.sh`.
- **PRs:** PR #38 closed (its work ported). PR #39 left open for its own rebase
  (see B). No new PR was created; the work is on `main`.

## B. Branches

- **Found:** 20 remote branches + `main`.
- **Already merged (13):** `arena/2161f211`, `arena/44b9ec6d`, `arena/4f942c7f`,
  `arena/5ee71838`, `arena/78a1a036`, `arena/8b83d99f`, `arena/a54ad8b6`,
  `arena/c56cbbf3`, `arena/dcb992a2`, `audit/pr29`, `docs/intent-and-env`,
  `nexus/chat-first-workflow`, `nexus/durable-autonomy-multiconn` — all ancestors
  of `main`, **deleted** after a `backup/merged-2026-11-14/*` tag.
- **Ported, not merged (1):** `arena/ab0d7665` (PR #38). Its unique work
  (`_continues_analysis`, the §10 suite, the concurrent-runs contract, the P0
  report, the project lifecycle) is in `main`; the branch is kept (backup tag
  `backup/pre-master/arena_ab0d7665-python-dispetcher`). PR closed.
- **Kept for review (1):** `nexus/perf-reliability-lifecycle` (PR #39) — a large
  parallel contribution whose migrations collide with `main`'s `0037`/`0038` and
  whose merge would revert the increment that landed since its base.
- **Kept (framework / superseded):** `arena/d7bb7e3c`, `nexus/mainline-hardening`
  (migration-integrity framework; the one real fix is in `main`),
  `nexus/continue-longtask-hardening` (stale snapshot, content already in `main`).
- **Kept (content ported):** `nexus/gap-analysis`, `nexus/ground-truth-verify`.
- **Unique unfinished changes:** none that are not either in `main` or in an open
  PR. Each kept branch has a `backup/pre-master/*` tag. See `docs/BRANCHES.md`.

## C. Fixes implemented

| Problem | Cause | Components | Solution | Test |
| --- | --- | --- | --- | --- |
| An edited, already-applied migration was re-run silently | `apply_migrations` wrote the new checksum over the old (`ON CONFLICT DO UPDATE`) and executed the changed body | `cloud_store.py` | Refuse with `MigrationError`, naming the file; a new file still applies | `test_schema_bootstrap.py::test_an_edited_migration_is_refused_not_reapplied` |
| A second chat answer was a raw 400 and left the chat `thinking` | `start_generation`'s `ValueError` surfaced unhandled | `web_server.py` | 409 with a clear message; conversation set idle; the running answer stays stoppable | `test_generation_cancel.py::test_a_second_answer_is_refused_while_one_is_generating` |
| Deleting a run left the goal pointing at a gone run | `nexus_goals.active_run_id` is a plain uuid column (no FK) | `nexus_lab_store.py` | Clear it in the same transaction | `test_run_history.py` |
| A bare follow-up question lost the run-state context | the Intent Router classifies a message in isolation | `web_server.py` (`_continues_analysis`) | A plain question after an `analysis` turn stays `analysis` | `test_regression_p0_suite.py` |
| A project could not be archived or retired | no lifecycle states | `nexus_projects.py`, migration `0038` | Archive / soft delete / restore, with guards | `test_project_lifecycle.py` (6) |
| `_count_runs` in tests counted history as new work | a test helper relied on `?include_hidden=1` being ignored | `test_chat_workflow.py`, `test_regression_p0_suite.py` | Ask the active list | `test_chat_workflow.py::test_a_spent_plan_does_not_start_a_second_run` |

## D. Testing

- **pytest (full suite, PostgreSQL 17):** `1854 passed, 9 skipped, 0 failed`
  locally; **`1857 passed, 6 skipped, 0 failed` on CI** (GitHub Actions run for
  `8fba2b6`; Chromium is present there, so the browser E2E runs and there are
  fewer skips).
- **ruff:** `All checks passed!`
- **Browser E2E (Chromium):** `37 passed`.
- **Migrations:** fresh install, re-apply (no-op), refuse-edited — all green
  (`test_schema_bootstrap.py`, `test_durable_migrations.py`, `test_migration_d2.py`).
- **Security:** `test_security_checks.py`, `test_archive_safety.py`,
  `test_route_auth.py`, `test_openhands_connections.py` (net guard) — green.
- **Regressions:** the §10 suite and the concurrent-runs contract pass; the
  pre-existing load-sensitive `test_acceptance_recovery` case passed in the full
  run.
- **Skips (9):** 4 Android APK (no SDK build-tools), 3 Windows `.exe` (no
  mingw-w64), 1 pooler schema-bootstrap (no `TEST_POOLER_DSN`), 1 section-F live
  variant.

## E. Performance

`tools/benchmark.py` (re-measured this session; full table in
`docs/PERFORMANCE.md`): the read paths are 0.5–5 ms p50, the provider-bound
paths (`chat_turn` 27 ms, `start_run` 65 ms) are dominated by the stand-in LLM,
and the store's own aggregate query is sub-millisecond. No regression versus the
documented baseline and **no speedup is claimed** — the comparison table is the
instrument for any such claim.

## F. Documentation

Created: `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/PRODUCTION_READINESS.md`, `docs/BRANCHES.md`,
`docs/INTEGRATION_TESTING.md`, `docs/KNOWN_LIMITATIONS.md`,
`docs/MASTER_COMPLETION_REPORT.md`. Ported: `docs/GAP-ANALYSIS.md`,
`docs/GROUND-TRUTH-BUILD-LINT-TEST.md`, `docs/P0-INCIDENT-FINAL-REPORT.md`,
`tools/verify_build_lint_test.sh`. Updated: `README.md`, `AGENTS.md`,
`CHANGELOG.md`, `docs/PERFORMANCE.md`.

## G. Production

- **Deployment:** `https://python-dispetcher-1.onrender.com` — **up and healthy**
  (`/healthz` 200, `/readyz` 200 with `database: true`), behind Cloudflare, on
  Python 3.12.6.
- **Deployed version:** the served `static/app.js` is byte-identical to `main` at
  `42609a4` (the pre-increment build). The increment is **not yet deployed**;
  `render.yaml` has `autoDeploy: true` but the assets have not changed, and no
  `RENDER_API_KEY`/deploy hook exists here to trigger one. Rebuild by hand in
  the Render dashboard.
- **PostgreSQL / OpenHands / GitHub / LLM (live):** all reachable and exercised
  (see H).

## H. Outstanding tasks

**VERIFIED (live):**
- LLM cascade read-only probe: Gemini `ok`, OpenRouter `ok`, OpenHands `ok`,
  Groq `blocked` (no key).
- GitHub read: `current_user()` → `Pavel1778`, 21 repositories.
- OpenHands Cloud: `health()` true, `ping()` 200.
- **Live autonomous run end to end:** a real run reached `completed`; both tasks
  `completed_verified` (the real `GitVerifier` read the live GitHub branch:
  "remote branch is at the reported commit"); the report `no_changes` was written
  to the chat.

**VERIFIED (against the real PostgreSQL / local stand-in servers):** the whole
suite (D).

**FIXED:** the six items in C.

**BLOCKED:**
- **A live task that pushes to GitHub.** The OpenHands account has no GitHub
  provider token (`provider_tokens_set: {'github': None}`), so the agent reports
  "the push is refused". The push path is exercised against `tests/fake_github.py`.
  Action: give the account a GitHub provider token.
- **Triggering a production deploy.** No `RENDER_API_KEY`/deploy hook here.
  Action: rebuild by hand in the Render dashboard.

**NOT IMPLEMENTED:** the MCP client (connect to external MCP servers); the
Self-QA engine; File Lab preview of PDF/DOCX/XLSX/PPTX; an iOS client; the
migration manifest/effect framework.

**NOT TESTED (live):** a two-live-account failover (only one OpenHands key);
GitHub writes (create repository / change visibility) live.

## I. Final status

**RELEASE READY — DEPLOYMENT PENDING.**

The code is published to `origin/main` (`c2b5fe8`), the full suite is green
(1854 passed / 0 failed), ruff is clean, and the live OpenHands / GitHub / LLM /
PostgreSQL were exercised — including a real autonomous run that completed with
verified tasks. The one thing not confirmed is that the **production deployment**
serves this commit: the live service is healthy but still on the `42609a4`
build, and no deploy hook exists here. The two live BLOCKED items (a GitHub push
token on the OpenHands account; a deploy trigger) are the only outstanding
actions.
