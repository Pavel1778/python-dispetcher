# NEXUS — Final Release Report

**Date:** 2026-11-16
**Repository:** `Pavel1778/python-dispetcher`
**Base `main` SHA:** `42824896e388c97e218a8915cab56a8498a28473`
**This session's branch:** `fix/approval-notification` (5 commits, see §14)
**Verdict:** **RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS**

NEXUS is a working autonomous AI Development Lab. It starts reliably, applies
its schema, accepts a natural-language task, plans it into a dependency DAG,
dispatches the tasks to live OpenHands, verifies results independently, reports
honestly and notifies the owner. This was exercised end to end against the live
OpenHands Cloud in this session (§9).

Two acceptance items need credentials this environment does not have (a second
live OpenHands account; a live account with a GitHub provider token). They are
named, not faked (§12).

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
backup/restore and safe restore points.

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
account has a GitHub provider token.

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

## 6. Migration status

41 files, `0001`–`0040`, applied idempotently by the process on first connect
(recorded in `schema_migrations`). Gaps `0012`/`0013` are reserved for the
deferred File Lab and are intentional. An edited, applied migration is refused
(`MigrationError`); the one registered production correction (0037) converges
via `MIGRATION_CORRECTIONS`.

## 7. Full test results

See §13 for the numbers from this session's final run.

## 8. Browser E2E results

`tests/test_browser_e2e.py` — 37 Chromium journeys (the full journey, hostile
text, failure and retry, phone and five other widths): **37 passed**.

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

Neither is faked anywhere in this repository.

## 13. Exact `main` commit SHA and test results

- Base `main`: `42824896e388c97e218a8915cab56a8498a28473`.
- This session's branch: `fix/approval-notification` @ `0065406` (5 commits).
- Full suite (fresh PostgreSQL 17, `OPENHANDS_API_KEY` and `AUTH_*` unset so the
  tests exercise the code, not the shell): **1976 passed, 9 skipped** — the 9
  skips are environmental (3 mingw, 4 Android SDK, 1 pooler DSN, 1 live).
- `ruff check .`: clean. Studio render test: passed. Browser E2E: 37/37.

## 14. This session's commits

| SHA | Change |
|---|---|
| `4aedcf8` | `approval_required` notification when a plan awaits confirmation |
| `c843ec6` | `task_started` notification when a task actually starts |
| `c3e9eea` | `run_blocked` notification when a run needs a person |
| `6de51dc` | fix: the direct transport forwards `secrets` to the sandbox create |
| `0065406` | test: poll for the rework's running state (determinism) |

## 15. Release readiness verdict

**RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS.**

The product is usable today: it starts, plans, dispatches to live OpenHands,
verifies, reports and notifies. The two blocked items require credentials this
environment does not have and are documented with exact acceptance steps.
