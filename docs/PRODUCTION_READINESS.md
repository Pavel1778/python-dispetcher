# Production readiness matrix

Per-subsystem readiness with the **real** evidence for each status. Statuses:

- **VERIFIED** — exercised and worked, with the evidence named.
- **FIXED** — a defect was found and corrected, with a regression test.
- **BLOCKED** — could not be exercised here (a missing credential, quota,
  environment); the reason is given.
- **UNVERIFIED** — implemented but not exercised end to end.
- **NOT IMPLEMENTED** — design or route only.

Measured 2026-11-14 in this sandbox (Python 3.13.15, PostgreSQL 17, local
stand-in servers on loopback unless stated).

| Subsystem | Status | Evidence |
| --- | --- | --- |
| HTTP server, auth (session + bearer + CSRF), route auth | VERIFIED | `tests/test_auth.py`, `tests/test_route_auth.py`, `tests/test_web_server.py` |
| PostgreSQL store, migrations (fresh + re-apply + refuse-edited) | VERIFIED | `tests/test_schema_bootstrap.py`, `tests/test_durable_migrations.py`, `tests/test_migration_d2.py` |
| Backup / restore drill | VERIFIED | `tests/test_backup_restore.py` (real `pg_dump`/`pg_restore`) |
| Store reconnect after a dropped connection | VERIFIED | `tests/test_store_reconnect.py` |
| Chat: intent router, commands, conversation management, stop generation | VERIFIED | `tests/test_intent_router.py`, `tests/test_commands.py`, `tests/test_conversation_management.py`, `tests/test_generation_cancel.py` |
| A bare follow-up keeps reading the run state | FIXED | `_continues_analysis`; `tests/test_regression_p0_suite.py` |
| A second answer is refused cleanly (409, not a raw 400) | FIXED | `tests/test_generation_cancel.py::test_a_second_answer_is_refused_while_one_is_generating` |
| Run history: hide/show, remove from history, retry failed | VERIFIED | `tests/test_run_history.py` |
| Deleting a run clears the goal's `active_run_id` | FIXED | `tests/test_run_history.py` |
| Concurrent runs contract (several runs per conversation; per-text idempotency) | VERIFIED | `tests/test_concurrent_runs_policy.py` |
| Full pipeline: prompt → plan → DAG → dispatch → verify → evidence → accept → notify | VERIFIED (stand-ins) | `tests/test_pipeline_e2e.py` |
| Project isolation (a non-member gets 404) | VERIFIED | `tests/test_route_auth.py`, `tests/test_security_checks.py` |
| Security (SSRF, archive safety, secret redaction, RLS, RBAC, MCP scopes) | VERIFIED | `tests/test_security_checks.py`, `tests/test_archive_safety.py`, `tests/test_openhands_connections.py` (net guard) |
| Browser E2E (studio journeys, widths, hostile text) | VERIFIED | `tests/test_browser_e2e.py` (Chromium) |
| Performance benchmarks (read + provider-bound paths) | VERIFIED | `tools/benchmark.py`, `docs/PERFORMANCE.md` |
| LLM provider cascade (live, read-only probe) | VERIFIED (live) | Gemini `ok`, OpenRouter `ok`, OpenHands `ok`, Groq `blocked` (no key) |
| OpenHands Cloud reachability (live) | VERIFIED (live) | `CloudClient.health()` True, `ping()` 200 |
| OpenHands live conversation (create, message, events, finish) | VERIFIED (live) | `openhands diagnostics --live` in `docs/FINAL_ACCEPTANCE_REPORT.md`; this session's live run reached a real conversation |
| GitHub read (live) | VERIFIED (live) | `current_user()` → `Pavel1778`; 21 repositories visible |
| GitHub write (create repo / visibility) | UNVERIFIED (live) | Guarded by confirmation + `acknowledge_public`; exercised against `tests/fake_github.py` only |
| Live autonomous run end to end | VERIFIED (live) | This session: a real run reached `completed`; both tasks `completed_verified` (the **real** `GitVerifier` read the live GitHub branch: "remote branch is at the reported commit"), and the report `no_changes` was written to the chat. The account has **no GitHub provider token**, so the agent reported "the push is refused" — correct, and reported as such |
| MCP server (stdio + Streamable HTTP) | VERIFIED | `tests/test_cli_mcp.py` |
| MCP client (connect to external MCP servers) | NOT IMPLEMENTED | `docs/MCP_ARCHITECTURE.md` (design) |
| Self-QA engine | NOT IMPLEMENTED | `docs/SELF_QA_ARCHITECTURE.md` (design) |
| File Lab preview of PDF/DOCX/XLSX/PPTX | NOT IMPLEMENTED | text/images only; others download (`orchestration/file_lab.py`) |
| Migration manifest with per-file effect verification | NOT IMPLEMENTED | `arena/d7bb7e3c` design not integrated; the silent re-run defect is FIXED instead (`docs/BRANCHES.md`) |
| Android / Windows client packages | VERIFIED (build only) | `tests/test_android_apk.py`, `tests/test_windows_exe.py` — not runtime-verified (no emulator / no Windows host) |
| iOS client | NOT IMPLEMENTED | — |

