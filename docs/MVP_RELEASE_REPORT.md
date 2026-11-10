# NEXUS MVP — Release Report

Status: **MVP READY — LIVE OPENHANDS VERIFICATION PENDING.**

This report states what is implemented, tested and verified, and what is not.
It describes the code in this branch (`nexus/mvp-integration`), not an
aspiration. The one thing that could not be exercised end to end in the build
sandbox is a *live* OpenHands execution: the sandbox has no working OpenHands
API key, so a task stops at the Cloud boundary and is reported honestly as
blocked. Everything on the NEXUS side of that boundary is verified.

---

## 1. What changed in this branch

The brief named eleven `nexus/*` branches to integrate. **They do not exist in
this repository.** The repository's own Phase 0 audit
(`docs/MASTER_COMPLETION_AUDIT.md`) already recorded that discrepancy. The
branches that *do* exist and were relevant were integrated:

| Source branch | What it is | Integrated |
|---|---|---|
| `fix/ci-python-version` | CI python pin fix (`3.12.6` → `3.12`) | cherry-pick `de16b34` |
| `docs/branch-topology-env-table` | branch topology + env docs | cherry-pick `dff2966` |
| `nexus/phase0-recon` | Phase 0 audit, roadmap, decisions, designs | cherry-pick `3b7dde3`, `ba04467` |
| `arena/*` (5 branches) | already merged into `main` (PRs #13–#18) | ancestor of `main` |
| `nexus/laboratory-files-github` | File Lab (D-1) | **not integrated** — its `0011` collides with `main`'s; D-1 is deferred by the brief |

The File Lab branch is left untouched as a deferred workstream; its
`0011_file_lab.sql` cannot coexist with `main`'s `0011_nexus_files.sql`.

---

## 2. Implemented

### 2.1 D-2 project execution binding (`migrations/0014_nexus_project_binding.sql`)

- `nexus_project_repositories` — explicit many-to-many project↔repository link.
  One repository can be shared by several projects.
- `tasks.project_id`, `workflows.project_id` — nullable, **no backfill**. Legacy
  rows stay unscoped.
- Canonical repository identity: `tasks.repository_id` is canonical; a trigger
  keeps it and `tasks.selected_repository` in agreement and **refuses a
  conflict** rather than picking one. It also refuses a cross-project reference
  (a project task may only name a repository linked to its project) and a
  workflow that changed project under its tasks.
- `ON DELETE SET NULL` on project references: deleting a project keeps execution
  history.

### 2.2 Dispatch authorization (`orchestration/nexus_binding.py`)

`authorize_dispatch` is the single gate, re-checked immediately before a
conversation is created (dispatcher) and before a task is queued (web / CLI /
MCP). It checks: project exists; task belongs to it; workflow shares it;
repository is linked, enabled, and on an allowed branch; the caller is a member.
The global allowlist is necessary but never sufficient for a project-scoped task.

### 2.3 Evidence Trust Engine (`orchestration/evidence_trust.py`, `nexus_evidence.py`)

- Ed25519 signatures over a canonical byte encoding; trusted keyring by `key_id`;
  revoked keys never verify.
- Verdicts: `VERIFIED` (trusted, valid signature), `UNVERIFIED` (unsigned, legacy,
  unknown/revoked key, or expected-commit mismatch), `FAILED` (tamper), `BLOCKED`
  (no crypto library). **HMAC can never produce `VERIFIED`.**
- Project-scoped records sign `project_id` (format v2); editing the column
  afterwards fails verification. Legacy records (format v1) stay unscoped.
- Hash chain (`prev_hash`/`entry_hash`) for reordering detection.
- Storage: `nexus_evidence` (migration `0014`), project-scoped reads, and
  re-verification that uses the real verifier.

### 2.4 CLI (`orchestration/cli.py`)

Thin bearer client over the HTTP API. Stable exit codes (0–8), `--json`
everywhere, no secret output. Commands: `version`, `doctor`, `status`,
`project list/create/show`, `project repo add/remove`, `task
list/create/show/dispatch`, `workflow list/show`, `evidence list/verify`,
`agent status`, `qa report/verify`. Entry point `nexus` in `pyproject.toml`.

### 2.5 MCP server (`orchestration/mcp_server.py`)

JSON-RPC 2.0 over stdio. Ten tools, no shell/SQL/eval. Every write re-checks
project membership; reads are project-scoped. Streamable HTTP is **not**
implemented; the server does not pretend to support it.

### 2.6 Web UI

The existing dashboard gained a **project execution context** panel: linked
repositories (with unlink), a task form that shows the project, repository and
branch it will use, the project's workflows and tasks, and signed evidence with
a real re-verify action that explains the verdict. Dashboard/system screens show
real component status (database, OpenHands, GitHub, auth, evidence).

### 2.7 Readiness

`GET /readyz` — liveness plus a real database round-trip (503 when the DB is
unreachable). `GET /healthz` stays a one-line probe.

### 2.8 Repository selection is explicit on the direct transport

The `direct` transport starts a sandbox conversation in an empty
`LocalWorkspace` and has no native `selected_repository` field, so a repository
task used to run against the wrong (empty) checkout with no error.
`OPENHANDS_DIRECT_REPO_MODE` now controls it: `prompt` (default) prepends a
deterministic clone instruction, `refuse` raises so the task dead-letters with a
clear reason, `allow` keeps the old behavior. `supports_repository_selection` is
exposed on the client, `/api/config` and the System screen.

### 2.9 Database-enforced workspace lock (migration `0015`)

`claim_task`'s workspace check read other rows, so two dispatchers could claim
two different queued tasks for one repository+branch at the same instant. A
partial unique index (`uq_tasks_workspace_active`) now enforces exclusivity in
the database; `claim_task` claims inside a savepoint and treats a
`UniqueViolation` as "workspace taken". Repository-less tasks never contend.

### 2.10 MCP Streamable HTTP

`POST /mcp` serves the same JSON-RPC tool surface over HTTP, mounted on the web
server with its session/CSRF or bearer auth. A notification returns 202, a parse
error 400.

### 2.11 Secret-table RLS (migration `0016`)

`conversation_routes` and `cloud_sandboxes` store a live sandbox
`session_api_key`; they now have RLS enabled with no client policy and their
default grants revoked, so a Supabase `anon`/`authenticated` role cannot read a
key through PostgREST.

### 2.12 Task and workflow detail views

Each task and workflow row in the project panel loads the real record: task
events, attempt/owner, conversation id and result; workflow Sr state with a link
to the Cloud transcript, the failure reason and its child tasks.

---

## 3. Verification

All commands run in this sandbox on PostgreSQL 17.

| Check | Command | Result |
|---|---|---|
| Full test suite (historical, this increment) | `TEST_SUPABASE_DB_URL=... python -m pytest tests/ -q` | **465 passed, 1 skipped** — superseded; current tip `e85d1c6` is 618 passed / 8 skipped (`docs/GROUND-TRUTH-BUILD-LINT-TEST.md`) |
| Lint | `python -m ruff check .` | **All checks passed** |
| Migrations | `tests/test_migration_d2.py` | apply + record + idempotent + legacy preserved |
| Backup/restore drill | `tests/test_backup_restore.py` | real `pg_dump` → `pg_restore` into a fresh DB; data intact |
| Secret-table RLS | `tests/test_backup_restore.py` | `conversation_routes`/`cloud_sandboxes` RLS + no PUBLIC grant |
| Browser E2E | `tests/test_browser_e2e.py` (Chromium) | **passed** — full journey, detail view, mobile overflow |
| Security negative | `tests/test_project_binding.py`, `test_evidence_trust.py`, `test_route_auth.py`, `test_cli_mcp.py`, `test_dispatch_authorization.py` | **passed** |
| Workspace lock | `tests/test_reliability.py` | two-connection race + deterministic index test |
| Direct repo mode | `tests/test_cloud_direct.py` | prompt / refuse / allow / repository-less |
| Clean startup | server start + `GET /healthz` / `GET /readyz` | **200 / ready** |
| Docker Compose | `docker compose up --build` then `/readyz`, `/login`, CLI | **ready / 200 / works** |

New test files (83 new tests over the 382 baseline):

- `test_evidence_trust.py` — signing, verification, tamper detection, key
  revocation, hash chain, project-scoped storage, cross-project read refusal.
- `test_project_binding.py` — link service, canonical identity, cross-project
  DB refusals, `authorize_dispatch` gates, project-delete keeps history.
- `test_dispatch_authorization.py` — the real dispatcher dead-letters an unlinked
  or disabled repository; Sr child tasks inherit the project.
- `test_route_auth.py` — every protected endpoint refuses an unauthenticated
  request (catches an unprotected route); public allowlist reachable; CSRF on
  session writes.
- `test_cli_mcp.py` — CLI exit codes, no token leakage, project isolation; MCP
  tool set, no dangerous tools, stdio round trip.
- `test_browser_e2e.py` — the real user journey in Chromium.
- `test_migration_d2.py` — migration apply/idempotency/legacy preservation and
  the readiness endpoint.
- `test_backup_restore.py` — a real `pg_dump`/`pg_restore` drill and the
  secret-table RLS check.
- `test_cloud_direct.py` (extended) — the direct transport's repository modes.

---

## 4. Capability status

Legend: **IMPLEMENTED** (code exists) · **TESTED** (automated test passes) ·
**BROWSER_VERIFIED** (driven in Chromium) · **E2E_VERIFIED** (full path with a
real external dependency) · **UNVERIFIED** · **BLOCKED** · **NOT_IMPLEMENTED**.
These are not merged: a capability can be IMPLEMENTED and TESTED but not
E2E_VERIFIED.

| Capability | Status |
|---|---|
| Authentication (login, logout, session, CSRF, throttling) | IMPLEMENTED, TESTED, BROWSER_VERIFIED |
| Projects (create, read, update) | IMPLEMENTED, TESTED, BROWSER_VERIFIED |
| Project ↔ repository binding (many-to-many) | IMPLEMENTED, TESTED, BROWSER_VERIFIED |
| Tasks with explicit project context | IMPLEMENTED, TESTED, BROWSER_VERIFIED |
| Workflows lifecycle (create → run → terminal) | IMPLEMENTED, TESTED |
| Dispatch authorization (project/repo/branch/member) | IMPLEMENTED, TESTED |
| Execution state durability | IMPLEMENTED, TESTED |
| OpenHands adapter (create/track/result/timeout/retry/recovery) | IMPLEMENTED, TESTED against the fake Cloud; **live execution UNVERIFIED** |
| Repository selection on the direct transport | IMPLEMENTED (explicit prompt/refuse/allow), TESTED; **live clone UNVERIFIED** |
| Database-enforced workspace lock | IMPLEMENTED, TESTED (concurrency + index) |
| Failure is never reported as success | IMPLEMENTED, TESTED |
| Evidence Trust Engine (Ed25519, keyring, chain, verdicts) | IMPLEMENTED, TESTED |
| Evidence repository identity + commit | IMPLEMENTED, TESTED, BROWSER_VERIFIED |
| Project-scoped evidence signing | IMPLEMENTED, TESTED |
| UI shows real statuses | IMPLEMENTED, BROWSER_VERIFIED |
| UI task/workflow detail views | IMPLEMENTED, BROWSER_VERIFIED |
| No critical IDOR / cross-project access | IMPLEMENTED, TESTED |
| CLI core operations | IMPLEMENTED, TESTED |
| MCP limited tool set (stdio) | IMPLEMENTED, TESTED |
| MCP Streamable HTTP | IMPLEMENTED, TESTED |
| Dashboard shows real data | IMPLEMENTED, TESTED |
| PWA (manifest, service worker, no-store on private) | IMPLEMENTED, TESTED |
| Integration diagnostics | IMPLEMENTED, TESTED |
| Security events visible to operator | IMPLEMENTED, TESTED |
| Backup/restore drill | IMPLEMENTED, TESTED |
| Live OpenHands end-to-end | **UNVERIFIED** (no key in sandbox) |
| File Lab (D-1), Android, voice, multi-account, billing | NOT_IMPLEMENTED (deferred by the brief) |

---

## 5. MVP readiness verdict

**MVP READY — LIVE OPENHANDS VERIFICATION PENDING.**

Why not "FULLY VERIFIED": the one capability that requires a real OpenHands API
key — a conversation that actually runs and returns a result — could not be
exercised here. The NEXUS side of that boundary is complete and tested against a
real local HTTP server that speaks the Cloud V1 surface (the two-phase start,
`READY` gating, error classification), and the adapter is the same code path
that was verified live in earlier work (see `AGENTS.md` and
`CLOUD-INTEGRATION-MAP.md`). No fake success is substituted anywhere: with no
key, the workflow is created and then honestly blocked.

Why not "PARTIALLY READY": every P0 criterion is met except live OpenHands
verification, which is an environment gap, not a missing feature.

---

## 6. Remaining issues (real blockers and limits)

1. **Live OpenHands execution is unverified here** (no API key). Provide
   `OPENHANDS_API_KEY` (and `OPENHANDS_DEFAULT_MODEL` if the sandbox default has
   no key) and run `tests/e2e_cloud_sr_jr.py` to close it.
2. **The `direct` transport selects a repository by instruction, not natively.**
   `supports_repository_selection` is `False`; the default `prompt` mode tells
   the agent to clone the repository itself, and `refuse` mode blocks the task
   instead. Whether the clone actually happens is only provable with a live key.
3. **Legacy RLS is permissive** (`USING (true)`) on the pre-NEXUS tables
   (`workflows`, `tasks`, `conversations`, `task_events`). They are not
   project-isolated. The new `nexus_*` tables and the secret tables are locked.
4. **Single-owner auth.** There is no membership-management UI; the machine
   bearer acts as the configured owner. Multi-user IAM is out of MVP scope.

Resolved since the first report: the workspace claim is now a real
database-enforced lock (migration `0015`); the sandbox `session_api_key` tables
now have RLS (migration `0016`); the `direct` transport no longer silently
ignores repository selection; MCP Streamable HTTP is implemented.

---

## 7. Run the MVP from a clean environment

```bash
# PostgreSQL reachable, then:
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus"
export AUTH_USERNAME=owner
export AUTH_PASSWORD_HASH="$(python -m orchestration.web_auth hash)"   # prompts
export AUTH_SESSION_SECRET="$(python -m orchestration.web_auth secret)"
eval "$(python -m orchestration.evidence_keygen | sed 's/^/export /; /^#/d')"
export WEB_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"

python -m orchestration.web_server --host 0.0.0.0 --port 8000 --token "$WEB_AUTH_TOKEN"
# -> open http://localhost:8000, sign in
# -> GET /healthz {"status":"ok"}; GET /readyz {"status":"ready","database":true}
```

Tests (disposable database only — the suite truncates data):

```bash
pip install -r requirements-dev.txt
export TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus_test"
python -m pytest -q
python -m ruff check .
```

---

## 8. Next five actions

1. Provide a live OpenHands key and run `tests/e2e_cloud_sr_jr.py`; record the
   result in §5. This is the only P0 gap.
2. Decide the `direct` repository-selection policy for production
   (`OPENHANDS_DIRECT_REPO_MODE`) and prove the chosen mode against a live key.
3. Decide the File Lab (D-1) unification (the colliding `0011`) or keep it
   deferred explicitly.
4. Tighten the legacy tables' RLS (`USING (true)`) to project-scoped policies
   once a user/organization model exists.
5. Add a lightweight CI job that runs the browser E2E and the backup/restore
   drill (they need Chromium and the pg client tools).
