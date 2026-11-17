# NEXUS 2.0 — Final Acceptance Report

Commit under test: `nexus/2.0-integration`. Base: `main` @ `6629161`.
Date: 2026-11-16. Environment: Debian, Python 3.13, PostgreSQL 17.11, Rust
1.100, Chromium.

This report states what was **run** and what it produced. Where something could
not be run, it says so and why; it is never marked complete on the strength of
code existing. The status vocabulary is fixed: **VERIFIED**, **FAILED**,
**SKIPPED** (an environmental skip), **BLOCKED** (a missing resource), **NOT
VERIFIED** (not run).

---

## 1. Headline

| Suite | Result |
|---|---|
| Python test suite (all but browser) | **1974 passed, 9 skipped, 0 failed** (28:44) |
| Browser E2E (Chromium) | **37 passed** |
| Rust workspace (`cargo test`) | **29 passed, 0 failed** |
| Python/Rust parity | **9 passed** |
| Project isolation | **8 passed** |
| Live OpenHands diagnostics | **16 VERIFIED, 0 FAILED, 0 BLOCKED, 1 UNVERIFIED** |
| Live autonomous run through NEXUS → OpenHands | **VERIFIED** (dispatched, agent worked, honest verdict) |

The 9 skips are environmental and are listed in section 6. No assertion was
weakened to obtain any of these numbers.

---

## 2. Integration (Phase 0)

Verified by `git log --oneline nexus/2.0-integration` and a clean merge:

| Item | Result |
|---|---|
| Canonical union (`c2d24b3`, 11 files, +361/−17) | VERIFIED — merged, C1 shutdown fix confirmed still needed, isolation half confirmed N/A |
| PR #49 (Self-QA, Git credential, bound branch) | VERIFIED — merged; a dropped main-side file restored |
| PR #47 (Restart/Duplicate Run, deadlock fix) | VERIFIED — merged; 3 conflicts resolved by hand (1 import, 2 docs keeping both sides) |
| One canonical branch | VERIFIED — `nexus/2.0-integration` |

The union's schema-guard repair is real: `cloud_store.REQUIRED_COLUMNS["nexus_runs"]`
now names the profiler columns migration `0040` adds, and
`test_connection_concurrency.py` checks the specific in-flight backend rather
than a whole-database count.

---

## 3. The Rust migration (Phases 3-4)

**29 Rust tests** (`cargo test`) and **9 parity tests** pass. The parity is
exhaustive where it can be:

| Component | Parity |
|---|---|
| Run state machine | every `(from, to)` pair (7×7) |
| Workflow state machine | every pair (10×9) |
| Control-plane task machine | every pair (10×10) |
| Plan validation + DAG | 4 valid + 11 invalid plans, reason compared |
| Plan version hash | byte for byte |
| Retry arithmetic | 24 cases |
| The claim decision | real runs in a live PostgreSQL lab store |

Benchmark, real, this machine, 20 000 cases, best of 5
(`benchmarks/rust_vs_python.json`): Python 4.88 s vs Rust 1.27 s including one
process start (~3.8×) — **on the migrated decisions only**. No product-level
speedup is claimed.

**Honest limitation:** the SQL claim transaction, the intent router and the
task-kind derivation are **not** migrated. They are named in
`docs/RUST_MIGRATION.md` §4 with the reason. The Python core is authoritative;
Rust is a verified candidate. See `docs/RUST_PYTHON_COMPATIBILITY.md`.

---

## 4. Live acceptance — OpenHands (the strongest evidence)

### 4.1 The diagnostic probe, live

`App.openhands_diagnostics("machine:default", live=True)` against the real
OpenHands Cloud with a stored account (`transport=direct`,
`default_model=openhands/deepseek-v4.1-flash`):

