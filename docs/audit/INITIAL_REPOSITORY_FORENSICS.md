# Initial Repository Forensics

Date: 2026-11-18. Author: engineering pass ("master prompt", Stage A).
Every fact below was measured/queried on the machine that holds the working
tree, not copied from earlier reports.

## Git state (measured)

| Field | Value |
|---|---|
| Working tree root | `/home/user/python-dispetcher` |
| Branch | `arena/aa1abcbc-python-dispetcher` (status: `pr_open`) |
| HEAD | `eff7cd2ff30441916396e0437895e16c7dc45a31` |
| `origin/main` | `c0f49fcfb91e5ced07d372c2a8743503d3bfde1e` (matches known baseline, verified live) |
| Commits ahead of `origin/main` | 2 (`edae290`, `eff7cd2`) |
| Merge-base | `c0f49fc` == `origin/main` (fast-forward-able, linear on main) |
| Local branches | `arena/aa1abcbc-python-dispetcher` (current), `main` |
| Remote branches | `origin/arena/aa1abcbc-python-dispetcher`, `origin/main`, `origin/HEAD -> origin/main` |
| Tags | none |
| PR | **#51 OPEN**, `arena/aa1abcbc-python-dispetcher` → `main`, mergeable, CI green at `eff7cd2` (run `44422028130`, event `pull_request`, conclusion `success`) |
| Uncommitted changes at entry | none (`git status --porcelain` empty) |

PR #51 diff under review: 2 commits (`edae290`, `eff7cd2`); +281 / −14 lines over
`c0f49fc`; no runtime code beyond two additive indexes (`0041`) and test harness fix.

## Environment (measured)

- Python: 3.11.2 (system, used to run tooling) and 3.12 target in CI
  (`.github/workflows/tests.yml`, runner `ubuntu-26.04`, service `postgres:17`).
- Local PostgreSQL: **16.2** (pgserver cluster in `/home/user/pgdata-arena`),
  DBs: `nexus_arena` (suite), `nexus_perf` (seeded 60k-task EXPLAIN harness).
  **No local PG 17** and no local Chromium — those are CI-only.
- Node v22.22.3 / npm present (Studio render test is `node tests/studio_render_test.js`).
- Rust: `rustc`/`cargo` **not installed**; `apt-get install rustc cargo` refused
  (dpkg lock, not root); crates.io TLS EOF on 2026-11-18 re-check and earlier.
- Docker: **not installed** — compose files are validated as YAML only.
- CI: `.github/workflows/tests.yml` — lint (`ruff check .`), Chromium E2E, full
  `pytest` on PG 17; latest runs green (`44422028130` PR #51;
  `44351108583` main; `44151435593` former branch). Node E2E needs Chromium
  system libs managed inside CI.

## Codebase scale (measured)

- `orchestration/`: 41,527 LOC, 78 modules + `studio/` (frontend) and `pwa/`.
  Largest files: `web_server.py` **5,376** LOC, `nexus_lab_store.py` 2,812,
  `cloud_store.py` 2,464, `cli.py` 1,132.
- `tests/`: 129 files, 43,949 LOC.
- `migrations/`: **42** files (`0001_init.sql` … `0041_branch_claim_indexes.sql`;
  gaps 0012/0013 reserved).
- Root docs: many historical reports (kept, audit value; not all reflect today).

## Architecture map (from the tree)

Entry points: `orchestration/web_server.py` (`main()`, classes `App`, `Handler`,
`WebServer`), `orchestration/lab_worker.py` (standalone worker), Render deploy
(`render.yaml`, CMD `python -m orchestration.web_server --host 0.0.0.0 --port
$PORT --token $WEB_AUTH_TOKEN`), Docker (`python:3.12-slim`, copies only
`requirements`, `orchestration`, `migrations` — deliberately no tests/tools).

Subsystems and their module anchors:

