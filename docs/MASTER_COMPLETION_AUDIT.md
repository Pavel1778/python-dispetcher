# NEXUS — Master Completion Audit (Phase 0)

Status: **Phase 0 reconnaissance, evidence-based.** Every fact below was
re-verified against the repository at the commit named in §0, not taken from
any earlier report. Where a claim in the programme brief did not match the
repository, the discrepancy is recorded in §1.4 and the repository wins.

Author: OpenHands agent (Principal Architect / Staff Backend / DevOps / AppSec /
AI-Agent / QA-Automation roles) on behalf of the repository owner.

---

## 0. Baseline (frozen)

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `bd31381` (Merge PR #18) |
| Working tree | clean |
| Preservation tags | `preservation/main-tip` → `bd31381`, `preservation/nexus-tip` → `9287a44`, `preservation/v2.0.0` → `363f20f`, `preservation/audit-baseline-2026-11-08` → `bd31381`, `preservation/docs-dff2966` → `dff2966` |
| Open PR | #19 `docs/branch-topology-env-table` → `main` (docs only) |
| Local test run | `382 passed, 1 skipped` against PostgreSQL 17 (`TEST_SUPABASE_DB_URL`) |
| Lint | `ruff check .` → All checks passed |
| CI | **RED on every branch** (see §4.1) |

Python used locally: 3.13.15. Declared support: ≥3.10. CI/Render pin `3.12.6`.

---

## 1. What the repository actually is

### 1.1 One-line description

A **durable control plane** that orchestrates OpenHands agent conversations:
it owns task state, leases, assignment, recovery and quality gates; OpenHands
executes the engineering work. State lives in PostgreSQL; conversations run on
OpenHands Cloud.

### 1.2 Real architecture (verified from code)

```
                     ┌────────────────────────────────────────────┐
  browser ──session──│  web_server.py  (stdlib HTTP, 1326 LOC)     │
  script ──Bearer────│   37 routes, background loop, CSRF         │
                     └───────────────┬────────────────────────────┘
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                             ▼
  SrRuntime.tick()          CloudDispatcher.tick()      WorkflowVerifier.tick()
  (sr_runtime.py)           (cloud_dispatcher.py)       (workflow_verifier.py)
   Sr client-tools           Jr task dispatch            terminal workflow state
        │                            │                             │
        ▼                            ▼                             │
   SrTools (sr_tools.py)      build_cloud_client()                 │
   durable Sr ops             ├─ cloud_direct.py  (default)        │
                              └─ cloud_client.py  (app, legacy)    │
                                     │                             │
                                     ▼                             ▼
                          OpenHands Cloud V1 / sandbox       cloud_store.py
                          (remote; NOT local Docker)         (PostgreSQL, 1779 LOC)
```

**Confirmed responsibilities:**

- `cloud_store.py` — the only durable store. Atomic claim
  (`SELECT ... FOR UPDATE SKIP LOCKED`), leases, `_ALLOWED` state machine,
  `force_transition` for legal-path recovery, idempotency keys, route table.
- `cloud_dispatcher.py` — two-phase create (start task → poll → bind), repo
  allowlist, bounded retry with backoff, recovery of expired leases.
- `sr_runtime.py` — the Sr↔system bridge: start Sr with `client_tools`, poll
  `events/search` for `ActionEvent`, claim by event id, execute, deliver result.
- `workflow_verifier.py` — the only writer of terminal workflow states.
- `web_server.py` — HTTP + the single background loop (Sr tick, Jr tick,
  verifier tick, sweeps).

**Legacy (still present, not the live path):** `control_plane.py` (SQLite),
`dispatcher.py`, `openhands_client.py`, `local_runtime.py`.

### 1.3 Capabilities that genuinely exist

| Capability | Where | Status |
|---|---|---|
| Workflow → Sr → Jr delegation | `sr_runtime.py`, `sr_tools.py`, `cloud_dispatcher.py` | Real, tested |
| Durable state + atomic claim + leases | `cloud_store.py` | Real, tested |
| Idempotent submit/dispatch/start | migrations 0006, `claim_sr_start` | Real, tested |
| Two Cloud transports (direct/app) | `cloud_direct.py`, `cloud_client.py` | Real |
| Web auth: session + Bearer + CSRF + throttle | `web_auth.py` | Real, tested |
| GitHub discovery + guarded writes | `github_client.py`, `github_confirmations.py` | Real, tested |
| NEXUS projects / goals / plans | `nexus_projects.py`, `nexus_planning.py` | Real (manual plans) |
| Project file storage (opaque) | `nexus_files.py`, `0011_nexus_files.sql` | Real, tested |
| PWA shell | `orchestration/pwa/`, `test_pwa.py` | Real |
| Operator controls (cancel/resume/retry/rename) | `cloud_store`, `web_server` | Real, tested |

### 1.4 Discrepancies between the brief and the repository

These are recorded because the brief instructed me not to assume prior reports
reflect the repository. The repository was treated as authoritative.

| Brief states | Verified reality |
|---|---|
| "migration **0015** exists; do not change it; prepare **0016**" | Highest migration is **`0011`**. No `0015`. |
| "use the results of the **independent audit**" | `docs/NEXUS-AUDIT.md` **does not exist**. Only `CLIENTS.md`, `DEVELOPMENT-LAB.md`, `NEXUS-OPERATIONS.md`, `NEXUS-SPEC.md`. |
| "do not delete **preservation snapshots**" | None existed before this phase; created in §0. |
| "Phase 0/1 acceptance audit, Phase 2 remediation already started" | No such artifacts in the repo. |
| Implied: `main` is green | CI is **red on every branch** (§4.1). Local tests pass. |

---

## 2. Duplicated subsystems (the central problem)

Two branches implement the **same** feature independently:

| Concern | `main` | `nexus/laboratory-files-github` |
|---|---|---|
| Migration | `0011_nexus_files.sql` | `0011_file_lab.sql` |
| Table `nexus_files` columns | `name, size integer (5 MiB), sha256, content, created_by` | `filename, content_type, size bigint (32 MiB), sha256, data, is_archive, uploaded_by` |
| Extra tables | — | `nexus_workspaces`, `nexus_workspace_files`, `nexus_voice_notes`, `nexus_pending_actions` |
| File service | `nexus_files.FileService` | `file_lab.FileLabService` |
| Archive safety | none (opaque bytes) | `archive_safety.py` (Zip Slip, bombs, symlinks) |
| GitHub writes | `github_confirmations.py` + `GITHUB_ENABLE_WRITES` | `GitHubWriter` + `nexus_actions.py` handshake |
| GitHub routes | `/api/github/prepare\|confirm` | `/api/v1/projects/{id}/github/*` |
| Multipart | none (raw body + `X-File-Name`) | `multipart.py` |
| Voice | none | `voice.py` |
| Tests | `test_lab_files.py`, `test_github_confirmations.py`, `test_github_write_http.py` | `test_file_lab.py`, `test_archive_safety.py` |

Both sides are individually green (main 382, nexus 445). The `nexus_files`
table name collides with incompatible columns, so **both migrations cannot be
applied**. This is a design decision, not a merge conflict.

**Decision required (owner):** §6 of `MASTER_COMPLETION_DECISIONS.md`.

---

## 3. Missing subsystems (claimed by the programme, absent in code)

Verified by grep across `orchestration/`:

| Subsystem | Evidence of absence |
|---|---|
| **CLI** (`nexus ...`) | No `cli.py`, no `argparse` entry point beyond `web_server`/`web_auth`. |
| **MCP client** | No MCP protocol code. `mcp_config` appears only as a *read* of the account's settings. |
| **MCP server** | Absent. |
| **Self-QA / Evidence engine** | No run store, no evidence records, no QA runner. |
| **Backup/Restore** | `grep backup\|pg_dump` in `orchestration/` → 0 hits. |
| **Local sandbox / Docker execution** | `grep subprocess\|docker` in `orchestration/` → **0 hits**. "Sandbox" is OpenHands Cloud's remote sandbox, not local isolation. |
| **Multi-account OpenHands** | Single `OPENHANDS_API_KEY`; no `_2/_3`, no rotation. |
| **Fallback orchestrator** | Absent; only the internal background loop. |
| **Agent role profiles** | `OPENHANDS_AGENT_PROFILE_ID` selects one profile; no role model. |
| **Observability** | `logging` only; no correlation IDs, metrics, or audit log. |
| **GitHub connector (unified)** | Discovery + guarded writes only; no PR/CI/branch management. |

---

## 4. Critical defects

### 4.1 CI is broken on every branch (P0 for the programme)

`.github/workflows/tests.yml` pins `python-version: "3.12.6"`. The runner image
is now Ubuntu 26.04, which does not ship 3.12.6:

```
##[error]The version '3.12.6' with architecture 'x64' was not found for Ubuntu 26.04.
```

`actions/setup-python@v5` fails, so lint and test never run. Confirmed failing
on `main` (`bd31381`) and on `nexus/laboratory-files-github` (`9287a44`). No
CI-based acceptance evidence is possible until this is fixed.

### 4.2 Workspace claim check is not a cross-process lock

`claim_task` refuses/skips a candidate whose workspace is occupied, but the
check reads other rows and does not serialize two concurrent claims of
different rows for one workspace. Documented in `NEXUS-SPEC.md`; still open.

### 4.3 `direct` transport does not verify repository/branch selection

Documented in `DEPLOY.md` and `NEXUS-SPEC.md`: `selected_repository` /
`selected_branch` are not proven to have been applied. Repository-scoped work
must not rely on it yet.

### 4.4 Secrets in the database

`0007_cloud_direct.sql` stores `session_api_key` in `conversation_routes` and
`cloud_sandboxes`. The claim "all secrets live only in ENV" is therefore false;
these tables need a grant/RLS review.

### 4.5 Legacy RLS is permissive

`0001_init.sql` / `0002_sr_runtime.sql` use `USING (true)` for authenticated
reads. New `nexus_*` tables are correctly locked, but legacy tables are not
project-isolated.

---

## 5. Security findings (static review, Phase 0)

| ID | Area | Finding | Severity | Status |
|---|---|---|---|---|
| SEC-01 | GitHub creds | Token redacted in `__repr__`, never logged/returned; `_NoRedirect` prevents redirect leak | — | Good, keep |
| SEC-02 | Client IP | Rightmost trusted hop used; per-IP + global login ceilings | — | Good, keep |
| SEC-03 | Redirects | `CloudClient` refuses all; `OpenHandsClient` same-origin only | — | Good, keep |
| SEC-04 | Path traversal (main file lab) | `validate_filename` rejects `/`, `\`, control chars | — | Good |
| SEC-05 | Path traversal (nexus lab) | `archive_safety.py` rejects traversal/absolute/drive/UNC | — | Good (branch) |
| SEC-06 | Archive bombs | Only nexus branch has limits (count/size/ratio) | — | main lacks this |
| SEC-07 | MIME spoofing | Uploads are opaque bytes, `nosniff` on download; no content-type trust | Low | Acceptable |
| SEC-08 | DB secrets | `session_api_key` in DB (§4.4) | Med | Open |
| SEC-09 | Legacy RLS | `USING (true)` (§4.5) | Med | Open |
| SEC-10 | No local sandbox | Untrusted code is never executed locally; execution is remote | — | By design; document |

Full checklist execution (ZIP Slip, bombs, traversal, MIME, post-extraction
size, unsafe names, credential leaks, authz, write confirmation, control
bypass) is scheduled in Phase 2 once the unified implementation exists.

---

## 6. Dependency map

```
web_server.py ──┬─► cloud_store.py ──► PostgreSQL
                ├─► cloud_dispatcher.py ─► cloud_direct.py ─► OpenHands Cloud
                ├─► sr_runtime.py ─► sr_tools.py ─► cloud_store
                ├─► workflow_verifier.py ─► cloud_store
                ├─► web_auth.py ─► cloud_store (sessions)
                ├─► github_client.py ─► GitHub API
                ├─► nexus_projects.py / nexus_planning.py / nexus_files.py ─► cloud_store
                └─► web_ui.py (static strings)
migrations/*.sql ──► schema (applied on start unless AUTO_MIGRATE=false)
tests/fake_{cloud,runtime,github}.py ──► real local HTTP servers (not mocks)
```

**Hard external dependencies:** PostgreSQL, OpenHands Cloud API, GitHub API
(optional). **Runtime dep:** `psycopg[binary]` only.

---

## 7. Implementation plan (phases, from the brief §17)

| Phase | Scope | Gate |
|---|---|---|
| 0 | Preservation, audit, baseline, this document | **done (this PR)** |
| 1 | Fix CI; unify file lab + GitHub; migration `0016`; remove duplication | Owner decision §6 DECISIONS |
| 2 | Reliability & security: workspace lock, sandbox policy, backup/restore, authz, concurrency | Phase 1 merged |
| 3 | CLI (`nexus ...`) with JSON + exit codes + tests | Phase 1 contracts frozen |
| 4 | Self-QA engine: doctor, QA runner, evidence, repair loop, independent reviewer | Phase 3 |
| 5 | MCP client + server + security + UI | Phase 3 |
| 6 | AI orchestration: fallback orchestrator, multi-account, role profiles | Phase 1 |
| 7 | Product completion: file formats, voice, UI, projects, GitHub | Phases 1–2 |
| 8 | Full acceptance: E2E, security audit, regression, recovery | all |

---

## 8. Acceptance criteria (Definition of Done, from the brief §20)

- No fake UI buttons; every action calls a real endpoint.
- CLI commands work, emit JSON, return machine-readable exit codes, are tested.
- MCP client and server work; permissions enforced; integration-tested.
- No known P0/P1 vulnerabilities; no sandbox bypass; no credential leak.
- Concurrency, recovery, migrations and backup/restore are exercised.
- Browser E2E covers login, projects, files, tasks, reports, adaptivity.
- Documentation matches the code.
- **Integration is not "done" because tests pass**: functionality of *both*
  original implementations must be demonstrably preserved (§B of the decision).

---

## 9. Immediate next actions (safe, reversible — started now)

1. **Fix CI** (§4.1) on a branch, not `main`. Unblocks all evidence.
2. Author `MASTER_COMPLETION_ROADMAP.md`, `_STATUS.md`, `_DECISIONS.md`.
3. Author `SELF_QA_ARCHITECTURE.md`, `CLI_REFERENCE.md`, `MCP_ARCHITECTURE.md`
   as **designs only** (no fake commands).
4. Open the integration branch `nexus/integration` off `main`.

**Blocked on owner decision:** the three items in
`MASTER_COMPLETION_DECISIONS.md` §6 (migration numbering, file/archive limits,
canonical GitHub flow). These touch the DB schema, product limits and the
public API, which the brief §22 classifies as requiring an explicit decision.
