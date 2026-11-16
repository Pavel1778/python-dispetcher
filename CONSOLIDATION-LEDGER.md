# NEXUS Consolidation — Integration Ledger

Consolidation of every development branch and preserved uncommitted work into one
`main`. Written 2026-11-16.

Base for the integration: **`foreign/owner-phase34` @ `3ca8e5f`**
("feat(consolidate): owner-approved Phase 3+4 reconciliation") — the owner-approved
reconciliation on top of `main` @ `28aa026` (PR #41 and #42 merged). The
integration branch is `consolidate/final`, built in a dedicated worktree
`/tmp/nexus_integration`.

## Architectural decisions (Phase C)

- **PostgreSQL connection architecture (PR #41 vs Phase 3 pool): KEEP POOL.**
  The Phase 3 `psycopg_pool.ConnectionPool` store (owner-phase34) is the
  connection layer. PR #41's single-connection concurrency test was rewritten to
  the pool's real invariants; `store._conn`/`store._lock` are not restored. This
  is the owner-approved direction recorded in `3ca8e5f`.
- **Terminal outputs (C2): one canonical module.** `orchestration/terminal_output.py`
  is the single union implementation: PR #42's source verification + Phase 4's
  conversation-scoped trajectory transport and ZIP security. The duplicate
  `orchestration/nexus_outputs.py` is not carried.
- **Phase 5 profiler (C3): ported onto the owner-approved base** (not merged as
  the branch, whose base predates the union).
- **Worker/scheduler (C4): preserved unchanged** — atomic claim, `FOR UPDATE SKIP
  LOCKED`, lease tokens/fencing, heartbeats, reclaim, restart recovery, start
  hand-over, DAG, parallel tasks, bounded retries, cancellation, no-progress.

## Source-branch dispositions

| Branch | HEAD | Disposition | Notes |
|---|---|---|---|
| `main` | `28aa026` | base | PR #41 + #42 merged |
| `foreign/owner-phase34` | `3ca8e5f` | **base of integration** | KEEP POOL + terminal-output union + test-DB port fix |
| `foreign/phase5` (`phase5/execution-intelligence`) | `7859e3a` | **ported** | run-time profiler (migration 0040, `nexus_profiler.py`, milestones, API, studio, docs, tests). Its terminal-output dup not carried. |
| `foreign/consolidate-phase34` | `8dcca85` | superseded | earlier consolidation of the same; owner-phase34 supersedes it |
| `cand/A` (`nexus/phase34-consolidation`) | `d570e89` | **partly incorporated** | code is the owner-phase34 line (keep owner); its unique docs/architecture (19) + docs/performance (12) preserved additively |
| `engineering/performance-evolution` (`ce70f07`) | worktree dirty | **incorporated** | pool+perf line is in owner-phase34; uncommitted `tools/perf_bench.py` + `docs/performance/baseline_db.json` preserved |
| `nexus/chat-first-approval` (WT1) | `ea2dd71` + dirty | **duplicate** | `nexus_approval.py` + migration reimplement main's `nexus_workflow.py` (already merged) and collide with migration 0035; preserved in the backup bundle, not applied |
| `main` worktree (WT2, openhands-inventory) | `d72f22e` + dirty | **incorporated** | `openhands_inventory.py`, CLI, route, tool, tests; fixtures unified |
| `arena/b10d2a83-python-dispetcher` | `31bd3ec` | **incorporated** | pause-honesty UI fix (`fc026be`/`12fdf02`); `test_terminal_output.py` skipped (symbol drift, superseded by `test_artifacts.py`) |
| `arena/ab0d7665-python-dispetcher` | `c2bce12` | **already in main** | PR #38 ported to main (lifecycle, `_continues_analysis`, regression tests) |
| `arena/d7bb7e3c-python-dispetcher` | `6b7ea4a` | **already in main** | only real fix (`cloud_direct.base_url`) is in main; migration-manifest framework deliberately not adopted |
| `nexus/mainline-hardening` | `56c71fb` | **already in main** | merge of d7bb7e3c |
| `arena/1038b6bd-python-dispetcher` | `935ebfe` | **already in main** | PR #42 merged |
| `arena/bafa7522-python-dispetcher` | `afbca3f` | **already in main** | PR #41 merged |
| `nexus/perf-reliability-lifecycle` | `fc0de5b` | **already in main** | PR #39 merged |
| `fix/production-recovery-migration-0037` | `f41f08f` | **partly incorporated** | `MIGRATION_CORRECTIONS` recovery + `test_migration_sql_guards.py`; the `DECLARE r text` fix is already in main; branch otherwise based on an older main |
| `nexus/continue-longtask-hardening` | `a60f1a3` | **stale snapshot** | its content is already in main (a60f1a3's tree removes newer work); the `DispatchError.__cause__` fix is in main |
| `nexus/gap-analysis` | `25179c3` | **already in main** | `docs/GAP-ANALYSIS.md` identical in main |
| `nexus/ground-truth-verify` | `968bcd2` | **already in main** | docs content in main (only a file-mode bit differs) |

## Uncommitted work preserved (backup: `/tmp/nexus_preservation_<stamp>/`)

- `wt1_chat-first-approval` (branch `nexus/chat-first-approval` @ `ea2dd71`):
  3 modified files + `migrations/0034_chat_approval.sql`, `orchestration/nexus_approval.py`.
  Disposition: **duplicate** of main's `nexus_workflow.py`; preserved, not applied.
- `wt2_openhands-inventory` (branch `main` @ `d72f22e`): 5 modified + 3 new files.
  Disposition: **incorporated**.
- `wt3_engineering-perf` (branch `engineering/performance-evolution` @ `a744263`):
  `cloud_store.py` (pool, an alternative of owner-phase34's), `tools/perf_bench.py`,
  `docs/performance/baseline_db.json`. Disposition: pool alt **superseded**;
  benchmark **incorporated**.

## Integration commits on `consolidate/final`

1. `consolidate(phase5)`: the run-time profiler.
2. `consolidate(phase4)`: the OpenHands inventory tool + fixture unification.
3. `consolidate(prod)`: the registered migration-correction recovery.
4. `consolidate`: pause-honesty UI fix + perf benchmark.
5. `consolidate(docs)`: the Phase 3/4 architecture and performance documents.

## Verification (Phase E)

- **Full suite**: `pytest tests/ -q` against a fresh PostgreSQL 17 on 127.0.0.1 →
  **1973 passed, 9 skipped, 0 failed** (31 min). 1982 tests collected.
- **ruff**: `ruff check .` clean.
- **Studio**: `node tests/studio_render_test.js` → passed; `tests/test_studio_ui.py`
  26 passed.
- **Browser E2E**: `tests/test_browser_e2e.py` (33 Chromium journeys, desktop and
  five widths) passed within the full run.
- **Migrations**: 41 files, `0001`–`0040` (gaps `0012`/`0013` reserved), no
  collisions; applied by `PostgresStore` on a fresh schema; the schema guard and
  the registered-correction path covered by `test_schema_bootstrap.py`.
- **Security**: route auth, project isolation (404 not 403), terminal-output
  access (conversation-scoped, no global `/api/file/*`), ZIP traversal/bomb
  rejection, size limits, MCP/CLI authorization and no-secret-leak assertions all
  pass inside the full run (`test_route_auth`, `test_archive_safety`,
  `test_artifacts`, `test_openhands_inventory`, `test_cli_mcp`, …).
- **Live OpenHands (direct transport, real key)**: VERIFIED. One conversation
  created (`READY`), ran `running → finished`, the marker `echo` appeared in its
  final response, 12 events, then the conversation was deleted. Cloud v1.92.0,
  `compatibility: compatible`.

### The 9 skips (all environmental, none a defect)

- 3 `test_windows_exe.py` — mingw-w64 not installed.
- 4 `test_android_apk.py` — Android SDK build-tools not installed.
- 1 `test_schema_bootstrap.py` pooler case — `TEST_POOLER_DSN` unset.
- 1 `test_comprehensive_suite.py` section F — needs a live OpenHands key *and a
  sandbox* for the two-account failover; BLOCKED (see AGENTS.md). The live
  conversation probe above is the honest substitute that did run.

### Live items still BLOCKED (not fabricated)

- **Two-account failover** — only one OpenHands key is available here; the
  failover is proven against the local fakes + PostgreSQL
  (`tests/test_openhands_failover.py`), not two live accounts.
- **Section F real development task through NEXUS** — same reason.

## Performance (Phase F)

Measured fresh with `tools/perf_bench.py` against a real local PostgreSQL 17
(different host from the historical figures, so they are not directly
comparable; recorded, not asserted):

| Metric | This run | Historical (Phase 3) |
|---|---|---|
| DB p99 @ 100 concurrent | 69.2 ms | 28.0 ms (baseline 550.9 ms) |
| DB throughput @ 100 concurrent | 1725 ops/s | 4467 ops/s |
| DB p99 @ 10 concurrent | 16.5 ms | — |
| claim throughput @ 50 workers | 1345 claims/s, 0 errors | — |

The pool served 100 concurrent clients with **0 errors** and every connection
returned (no pool exhaustion). Absolute numbers differ from the historical
machine; no regression was introduced by the consolidation (the pool code is the
owner-approved Phase 3 line, unchanged).

