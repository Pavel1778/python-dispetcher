# NEXUS 3.0 — Phase 0: Preservation and Forensics

Status: **in progress**. Every fact below was re-verified in this session against
the repository and a live PostgreSQL 17. Nothing is taken from an earlier report
without a fresh check; where an old report disagrees, the fresh measurement wins
and the disagreement is noted.

Working branch: `nexus/3.0-foundation` (isolated; `main` is not touched).

## 1. Repository facts (verified)

| Fact | Value | How checked |
|---|---|---|
| `main` HEAD | `2edf3aa` (merge of PR #53, `arena/e1a9f455`) | `git log --oneline` |
| Branches on `origin` | `main`, `arena/3a132869`, `arena/71f72168`, `arena/aa1abcbc`, `arena/e1a9f455`, `feat/run-restart-duplicate`, `release/acceptance`, `release/nexus-2.0-pr-ready` | `git branch -r` |
| Tags | `v2.0.0`, four `preservation/*` | `git tag` |
| Working copy at start | clean | `git status --short` |
| Python modules | 68 files, **42,797 lines** under `orchestration/` | `wc -l` |
| Test modules | **136** files under `tests/` | `ls tests/*.py \| wc -l` |
| Migrations | `0001`–`0042`, **50 tables** (gaps `0012`/`0013` reserved, by design) | `ls migrations/`, `grep CREATE TABLE` |
| Studio (front end) | 5,549 lines: `app.js` 3,834, `app.css` 821, `index.html` 209, + markdown/highlight/login | `wc -l orchestration/studio/*` |
| Rust workspace | 3 crates, **1,763 lines**: `nexus-domain`, `nexus-core`, `nexus-cli` | `wc -l rust/crates/*` |

## 2. Toolchain (verified in this environment)

| Tool | Version | Note |
|---|---|---|
| Python | 3.13.15 | matches the suite's target |
| PostgreSQL | 17.11 | running on `127.0.0.1:5432`, `nexus` superuser |
| Node | 24.22.1 | for `tests/studio_render_test.js` |
| rustc / cargo | 1.100.0 | installed this session; `crates.io` reachable (verified with a scratch crate) |
| ruff | 0.13.1 | matches the CI pin |
| pytest | 9.0.3 | matches `requirements-dev.txt` |
| Playwright + Chromium | present (`/usr/bin/chromium`, bundled `chromium-1255`) | browser E2E will run, not skip |

**No Rust toolchain was present at session start**; it was installed. `cargo` can
fetch external crates, so a Tokio/Axum/SQLx workspace is buildable here (a
prerequisite for the Rust-first target that must be proven, not assumed).

## 3. What the current backend actually is

- **HTTP server:** Python stdlib `ThreadingHTTPServer` + `BaseHTTPRequestHandler`
  (`orchestration/web_server.py`, 5,243 lines). One thread per request; a
  background lab loop runs on its own thread unless `NEXUS_LAB_INLINE` says no.
- **Store:** `PostgresStore` (`cloud_store.py`, 2,526 lines) over `psycopg_pool`.
  One pool, prepared statements disabled (pooler safety), `StoreUnavailable` on
  outage with a 2 s back-off. `REQUIRED_COLUMNS` is the schema contract.
- **Lab store:** `LabStore` (`nexus_lab_store.py`, 3,052 lines). Atomic claim via
  `FOR UPDATE SKIP LOCKED` + advisory locks; `_TASK_DUE`, `_DEPS_VERIFIED`,
  `_BRANCH_FREE` are SQL predicates; `lease_token` fencing on every mutation.
- **Scheduler:** `LabScheduler` (`nexus_lab.py`, 2,525 lines): reclaim → claim →
  dispatch → poll → verify → record → decide; evidence and acceptance hooks.
- **OpenHands:** two transports (`cloud_client.py` app, `cloud_direct.py`
  direct), account pool (`openhands_pool.py`), router/failover
  (`openhands_router.py`), diagnostics (`openhands_diagnostics.py`).
- **MCP:** `mcp_server.py` — stdio and Streamable HTTP (`POST /mcp`), scoped tools.
- **CLI:** `cli.py` (1,204 lines), thin HTTP client.

## 4. API surface (the compatibility contract to preserve)

Routes seen in `web_server.py` (prefix `/api`): `capabilities`, `commands`,
`config`, `dashboard`, `github/{accounts,confirm,prepare,repositories,status}`,
`health`, `migrations`, `observability`, `openhands/{accounts,diagnostics,
inventory,pool,tools}`, `providers`, `repositories{,/sync,/remove}`,
`security/policy`, `session(s)`, `studio`, `system`, `tasks`, `v1/projects/*`
(the project-scoped tree: conversations, messages, runs, tasks, evidence,
files, github, executors, specs), `workflows`. `docs/API.md` is the written
contract. `tests/test_route_auth.py` fails if a protected route lacks auth.

## 5. Front-end identity (must not change)

The studio is a chat-first application: history left, conversation + composer
centre, workspace right (Файлы / Агент), settings and repository connection in
dialogs. CSP is strict (`script-src 'self'`, no inline script/style/`on*=`).
Assets are content-hashed (`ASSET_VERSION`). `tests/test_studio_ui.py` asserts
structure, packaging globs and contrast; `tests/studio_render_test.js` holds the
hostile-text corpus; `tests/test_browser_e2e.py` drives real Chromium journeys.

A **screenshot baseline** of the main screens is a Phase 0 deliverable and is
captured before any change (see `PHASE0-BASELINE.md`).

## 6. Packaging and CI (current)

- `packaging/debian/build-deb.sh` → real `.deb` via `dpkg-deb`; verified by
  `tests/test_deb_package.py`.
- `packaging/android/build-apk.sh` → signed APK via SDK tools, no Gradle;
  `tests/test_android_apk.py` (skips without the SDK).
- `packaging/windows/build-exe.sh` → PE32+ GUI `.exe` via mingw-w64;
  `tests/test_windows_exe.py` (skips without mingw-w64).
- CI: a single `tests.yml` with a `rust` job (builds the workspace, runs the
  parity suite) and a `test` job on `ubuntu-26.04`. **No per-platform package
  build job exists** — a Phase 5 gap.

These are **WebView/launcher shells**, not yet Tauri 2 (the NEXUS 3.0 target).
The distinction matters: a shell that loads the dashboard is not the same as a
Tauri application with a native bridge, and only the latter satisfies the
acceptance table.

## 7. Performance baseline

`tools/benchmark.py` measures real paths against a real server + PostgreSQL +
the same stand-in servers the suite uses (JSON out; compare with
`tools/benchmark_compare.py`). A stored sample exists at
`docs/performance/baseline_db.json` (DB throughput ~740 ops/s at 1 connection,
p50 ~1.3 ms). Phase 0 re-runs it on this machine to get a *local* baseline before
any migration, so the "before/after" comparison is on identical hardware.

## 8. Known constraints carried forward

- The live OpenHands account has **no GitHub provider token**, so a live task
  that must push ends `completed_unverified` — correct, not a defect.
- Render's free plan stops the process after ~15 min idle; the target is a
  dedicated server (systemd), which removes that.
- Several tests are environmental skips (Android SDK, mingw-w64, pooler DSN,
  live section F) — they must be counted as skips, never as passes.

## 9. Phase 0 exit criteria

- [x] `main` commit, branches, tags recorded.
- [x] Toolchain verified (incl. Rust installed and network-checked).
- [x] Backend, schema, studio, packaging, CI audited with line/table counts.
- [x] Full test suite run recorded: **2094 passed, 9 skipped, 0 failed** (530 s).
- [x] Rust workspace builds and its tests pass (29 + 5 + 4 = 38 Rust-side cases).
- [x] Local performance baseline captured (`PHASE0-BASELINE.md`).
- [x] Screenshot baseline of the studio captured (`baseline/*.png`).
- [x] Backup + recovery verification plan written (`PHASE0-RECOVERY-PLAN.md`).

**Baseline result:** green. The suite that must keep passing as NEXUS 3.0 is
built is 2094 tests; the reference screenshots and the JSON benchmark are the
before-pictures for "appearance preserved" and "performance measured".
