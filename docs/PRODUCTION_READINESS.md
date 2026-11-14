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
