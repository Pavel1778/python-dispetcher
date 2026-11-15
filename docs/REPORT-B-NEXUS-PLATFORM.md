# REPORT B — NEXUS Platform (2026-11-15)

## Git state

| Item | Value |
| --- | --- |
| `main` SHA | `28aa026b0d22b82a0befd9a36492aabc09aabc49` (merge of PR #42) |
| Working branch | `arena/b10d2a83-python-dispetcher` (branched from `28aa026`) |
| PRs this session | #42 (merged on explicit approval, 2026-11-15) |
| Changed vs `main` | `tests/test_terminal_output.py` (new, `fc026be`), `orchestration/studio/app.js` (pause honesty, `12fdf02`), `docs/PERFORMANCE.md`, this report + REPORT A |

## Test evidence (honest)

| Run | Result |
| --- | --- |
| **Full sequential suite `pytest tests/ -q`** (HEAD `9cc5c4b`; second run, after the studio edit) | **1915 passed, 49 skipped, 0 failed — 36m54s, `EXIT=0` (2026-11-15)** |
| `tests/test_terminal_output.py` (standalone) | 45 passed |
| `tests/test_studio_ui.py` | 25 passed |
| `ruff check` (changed files) | clean |
| `node --check orchestration/studio/app.js` | OK |

The studio-UI edit landed after the full-suite green run; the studio policy
suite (25/25 incl. `node --check` and asset accounting) is green on the
edited file. The full suite has NOT been re-run after `12fdf02` — treat
the 1915/49 as evidence for `fc026be`, not for the studio edit.

The 49 skips are environment-gated (Playwright/Chromium absent — browser
e2e; systemd tooling; Windows .exe on Linux; live-credential tests).
**No test failure was observed; nothing was made green by deleting or
weakening assertions.**

## Subsystem status matrix

Legend: VERIFIED = tested/reproduced here; PARTIAL = code read + fake-socket
coverage, not live; NOT VERIFIED = not checked here this session.

| Subsystem | Status | Evidence |
| --- | --- | --- |
| Projects & isolation | PARTIAL–VERIFIED | project-scoped 404 tests green; not re-audited line-by-line |
| PostgreSQL persistence & migrations | VERIFIED | both `0038_*.sql` applied on fresh `nexus_bench` (see below); runner is filename-keyed + checksummed |
| DAG scheduler (claim/lease/reclaim, cycle budget) | PARTIAL | durable-run tests green vs fake agents; live timing not measured |
| Run Management backend (pause/resume/cancel/hide/show/retry-failed/switch) | PARTIAL–VERIFIED | transactional state settlement tests green; live drill not run |
| Run Management UI (pause semantics) | FIXED this branch | tooltip/aria/toast/standing note; studio_ui 25/25 |
| Terminal-output recovery | see REPORT A | 45 tests; raw stdout BLOCKED by upstream design |
| Recovery / lost worker / stuck tasks | PARTIAL | lease+reclaim tested vs fakes; live restart drill not run |
| Test-DB isolation | VERIFIED | `tests/_test_db.py` per-process DBs / flock; suite green with it since before PR #42 merge |
| OpenHands client & events | PARTIAL–VERIFIED | suite green vs fakes; live not re-verified |
| GitHub/PR guarded actions | NOT VERIFIED here | suite green vs fake GitHub; no live action performed |
| CLI (`nexus`) | NOT VERIFIED here | present (`orchestration/cli.py`); not examined this session |
| MCP | NOT VERIFIED here | architecture docs only |
| File Laboratory / archives | NOT VERIFIED here | suite green; not re-audited |
| Reports & evidence | PARTIAL | report paths tested by suite; Self-QA design-only |
| Notifications | PARTIAL | store/API tested by suite; UX not audited |
| Frontend (Vanilla JS) | PARTIAL | studio static tests + node --check; **browser/mobile/Android NOT done** |
| Deployment & packaging | NOT VERIFIED here | files exist; not built |
| PWA | NOT VERIFIED here | manifest/service-worker present; not exercised |

## Migrations: the two `0038_*.sql` files

Both are additive (`ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`)
and were recorded as applied in `schema_migrations` on a **fresh** database
(`nexus_bench`, created 2026-11-15, seeded by `tools/benchmark.py` which applies
migrations): `0038_lifecycle.sql` and `0038_project_lifecycle.sql` are both
present, and `nexus_projects`/`nexus_conversations` exist. The duplicate prefix
is therefore inert with the current runner (filename is the key, applied in
sorted filename order, checksum-guarded). This does **not** verify order- or
content-independence in general — it verifies these two files, additively, on a
fresh database. No rename is recommended without a migration-runner audit.

## Performance (tables in PERFORMANCE.md)

2026-11-15 (Python 3.11.2, local PG 16.2, own `nexus_bench` DB): read paths
single-digit ms (list_projects 1.24, open_project 1.48, list_conversations
1.47, open_conversation 1.44, fetch_history 2.20, search_by_content 1.95,
queue_claim 2.15, studio_status 0.97); provider-bound paths chat_turn 18.53 /
start_run 33.40. **No regression vs 2026-11-14 is claimed** — different Python
(3.11 vs 3.13), so not a controlled A/B; the comparison instrument is
`tools/benchmark_compare.py`. **No NEXUS-side hotspot; no speedup claimed.**
The terminal-outputs list path is NOT yet benchmarked.

## Security notes

- Outbound calls go through the SSRF policy (loopback/private refused by
  default; tests opt in explicitly; cloud-metadata addresses never permitted).
- Sandbox-supplied paths are lexically validated; the file path is recomputed
  from event content; paths never leave the server; 8 MiB read cap.
- Audit line (`terminal_output.read`) on successful saved-output reads.
- No credentials in UI, exports, or reports.

## Known limits

1. The new studio UI copy and outputs panel have not been rendered in a real
   browser or an Android viewport (Playwright absent).
2. Self-QA / autonomous verification is **design only**
   (`docs/SELF_QA_ARCHITECTURE.md`); agent-result verification today is the
   GitVerifier chain, not a per-project build/lint harness.
3. The wall-clock cost of a run is dominated by the agent/LLM; no
   NEXUS-side acceleration is justified without a measured bottleneck.
4. OpenHands conclusions are anchored to 1.60.1; an image upgrade invalidates
   the terminal-output analysis until re-derived.

## Next priorities

1. Mobile/Android + real-browser pass of studio (outputs panel, pause copy).
2. ~~Re-run full suite after `12fdf02`~~ — done: second run green (36m54s).
3. Benchmark the terminal-outputs list path; add it to `tools/benchmark.py`.
4. CI: push/PR pipeline running the suite; block merge on red.
5. Self-QA first vertical slice: `nexus doctor` (status/severity/action).
6. Live OpenHands verification round (with credentials), then update REPORT A.
7. Android APK build/signing verification in CI (`packaging/android`).

## What is explicitly NOT claimed

- No raw terminal-output recovery (impossible on this agent-server version).
- No live-cloud verification this session.
- No performance win; no "production-ready" stamp beyond the evidence above.

## Static mobile/Android pass (2026-11-15, static only — NOT a render)

Playwright is installed but its Chromium build is absent and the browser
download call failed (`--only-shell`, sandbox network), so **no page was
rendered**. Static review only:

- `index.html` / `login.html`: `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">` ✓
- Outputs panel renders through the shared dialog; the output view is
  `.log-view` — `overflow: auto`, `max-height: min(62vh, 560px)`, `white-space:
  pre`, `-webkit-overflow-scrolling: touch`; at `max-width: 700px` it drops to
  `56vh`/11px and `.output-actions .btn` wraps full-width. No horizontal
  page overflow is expected from this rule set.
- Copy-fallback (`textarea.copy-area`) and download are native controls;
  no canvas/iframe tricks.
- 7 `@media` blocks cover hover/pointer/motion/1180/860/700/420 px.

Verdict: statically plausible at Android widths; **NOT verified in a real
browser or device.** Needs CI with Playwright or a phone to close.
