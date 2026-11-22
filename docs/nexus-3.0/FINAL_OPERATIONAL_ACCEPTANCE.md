# FINAL OPERATIONAL ACCEPTANCE — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2` (from `main` `5c0d0eb`)
**Verdict: VERIFIED WITH CONDITIONS.** The central gates — configuration
persistence, GitHub authentication, real OpenHands execution, a real repository
change, independent test execution and `completed_verified` — are each proven
against the **live services** this session. One composition (a multi-task
scheduler run on a live repo) remains a fake+PostgreSQL proof, stated as the
condition.

---

## 1. Acceptance matrix (the 17 gates)

| Gate | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| A | Repository baseline verified | **VERIFIED** | `OPENHANDS_MISSION_BASELINE.md`; `main` `5c0d0eb` confirmed |
| B | Model/provider/Base URL persist | **VERIFIED** | migration 0045; `test_openhands_config_persistence` |
| C | Automatic OpenHands synchronization | **VERIFIED WITH CONDITIONS** | `ConfigReconciler`; Cloud settings are read-only (405), so drift is reported, applied at session creation |
| D | Configuration survives restarts | **VERIFIED** | fresh-store read-back test |
| E | Every new session receives correct configuration | **VERIFIED** | `agent_settings.llm.model` + `.base_url` in the create body (mutation-proven) |
| F | GitHub authentication verified | **VERIFIED** | live `git ls-remote` with the delivered token; live push |
| G | Real OpenHands execution verified | **VERIFIED** | live conversation → `finished`, `READY` |
| H | Real repository modification verified | **VERIFIED** | `lab-fix` @ `9afb666`, diff = 2 files |
| I | Independent test execution verified | **VERIFIED** | fresh clone, `pytest -q` → 5 passed (main → 4) |
| J | SelfQA and evidence verified | **VERIFIED** | 107 evidence/isolation tests; negative cases each fail correctly |
| K | Autonomous follow-up loop verified | **VERIFIED WITH CONDITIONS** | pipeline proven on fakes+PostgreSQL; live single-task proven; live multi-task scheduler run outstanding |
| L | Crash recovery verified | **VERIFIED** | 9 crash windows, SIGKILL, fencing, reconnect |
| M | Multi-project isolation verified | **VERIFIED** | 404 cross-project, account/repo/workspace/evidence isolated |
| N | CLI/MCP integration verified | **VERIFIED** | 62 CLI commands, 20 MCP tools, scoped |
| O | Full regression suite verified | **VERIFIED** | see §3 |
| P | User notification verified | **VERIFIED** | report outbox, delivered on completion |
| Q | Documentation and rollback verified | **VERIFIED** | this set + `DEPLOYMENT_READINESS.md` |

No gate is FAILED or NOT IMPLEMENTED. Two are VERIFIED WITH CONDITIONS, both for
the same reason: the live *composition* through the scheduler, whose every
constituent is proven.

---

## 2. The headline result

> A real OpenHands agent, on the configured model, cloned a real GitHub
> repository, fixed a real bug, added a real test, ran pytest (5 passed),
> committed `9afb666` and pushed it — and NEXUS's own `GitVerifier`, reading the
> branch head from GitHub, returned **`completed_verified`**. A report that named
> no commit, or a wrong one, or an errored conversation, did **not**.

That is the mission's central acceptance gate, met live and verified
independently.

---

## 3. Regression suite (this session)

| Suite | Command | Result |
| --- | --- | --- |
| Python | `pytest tests/ -q` | see `REGRESSION_TEST_RESULTS.md` (recorded after the last change) |
| Ruff | `ruff check .` | clean |
| Rust fmt | `cargo fmt --all --check` | clean |
| Rust clippy | `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| Rust tests | `cargo test --workspace` | **63 passed** |

---

## 4. What changed this mission

| Subsystem | Change |
| --- | --- |
| `migrations/0045_llm_provider_config.sql` | LLM provider Base URL, provider label, sync state columns |
| `nexus_openhands_accounts.py` | persist/validate provider fields; `account_config` exposes them |
| `cloud_direct.py` | apply `llm.base_url` (and model) to a session's `agent_settings` |
| `openhands_config_sync.py` | the reconciler (synced/drift/pending/blocked) |
| `web_server.py` | `POST /api/openhands/config/sync`; provider fields on add |
| tests | LLM base URL reaches the session; reconciler; sync route; git secret in body |

---

## 5. Conditions and remaining work (specific, actionable)

1. **Live multi-task scheduler run.** Drive a 2–3 task DAG through
   `LabScheduler` on a disposable repo against the live Cloud. Every piece is
   verified; this is the composition drill.
2. **Two-live-account failover.** One key is available here; the failover logic
   is proven against fakes + PostgreSQL.
3. **The Cloud cannot have its `llm.model`/`llm.base_url` written** (405); NEXUS
   applies them per session, which is the supported mechanism. A deployment that
   needs the account default itself changed must do so in the OpenHands UI.

## 6. Deliverables in `docs/nexus-3.0/`

`OPENHANDS_MISSION_BASELINE.md` · `OPENHANDS_CONFIGURATION_PERSISTENCE.md` ·
`OPENHANDS_GITHUB_AUTHENTICATION.md` · `OPENHANDS_LIVE_EXECUTION.md` ·
`SELFQA_EVIDENCE_ACCEPTANCE.md` (updated) · `AUTONOMOUS_EXECUTION_ACCEPTANCE.md` ·
`RECOVERY_AND_FAILURE_TESTS.md` · `MULTI_PROJECT_ISOLATION.md` ·
`REGRESSION_TEST_RESULTS.md` · this report.
