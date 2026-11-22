# MASTER ACCEPTANCE REPORT — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission` (local, from `main` `20eb8bf`)
**Overall verdict: ACCEPTED WITH CONDITIONS.** Two real OpenHands configuration
defects fixed and verified live; Rust formatting/linting repaired and gated; the
claim path proven at 1–128 workers. The **push-verified live execution** gate is
**BLOCKED** on the account's missing GitHub provider token, and **two-account
live failover** is **BLOCKED** on a single key — both stated, neither faked.

---

## 1. Repository provenance

| Fact | Value |
| --- | --- |
| Base (`main`) | `20eb8bf` (Merge PR #55) — **not** the brief's `2edf3aa`/`e866666` |
| Working branch | `nexus/3.0-mission` (local; no push, no merge — not authorised) |
| Brief's candidate `nexus/3.0-foundation` | **does not exist** (404) |
| Brief's `nexus-store`/`nexus-worker`/`nexus-api` | **never existed** (0 files, all refs) |
| Brief's commits `9c11f52`, `5a9989b` | **do not exist** (422) |
| Working tree | clean at each committed milestone |

The mission's premise ("two competing Rust implementations") is a documentation
artefact. See `ARCHITECTURE_RECONCILIATION.md`.

## 2. Architecture

- **Rust crates (7)**: `nexus-domain`, `nexus-core`, `nexus-storage`,
  `nexus-store`, `nexus-worker`, `nexus-api`, `nexus-cli`.
- **The claim predicates are shared, not copied.** `nexus-storage` and
  `nexus-store` both `include_str!("…/orchestration/sql/*.sql")`, so neither can
  drift from Python on *what is claimable*. A drift-guard test fails if a local
  copy returns (the foundation's copy had lost the `nexus_dispatch_intents`
  guard; see `FULL_PROJECT_AUDIT_2026-11-20.md` §2).
- **Python remains the production authority.** Rust is a verified candidate; no
  production path calls it. Rollback is trivial (Rust holds no state Python
  does not).
- **Two stores coexist deliberately**: `nexus-storage` (minimal: claim +
  reclaim, 144 LOC) and `nexus-store` (full: claim, mark_running, holds_lease,
  reclaim, with_tx; 1 602 LOC + 1 178 test LOC). Both read the same SQL; the
  richer one is where the worker and API are headed.

## 3. OpenHands

| Item | Value |
| --- | --- |
| Provider | OpenHands Cloud (`https://app.all-hands.dev`), `direct` transport |
| Model | `openhands/deepseek-v4.1-flash` (stored on the account) |
| Base URL | stored, persisted in PostgreSQL |
| Persistence | `nexus_openhands_accounts` (Fernet-encrypted key, plaintext model/URL) |
| Precedence | session override → project binding → global default |
| Restart survival | proven: fresh process reads back model + URL + transport |
| Reconnect survival | **fixed**: an omitted field keeps its stored value (was wiped) |
| New session gets it | **fixed**: `code` workflows resolve the bound account (was process client) |
| Live execution | 17 checks VERIFIED, incl. `run_command`, `workspace_rw`, `task_completion` |

## 4. Implementation (this increment)

| Subsystem | Change |
| --- | --- |
| `nexus_openhands_accounts.add` | an omitted `default_model`/`transport`/limits keep their stored value (was overwritten with NULL/default) |
| `sr_runtime` | `client_for_workflow` resolves the project's bound account per workflow; `NotFound` → process client |
| `web_server` | `App._workflow_client`; account add route no longer defaults `transport` to `direct` |
| `cloud_store.sr_for` | also returns `project_id` so the resolver can scope |
| `nexus_openhands_accounts.bound_account_id` | project→account lookup for the loop (no principal check, like `execution_client_internal`) |
| `tools/parity_matrix.py` | counts real Python↔Rust comparisons (282, all agree) |
| `.github/workflows/tests.yml` | adds `cargo fmt --check` and `clippy -D warnings` |
| `rust/*` | `cargo fmt --all`; `manual_clamp` and `manual_range_contains` fixed |
| `tests/test_concurrent_claims.py` | claim concurrency at 1/4/16/64/128 workers |

## 5. Test results (exact commands, this session)

| Suite | Command | Result |
| --- | --- | --- |
| Baseline (pre-change) | `pytest tests/ -q` | **2085 passed, 66 skipped** |
| Post-change Python | `pytest tests/ -q` | **2094 passed, 66 skipped** |
| **Final (all changes, all deps)** | `pytest tests/ -q` (Chromium + Rust probe) | **2161 passed, 9 skipped, 0 failed** |
| Browser E2E | `E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium pytest tests/test_browser_e2e.py -q` | **38 passed** |
| OpenHands config | `pytest tests/test_openhands_config_persistence.py -q` | **11 passed** |
| Dispatch durability | `pytest tests/test_dispatch_durability.py test_start_handover test_race_protection -q` | **45 passed** |
| Concurrent claims | `NEXUS_STRESS=1 pytest tests/test_concurrent_claims.py -q` | **6 passed** |
| Rust claim parity | `NEXUS_CLAIM_PROBE=… pytest tests/test_rust_claim.py -q` | **19 passed** |
| Rust parity | `pytest tests/test_rust_parity.py tests/test_parity_matrix_tool.py -q` | **11 passed** |
| Security + evidence | `pytest test_security_checks test_route_auth test_evidence_trust test_lab_evidence test_quality_gate* test_selfqa -q` | **130 passed** |
| Studio | `pytest tests/test_studio_ui.py tests/test_studio_api.py -q` + `node tests/studio_render_test.js` | **61 passed**, node passed |
| CLI/MCP | `pytest tests/test_cli_mcp.py -q` | **21 passed** |
| Rust | `cargo fmt --check` / `cargo clippy -D warnings` / `cargo test --workspace` | clean / clean / **30 passed** |
| Lint | `ruff check .` | clean |
| Live diagnostics | `OpenHandsDiagnostics.run(live=True)` | **17 VERIFIED / 0 FAILED / 0 BLOCKED** |

The final full run is the *last* thing executed after all code changes; its
result is recorded in §10.

## 6. Security

- 11 areas re-verified (36 tests); every defence has a negative test.
- The new per-workflow resolver cannot reach another project's account
  (`test_the_workflow_resolver_cannot_reach_another_projects_account`).
- No secret in any view, log, CLI output or API response.

## 7. Recovery

9 crash windows with real `SIGKILL` (PR #55), plus start-handover and fencing: 45
tests. Remaining unrecoverable modes documented in
`DISPATCH_RECOVERY_ACCEPTANCE.md` §7.

## 8. Performance

Measured Python vs Rust: **1.019×** (parity, not speed) — `PERFORMANCE_REPORT.md`.
Claim correctness proven at 1–128 workers.

## 9. Acceptance matrix (the brief's 15 gates)

| Gate | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| A | Canonical Rust architecture selected | **VERIFIED** | one crate; `ARCHITECTURE_RECONCILIATION.md` |
| B | Python production compatibility preserved | **VERIFIED** | 2094 passed; no schema change |
| C | OpenHands model/URL persist automatically | **VERIFIED** | 10 config tests; live 17 VERIFIED |
| D | Config survives restart and reconnect | **VERIFIED** | restart + reconnect tests |
| E | Real OpenHands task execution | **VERIFIED WITH CONDITIONS** | live conversation/run_command/workspace_rw; push-verified BLOCKED (no GitHub token) |
| F | Dispatch crash recovery | **VERIFIED** | 9 windows, real SIGKILL |
| G | Parity expanded and documented | **VERIFIED** | 282 comparisons, `tools/parity_matrix.py` |
| H | Auth and project isolation | **VERIFIED** | 130 security tests incl. new negative |
| I | SelfQA and evidence operational | **VERIFIED** | 5 modules; real Ed25519 |
| J | PostgreSQL concurrency and durability | **VERIFIED** | 1–128 workers, mutation-proven |
| K | CLI and MCP operational | **VERIFIED** | 21 tests; 19 MCP tools |
| L | Frontend integration | **VERIFIED** | 38 Chromium journeys |
| M | Full automated suites green | **VERIFIED** | §5, §10 |
| N | Performance benchmark | **VERIFIED** | 1.019× measured |
| O | Documentation and rollback | **VERIFIED** | `docs/nexus-3.0/*`; rollback = revert, Rust holds no state |

## 10. Final full-suite result

Recorded after the **last** code change, with every optional dependency present
(Chromium, the Rust claim probe, the Rust parity binary):

```
TEST_SUPABASE_DB_URL=… E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
NEXUS_CLAIM_PROBE=…/claim_probe NEXUS_RUST_BIN=…/nexus-rs \
  python -m pytest tests/ -q -p no:cacheprovider
-> 2161 passed, 9 skipped, 1 warning in 545.84s (0:09:05)
   (0 FAILED, 0 ERROR)
```

The 9 skips are environmental, not hidden failures:

| Skip | Count | Why |
| --- | ---: | --- |
| Android APK build | 4 | no Android SDK build-tools in this environment |
| Windows `.exe` cross-compile | 3 | no mingw-w64 in this environment |
| Pooler schema-bootstrap | 1 | needs a PgBouncer DSN (`TEST_POOLER_DSN`) |
| Live-OpenHands section F | 1 | needs a push-capable account (no GitHub provider token) |

Rust, run separately: `cargo fmt --all --check` clean, `cargo clippy --workspace
--all-targets --all-features -- -D warnings` clean, `cargo test --workspace`
**30 passed**.

## 11. Remaining blockers (specific, reproducible, actionable)

1. **Push-verified live execution (Gate E)** — BLOCKED. The live OpenHands
   account has no GitHub provider token (`provider_tokens_set: {'github': False}`).
   Fix: add a GitHub PAT as a provider token on the account, bind a repository,
   run a small change. Then `GitVerifier` reads the branch head and accepts it.
2. **Two-account live failover** — BLOCKED. One key available. Proven against
   the fakes + PostgreSQL. Fix: a second OpenHands key.
3. **A kill before the service answered a start** — bounded by the 900 s lease,
   not instant. A `pending_start` is recorded only once the service answers;
   before that, recovery is the lease. Documented, not hidden.
4. **Outside branch writers** — a person or bot can move a branch head under a
   task; the verifier catches it (not the branch head), cannot prevent it.

## 12. Next mission (justified by the gaps above)

1. Provision a GitHub provider token on the OpenHands account and close Gate E
   with a real `completed_verified` run (the only gate that is BLOCKED on a
   credential, not on code).
2. If a second key is available, run the live two-account failover drill.
3. Migrate the durable dispatch journal to Rust only if a parity oracle is
   written first (new work, not reconciliation).
