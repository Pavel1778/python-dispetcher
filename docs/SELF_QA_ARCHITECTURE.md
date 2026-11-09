# NEXUS Self-QA — Architecture (design)

Status: **design only.** Nothing in this document is implemented yet. It
specifies the "NEXUS Self-QA / Autonomous Quality Laboratory" (brief §5). No
command listed here exists today; the CLI reference marks each one as
implemented or planned.

---

## 1. Goal

Let OpenHands verify the whole project with no human in the loop, and produce
evidence that an independent reviewer can re-check. The engine must be able to
say "I could not verify this", never a false "pass".

## 2. Components

```
nexus doctor   ──► DiagnosticsEngine  ──► [ComponentCheck, ...] ──► report
nexus qa run   ──► QaRunner ──► TestCategory[] ──► EvidenceStore
nexus qa report──► EvidenceStore.read(run_id)
nexus qa repair──► RepairLoop ──► (QaRunner, AgentSession) ──► Diff ──► Reviewer
```

All four are new modules under `orchestration/qa/`. They call existing services
(`cloud_store`, `web_server` handlers, the real test suite) — they do not
re-implement business logic.

## 3. Diagnostics engine (`nexus doctor`)

Each check returns a record:

```
ComponentCheck {
  id:          str      # "postgres.schema", "cloud.api", ...
  status:      "healthy" | "degraded" | "failed" | "not_configured" | "not_applicable"
  severity:    "info" | "warning" | "critical"
  message:     str
  root_cause:  str | None
  action:      str | None      # recommended operator action
  evidence:    str | None      # reference into EvidenceStore
}
```

Checks (from brief §5.1), each mapped to a real probe:

| id | Probe (real) |
|---|---|
| `runtime.python` | `sys.version_info` vs required ≥3.10 |
| `runtime.deps` | `import psycopg`; version |
| `config.env` | presence of required env vars (values never printed) |
| `postgres.connect` | open a connection, `SELECT 1` |
| `postgres.schema` | the store's `REQUIRED_COLUMNS` guard |
| `postgres.migrations` | `schema_migrations` vs `migrations/` on disk |
| `http.api` | `GET /healthz` (must stay one-line JSON) |
| `http.auth` | session route reachable; CSRF present |
| `cloud.api` | `GET /api/v1/settings` (read-only) |
| `cloud.transport` | which transport `build_cloud_client` selected |
| `cloud.sandbox` | sandbox list reachable (read-only) |
| `github.connector` | `GitHubClient.configured`; `GET /rate_limit` |
| `files.subsystem` | `nexus_files` table present; quota config |
| `frontend.assets` | static files served; PWA manifest + service worker |
| `resources.disk` | free space on the data volume |

Secrets rule: a check may report *presence* and *validity*, never a value.
`credentials` never appear in `message`, `evidence`, or logs.

## 4. QA runner (`nexus qa run`)

Categories (brief §5.2). Each category is a named, reproducible command with a
declared isolation mode. **Real integrations must not be mocked**; a category
that cannot run (no DB, no Cloud key) reports `skipped`, not `pass`.

| # | Category | Real command |
|---|---|---|
| 1 | Static analysis | `ruff check .` |
| 2 | Lint | (same as 1; kept separate for reporting) |
| 3 | Type checking | none today — reported `not_applicable` |
| 4 | Unit tests | `pytest tests/ -m unit` |
| 5 | Integration tests | `pytest tests/ -m integration` (real PostgreSQL) |
| 6 | API contract tests | `pytest tests/test_web_server.py` |
| 7 | Database tests | `pytest tests/test_transactions.py` |
| 8 | Migration tests | `pytest tests/test_schema_bootstrap.py` |
| 9 | Security tests | `pytest tests/test_auth.py` + negative cases |
| 10 | Sandbox tests | Cloud sandbox probe (read-only) |
| 11 | CLI tests | `pytest tests/test_cli*` (Phase 3) |
| 12 | MCP tests | `pytest tests/test_mcp*` (Phase 5) |
| 13 | Frontend tests | `node --check orchestration/pwa/*.js` |
| 14 | Browser E2E | Playwright (below) |
| 15 | PWA tests | `pytest tests/test_pwa.py` + `node tests/pwa_worker_test.js` |
| 16 | Regression tests | full `pytest tests/` |
| 17 | Backup/Restore | `nexus db restore-check` on a disposable DB |
| 18 | OpenHands integration | `tests/e2e_cloud_sr_jr.py` (needs live key) |

## 5. Browser E2E (brief §5.3)

Playwright drives the real server on a disposable database:

- login / logout / session
- create project, switch project
- upload a file, preview, download, delete
- create a task, watch status, open report
- integrations tab, settings
- responsive widths (desktop / tablet / mobile)

Failures on: any console error, any network error, any unexpected HTTP status.

## 6. Evidence engine (brief §5.4)

An `EvidenceRecord` is written per run and per category:

```
EvidenceRecord {
  run_id, commit_sha, branch, started_at, finished_at,
  env: {python, psycopg, os},          # no secrets
  config: {categories, isolation},
  commands: [{argv, exit_code, duration_s}],
  results:  [{category, status, counts}],
  artifacts: [paths],                   # logs, reports
  sandbox: {transport, sandbox_id?},
  errors: [...],
  integrity: sha256(canonical_json_without_integrity)
}
```

Integrity: the record is hashed and the hash stored beside it. A result with no
record, a tampered record, or a record whose `commit_sha` does not match the
checked-out HEAD is **not** evidence. `nexus qa report` re-verifies the hash.

## 7. Repair loop (`nexus qa repair`, brief §5.5)

```
run QA ─► collect failures ─► group ─► root cause ─► plan
   ─► assign to an agent (isolated branch) ─► targeted tests
   ─► full regression ─► diff ─► report ─► independent reviewer
```

Hard limits (enforced, not advisory): max cycles, max wall-clock, max tokens.
Forbidden, and asserted in tests: deleting tests, weakening assertions,
disabling security checks, masking errors, auto-merge to `main`, auto-deploy.
If the agent is unsure, it reports the problem instead of faking success.

Rollback: each cycle works on its own branch; on failure the agent resets only
its own branch (`git reset --hard <cycle-base>`) and tries an alternative.

## 8. Independent reviewer (brief §5.6)

A separate session that: reads the diff, runs the tests, reproduces the original
failure, checks side effects and security, verifies the evidence hash, and
returns `PASS | PASS_WITH_CONDITIONS | FAIL | BLOCKED`. The author never
approves its own work.

## 9. Non-goals

- No mocks presented as integration proof.
- No claim of success without a matching, integrity-checked evidence record.
- No infinite repair; the loop is bounded and stops with a report.