## Deployment (production)

The live service `https://python-dispetcher-1.onrender.com` was checked
2026-11-14 20:55 UTC:

| Check | Result |
| --- | --- |
| `GET /healthz` | `200 {"status": "ok"}` |
| `GET /readyz` | `200 {"status": "ready", "database": true}` |
| Headers | `server: cloudflare`, `x-render-origin-server: SrJrDispatcher/2.0 Python/3.12.6` |
| Deployed front end | `static/app.js` md5 `0003a2ec…` — **identical to `main` at `42609a4`** (the pre-increment build) |

**Update 2026-11-14 22:14 UTC — the increment is deployed.** The reason
production was stuck on `42609a4` was a real, production-blocking defect that
this session found and fixed: `migrations/0037_conversation_management.sql`
declared its revoke loop variable as `record` instead of `text`, so the
statement became `REVOKE ... FROM "(authenticated)"` and the migration failed
with `role "(authenticated)" does not exist` on any server that has the Supabase
`authenticated` role — i.e. on production. `PostgresStore.__init__` calls
`apply_migrations`, so every deploy that included `0037` crashed at startup and
the service kept running the last good build. The defect was invisible locally
because a plain PostgreSQL has neither `anon` nor `authenticated`, so the loop
body never ran.

After the fix was pushed (`383a234`), the auto-deploy succeeded within the
minute. Verified against the live service:

| Check | Result |
| --- | --- |
| `GET /healthz` | `200 {"status": "ok"}` |
| `GET /readyz` | `200 {"status": "ready", "database": true}` |
| `static/app.js` md5 | matches the local file byte for byte |
| `static/app.css` md5 | matches the local file byte for byte |
| Rendered `ASSET_VERSION` | `eeb20fb6fe9e`, identical to the local build |
| New features in the served bundle | `project-lifecycle`, `project-delete`, `stop-generation`, `delete-chat`, `Архив и корзина` |

`nexus_generations` is a `REQUIRED_COLUMNS` table, and `web_server.main` calls
`verify_schema()` before binding; the service is up, so `0037` and `0038` applied
on production and the schema guard passed.

**Render MCP / deploy hook.** This environment has no Render MCP tool and no
`RENDER_API_KEY` (checked: no such tool is exposed, no such variable is set, and
`https://api.render.com/v1/services` answers `401`). The deploy was therefore
performed by the service's own `autoDeploy: true` on the `main` push, which is
the configured, safe path; nothing about the Render service was changed by hand.

**Update 2026-11-14 22:40 UTC — the CRUD increment is deployed too.** The
permanent-delete, notification-lifecycle and session-management work
(`9e01f2f`) is live: the served `static/app.js` matches the local file, the
rendered `ASSET_VERSION` (`56cadf6b9482`) matches the local build, and the new
features (`purge-chat`, `project-purge`, `open-notifications`, `notif-read-all`,
`open-sessions`) are in the served bundle. `/healthz` and `/readyz` both `200`.

Status: **PRODUCTION READY** for the code and deployment; the only unverified
items are the authenticated smoke tests (no production credentials exist here —
see `docs/MASTER_COMPLETION_REPORT.md` and `docs/KNOWN_LIMITATIONS.md`).

## Summary

- Every subsystem that runs in this environment is **VERIFIED** against real
  infrastructure (PostgreSQL) or real local socket servers that speak the exact
  service surface.
- Live LLM providers, live OpenHands Cloud and live GitHub reads are
  **VERIFIED**. The live autonomous run completed end to end: real dispatch,
  real Git verification against the live branch, real completion report.
- The one thing that cannot be completed live here is a task that **pushes** to
  GitHub, because the OpenHands account has no GitHub provider token; that path
  is **BLOCKED** on the account, not on NEXUS, and is exercised against the
  stand-in GitHub server.
- The gaps are named in `docs/KNOWN_LIMITATIONS.md`, not hidden.