- HTTP layer / routing / auth: `web_server.py` (`Handler.do_GET/do_POST/do_DELETE/do_PUT...`,
  `_LoginThrottle`, `_json_body_cap`, CSP/`_is_compressible`, session cookie handling),
  `web_auth.py` (`AuthService`, pbkdf2_sha256 hashes, session+CSRF), `multipart.py`,
  `net_guard.py`.
- Application services: `nexus_*.py` (lab, conversations, evidence, generation,
  planner, projects, reports, retry, selfqa, specs, workflow, policy …),
  `openhands_*.py`, `github_*.py`, `providers.py`, `cloud_*.py`.
- Persistence: `cloud_store.py` (`PostgresStore`, pooling, `_pool_check_connection`),
  `nexus_lab_store.py` (claim path `_BRANCH_FREE`/`claim_task`), `restore_point.py`,
  `migrations/` + idempotent `apply_migrations`-style bootstrap verified against
  fresh and pre-existing DBs.
- Worker: `lab_worker.py` (lease-based claims via `nexus_lab_store`), plus the
  app embeds a thread unless `NEXUS_LAB_INLINE=0`.
- Evidence/verification: `nexus_evidence.py`, `workflow_verifier.py`,
  `evidence_trust.py`, `evidence_keygen.py`.
- Files/extraction: `nexus_files.py`, `archive_safety.py`, `file_lab.py`, `multipart.py`.
- CLI / MCP / Self-QA: `cli.py`, `mcp_server.py` (stdio; Streamable HTTP at `POST /mcp`),
  `nexus_selfqa.py`.
- Frontend: `orchestration/studio/` (+ `pwa/`), rendered and checked by
  `tests/studio_render_test.js` and the browser E2E (CI only).
- Tracing: `tracing.py`; secrets: `secrets.py`; LLM routing: `openhands_router.py`.

## Identified risks (to be addressed or recorded)

1. **`web_server.py` size** (5.4k LOC; `App` ~1.3k, `Handler` ~3.4k). Strangler
   extraction is the plan; an empty-abstraction refactor would be worse than none.
2. **Claim-path DB cost** — fixed by `0041` (measured: seq scan 11.5 ms → 0.008 ms);
   see `docs/PERFORMANCE.md`.
3. **Local verification asymmetry** — PG 16.2 vs CI PG 17; Chromium E2E CI-only.
4. **One load-sensitive test flake** under 2-CPU saturation
   (`test_long_task_cases.py::test_a_restart_while_the_plan_is_being_built…`);
   6/6 passes isolated; documented in `docs/KNOWN_LIMITATIONS.md`.
5. **Single-instance statefulness** — the app runs a dispatch loop; scaling out
   is unsupported by design (see `docker-compose.yml`, `docs/SINGLE_VM.md`).
6. **Docs sprawl / historical drift** — many root-level "final report" markdown
   files; kept for audit value, reconciled where counts were stale (42 migrations).
7. **Rust** — no local toolchain/registry; measured candidates show I/O-bound
   workload; no Rust to be adopted without a measurable win.

## Initial test baseline

- Full suite at tree `eff7cd2` (fresh PG 16.2): **1974 passed · 49 skipped ·
  1 failed** (the load-sensitive flake above; passes in isolation; CI at
  `eff7cd2` is green incl. Chromium E2E and PG 17). Log: `/home/user/full_suite_arena3.log`.
- `ruff check .` clean. `node tests/studio_render_test.js` passes (run in earlier stage).
- Targeted migration/write-gate modules: green (`test_durable_migrations.py` 3/3;
  `test_lifecycle_migrations.py`, `test_schema_bootstrap.py`, `test_write_gate.py` pass).
- Live smoke (fresh empty DB, previous session): server self-applied 41→now 42
  migrations; `/healthz` 200; `/readyz` ready; anon API 401; login flow OK;
  cookie `HttpOnly; SameSite=Strict`; logout POST allowed with session but without
  CSRF by deliberate design (see Phase Five).

## Out of scope of this pass (declared up front)

- No production deploy, no production DB, no merges (PR #51 stays open for owner),
  no touching PR #47, no force-push, no deletion of branches/tags.
- PostgreSQL 17 and Chromium verification delegate to CI.