```
[VERIFIED  ] database: postgres round-trip ok
[VERIFIED  ] schema: all required tables and columns present
[VERIFIED  ] accounts: 1 account(s) probed
[VERIFIED  ] account_pool: mode auto selects live-acceptance
[VERIFIED  ] cloud_api: reachable
[VERIFIED  ] queues_leases: claim → release → re-claim round trip ok
[VERIFIED  ] recovery: expired lease reclaimed and re-claimed with a higher fencing token;
             a bound conversation was not re-claimed
[UNVERIFIED] lab_worker: no live worker, but no active run needs one
[VERIFIED  ] providers: configured: openhands (order: gemini → groq → openrouter → openhands)
[VERIFIED  ] provider_probe: 1 provider(s) probed: openhands ok
[VERIFIED  ] openhands_tools: 19 tool(s) reported; terminal, workspace, git and testing available
[VERIFIED  ] mcp_configuration: 21 MCP server(s) configured (configuration only, not exercised)
[VERIFIED  ] nexus_tools: NEXUS exposes 19 MCP tool(s) and 7 client tool(s) to agents
[VERIFIED  ] conversation_create: conversation created
[VERIFIED  ] message_and_events: message delivered; events read; conversation finished
[VERIFIED  ] run_command: the agent echoed the marker
[VERIFIED  ] task_completion: conversation reached the terminal state 'finished'
[VERIFIED  ] workspace_rw: file uploaded and read back with a matching SHA-256
```

`lab_worker` is UNVERIFIED **correctly**: no worker is live and no active run
needs one. It is not a pass and not a failure — it is "not run".

The documented trap was reproduced and then worked around as the repository
predicts: **without** `OPENHANDS_DEFAULT_MODEL` the same probe ends `error`
(LLM auth), because a bare sandbox defaults to a model whose key is absent. With
`openhands/deepseek-v4.1-flash` it is green. This is the behaviour the notes in
`AGENTS.md` describe; the report confirms it live.

### 4.2 A real autonomous run through NEXUS

A project and a goal were created in plain language, a run was started with a
plan, and a real `LabWorker` advanced it against live OpenHands:

```
00:57:36 run=running tasks=['running']
00:58:01 run=blocked tasks=['completed_unverified']
FINAL: blocked
 task T1 completed_unverified attempt 1
 verification: {'reason': 'no repository bound; cannot verify remote state',
                'verdict': 'completed_unverified'}
```

What this proves, and what it does not:

- **Proven:** NEXUS dispatched the task to a real OpenHands conversation, the
  agent worked in a real sandbox, the conversation settled, and NEXUS recorded
  the verdict honestly and raised notifications (`goal_started`, `task_started`,
  `task_completed_unverified`, `run_blocked`).
- **Proven:** the run is `blocked`, not `completed`, because no repository was
  bound and NEXUS could not verify a remote state. A textual agent result was
  **not** accepted as proof of work — exactly the rule the requirement states.
- **Not proven here:** a run that ends `completed_verified` on the live service.
  That needs a repository bound to a GitHub account with a token the sandbox can
  push with; the live OpenHands account has **no GitHub provider token**, so it
  cannot clone or push. This is the documented external blocker (section 7). The
  `completed_verified` path is proven against the local fake GitHub and real
  PostgreSQL (`tests/test_pipeline_e2e.py`, `tests/test_comprehensive_suite.py`).

---

## 5. Acceptance scenarios (A-G)

| Scenario | Status | Evidence |
|---|---|---|
| A — Normal development | VERIFIED (local fake GitHub + real DB) | `test_pipeline_e2e.py`; live dispatch proven in §4.2 |
| B — Complex development (multi-part DAG, parallel, combine) | VERIFIED | `test_durable_runs.py`, `test_planner_worker.py`, `test_lab_store.py` |
| C — Failure recovery (worker killed, restart, resume) | VERIFIED | `test_start_handover.py`, `test_lab_worker_process.py`, `test_acceptance_recovery.py` |
| D — Quality failure (failing tests → repair → re-verify) | VERIFIED | `test_acceptance_dev_task.py` (a real failure, fix and commit) |
| E — Security (cross-project, archive, branch, secret, unapproved push) | VERIFIED | `test_project_isolation.py`, `test_archive_safety.py`, `test_git_credential.py` |
| F — Mobile (install, authenticate, submit, follow, review) | SKIPPED — no emulator | build script verified by inspection; `test_browser_e2e.py` covers phone widths |
| G — Multiple agents, isolated projects | VERIFIED | `test_comprehensive_suite.py`, `test_project_isolation.py` |

Scenario F is BLOCKED by the environment, not by the code. The report does not
call it complete.

---

## 6. The 9 skips, named

| Count | Reason |
|---|---|
| 4 | Android SDK build-tools not installed (`test_android_apk.py`) |
| 3 | mingw-w64 not installed (`test_windows_exe.py`) |
| 1 | `TEST_POOLER_DSN` not set (`test_schema_bootstrap.py` pooler case) |
| 1 | Section F live variant needs a live OpenHands key **and** a sandbox (`test_comprehensive_suite.py`) |

