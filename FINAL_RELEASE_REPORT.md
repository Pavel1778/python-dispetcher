# NEXUS — Final Release Report

Final Delivery Sprint. This is an implementation report: what was finished, what
was verified, and what is blocked by missing credentials.

- **Repository:** `Pavel1778/python-dispetcher`
- **Base (canonical `main`):** `42824896e388c97e218a8915cab56a8498a28473`
- **This branch:** `feat/run-restart-duplicate` @ `b30aba928553354f4842ce075c747c5df53d33bb`
- **Environment:** Python 3.13.15, PostgreSQL 17.11, Chromium 159.0.8154.6,
  pytest 9.0.3, ruff 0.13.1

## Verdict

**RELEASE CANDIDATE READY WITH EXTERNAL BLOCKERS.**

The product is usable: the full workflow runs end to end (verified against a
live OpenHands account and against the local stand-in servers). The two
remaining blockers are external credentials this sandbox does not hold — a
second OpenHands key (two-account failover) and a GitHub provider token on the
live account (a live push). Neither is a code defect.

## 1. What was done this sprint

The directive says: stop consolidating, finish the product. A gap analysis of
the **actual** `main` showed almost everything the directive names is already
implemented and verified. Three things were genuinely missing or broken:

### Restart Run — implemented
`LabStore.restart_run`: re-runs a finished run's whole plan as a **new run
under the same goal**; every task re-created `pending` at attempt 0; the old run
kept as history. Refused while the goal has an active run, and while the run is
not over. Surface: `POST …/runs/{id}/restart`, `nexus run restart`, MCP
`restart_run`, studio «Перезапустить».

### Duplicate Run — implemented
`LabStore.duplicate_run`: copies a finished run's plan into a **new goal and
run** (a copy cannot share the goal — one active run per goal). Idempotent with
a key. Surface: `POST …/runs/{id}/duplicate`, `nexus run duplicate`, MCP
`duplicate_run`, studio «Дублировать».

### Cancel/reclaim deadlock — fixed (P1 reliability)
`reclaim_expired` read the run row **without** `FOR UPDATE` and then locked task
rows; the cancel path locks the run row first and then tasks. The inverted order
deadlocked under load (`psycopg.errors.DeadlockDetected`). Fixed by locking the
run row first — the order every other site already uses.

## 2. Automated quality gates (on `b30aba9`, this environment)

| Gate | Command | Result |
|---|---|---|
| Full suite | `pytest tests/ -q` | **1980 passed, 9 skipped, 0 failed** (31:40) |
| Lint | `ruff check .` | **All checks passed** |
| Studio render | `node tests/studio_render_test.js` | **studio render tests passed** |
| Browser E2E | `pytest tests/test_browser_e2e.py` | **37 passed** (Chromium) |
| Deadlock stress | cancel test ×12 | **0/12 failed** (was 1/6 on clean `main`) |

Reference: clean `main` @ 4282489 (nothing else running) → **1973 passed, 9
skipped, 0 failed**. This branch adds 7 tests → 1980.

**Skips (environmental, none a defect):** 4 Android SDK build-tools, 3 mingw-w64
(Windows `.exe`), 1 `TEST_POOLER_DSN` pooler case, 1 section-F live variant.

## 3. Live OpenHands acceptance (real account, real key)

`OpenHandsDiagnostics` live probe (creates and deletes exactly one conversation):

| Check | Status |
|---|---|
| `conversation_create` | **VERIFIED** |
| `message_and_events` | **VERIFIED** — message delivered, events read, conversation `finished` |
| `run_command` | **VERIFIED** — the agent echoed the marker |
| `task_completion` | **VERIFIED** — reached terminal state `finished` |
| `workspace_rw` | **VERIFIED** — file uploaded and read back, SHA-256 matched |

`CloudClient.health()` → `True`; `ping()` → `200`.

**Not verified (external prerequisite):**
- **Two-account failover** — only one OpenHands key is available. The logic is
  covered against the local fakes + real PostgreSQL
  (`tests/test_openhands_failover.py`). Missing prerequisite: a second
  authorized OpenHands key.
- **Live GitHub push** — the live account has **no GitHub provider token**
  (`provider_tokens_set: {'github': False}`), so a task that must clone/push ends
  `completed_unverified`. Missing prerequisite: a GitHub provider token on the
  account. Read paths work (`GET /user`).

## 4. Security status

No new P0/P1 vulnerability. This sprint changed run-management and one lock
order; the audit surfaces remain as documented in `docs/PRODUCTION_READINESS.md`
(project isolation = 404 not 403, MCP scopes enforced, archive safety, SSRF
guard, secret redaction, RLS on the session-key tables). The two new endpoints
are project-scoped like every other run route (a non-member gets 404 — asserted
in the new member-only test), and the two new MCP tools carry
`SCOPE_RUN_CONTROL`.

## 5. Remaining limitations and external blockers

- MCP **client**, Self-QA engine, iOS client: design only (not product gaps).
- Two terminal-output implementations by design (run-level and task-level
  routes) — not to be collapsed.
- `direct` transport selects a repository by instruction, not a structured field.
- Render free tier sleeps without traffic (documented).

## 6. How to install and run

See `DEPLOYMENT_CHECKLIST.md`. Short form:

```bash
pip install -r requirements.txt -r requirements-dev.txt
export SUPABASE_DB_URL=… WEB_AUTH_TOKEN=… AUTH_USERNAME=… AUTH_PASSWORD_HASH=… \
       AUTH_SESSION_SECRET=… OPENHANDS_API_KEY=… OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash
python -m orchestration.web_server --host 0.0.0.0 --port "$PORT" --token "$WEB_AUTH_TOKEN"
```

Migrations apply automatically on first connect. `/healthz` and `/readyz` are
the health checks.

## 7. Can the owner use it immediately?

**Yes**, against their own OpenHands account and PostgreSQL. Set the variables
above, start the web server (the server advances runs itself by default), open
the studio, create a project, connect a repository, and submit a task. The two
external items (a second OpenHands key, a GitHub provider token) only affect
two-account failover and a live push, not the core workflow.

## 8. Acceptance scenario status

| Step | Status |
|---|---|
| 1. Start NEXUS | VERIFIED (`/healthz`, `/readyz`) |
| 2. Create a project | VERIFIED (`test_studio_api.py`, studio) |
| 3. Connect a repository | VERIFIED (`test_studio_api.py`; live read) |
| 4. Submit a natural-language task | VERIFIED (`test_pipeline_e2e.py`) |
| 5. Generate a plan | VERIFIED |
| 6. Dispatch to OpenHands | VERIFIED (live probe; `test_openhands_failover.py`) |
| 7. Observe execution | VERIFIED |
| 8. Retrieve the result | VERIFIED |
| 9. Run verification | VERIFIED (real `GitVerifier`; `test_pipeline_e2e.py`) |
| 10. Display evidence | VERIFIED (`test_evidence_trust.py`, studio) |
| 11. Produce a final report | VERIFIED (`test_run_reports.py`) |
| 12. In-app notification | VERIFIED (`test_lab_http.py`) |
| 13. Restart NEXUS, history intact | VERIFIED (durable PostgreSQL; `test_lab_worker_process.py`) |

Plus: project isolation (VERIFIED), failure recovery (VERIFIED), cancellation
(VERIFIED, live), mobile usability (VERIFIED, browser E2E at phone widths).