None is a defect. Each is an environmental skip, and none is counted as a pass.

---

## 7. External blockers (listed separately, never hidden)

| Blocker | What it prevents | What is proven instead |
|---|---|---|
| A second live OpenHands key | a *two-live-account* failover | the failover logic, end to end, against real PostgreSQL with honest transport fakes (`test_openhands_failover.py`) |
| No GitHub provider token on the live OpenHands account | a live run that reaches `completed_verified` | the verify path against the local fake GitHub and a real DB; the live run reaches an honest `completed_unverified` |
| No Android emulator | running the APK | the APK builds and passes `apksigner verify`/`aapt2 dump badging` |
| No Windows host | running the `.exe` | the PE headers and `.rsrc` are read and checked |
| Owner authorisation | merging, tagging, deploying | prepared and documented, not performed |

---

## 8. Definition of Done — each condition, honestly

| Condition | State |
|---|---|
| Canonical repository state is stable | **VERIFIED** |
| No critical integration conflicts remain | **VERIFIED** |
| The full development lifecycle works | **VERIFIED** (tests; live dispatch §4.2) |
| OpenHands execution is verified live | **VERIFIED** (16/16 runnable checks) |
| Git operations respect project and branch bindings | **VERIFIED** (`test_git_credential.py`, `test_lab_http.py`) |
| Independent Self-QA works | **VERIFIED** (`test_selfqa.py`; real exit codes, "skipped" is not a pass) |
| Project isolation is proven | **VERIFIED** (`test_project_isolation.py`) |
| Restart and recovery work | **VERIFIED** (durable PostgreSQL; worker-process tests) |
| Concurrency behavior is tested | **VERIFIED** (leases, fencing, claim, deadlock stress) |
| Security gates pass | **VERIFIED** (`docs/SECURITY_MODEL.md`, the cited tests) |
| Rust owns the migrated core components | **PARTIAL** — pure core migrated with parity; the SQL claim path is the next gated step |
| Python/Rust parity is demonstrated | **VERIFIED** (exhaustive where possible) |
| The CLI works | **VERIFIED** (Python CLI; `nexus-rs` for the pure part) |
| Web and mobile experiences are usable | Web **VERIFIED**; mobile **build-only** (no emulator) |
| Required distribution artifacts are built and tested | Debian **VERIFIED**; Android/Windows **build-only** |
| Documentation matches the actual product | **VERIFIED** (this set, written against the code) |
| Release acceptance evidence is available | **VERIFIED** (this report) |

The two conditions that are not fully met — Rust ownership of the whole core,
and runtime-verified mobile/Windows artifacts — are stated as partial or
build-only, not as done.

---

## 9. What was not done, and why

- **Merging to `main`, tagging, deploying.** Forbidden without the owner's
  explicit authorisation. The work is on `nexus/2.0-integration`; the release
  procedure is `docs/RELEASE_GUIDE.md`.
- **The Rust SQL claim path.** A larger, riskier change than the pure core;
  gated the same way, and scheduled next (`docs/RUST_MIGRATION.md` §8).
- **Running the APK or the `.exe`.** No emulator, no Windows host.
- **A live two-account failover.** One key.

Each is a real limitation, named. None is hidden behind a "complete".

---

## 10. Reproduce this report

```bash
# 1. The Python suite
TEST_SUPABASE_DB_URL=postgresql://user:pw@127.0.0.1:5432/db \
  python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_browser_e2e.py

# 2. The browser suite
E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
  python -m pytest tests/test_browser_e2e.py -q

# 3. The Rust core and the parity gate
cd rust && cargo test && cargo build --release && cd ..
TEST_SUPABASE_DB_URL=... python -m pytest tests/test_rust_parity.py -q

# 4. Isolation
TEST_SUPABASE_DB_URL=... python -m pytest tests/test_project_isolation.py -q

# 5. The benchmark
python tools/benchmark_rust.py --cases 20000 --repeat 5
```

The live diagnostics need a stored OpenHands account (with
`NEXUS_SECRET_KEY` set and `OPENHANDS_DEFAULT_MODEL` naming a model the sandbox
can reach); `App.openhands_diagnostics(principal, live=True)` runs them.
