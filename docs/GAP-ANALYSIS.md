# NEXUS Gap Analysis — against the product vision and AUDIT §8 Definition of Done

## 0. Verification basis (the evidence spine)

All findings below were re-verified against the checkout, not taken from any report. Every command was run in this environment.

| Check | Command | Result (verbatim) |
|---|---|---|
| HEAD / tree | `git log --oneline -1` · `git status --porcelain` | `e85d1c6 docs: autonomous lab, worker, clients and live-verification report`; tree **clean** |
| Lint | `python -m ruff check .` | `All checks passed!` |
| Tests | `TEST_SUPABASE_DB_URL=… python -m pytest tests/ -q -p no:cacheprovider` | **`618 passed, 8 skipped, 1 warning in 337.06s`** |
| CI (external) | GitHub Actions API, `branch=main` | `e85d1c6c tests completed success 2026-11-09T18:49:31Z` |
| Skip reasons | `pytest … -rs` on the suspect files | 4× `Android SDK build-tools are not installed`; 3× `mingw-w64 is not installed`; 1× `set TEST_POOLER_DSN to a transaction-mode pooler` |
| Migrations | `ls migrations/` | `0001`–`0011`, `0014`–`0019` (gap `0012`/`0013`, intentional) |
| Runtime | `python -V`; `psql` | Python 3.13.15; PostgreSQL 17.11 |

**The repository's own control documents are stale.** The Phase-0 audit froze a baseline of `main @ bd31381`, `382 passed`, "no CLI/MCP/Self-QA/backup". The current HEAD is `e85d1c6` with `618 passed` and several of those subsystems present. Any gap analysis that trusts `MASTER_COMPLETION_AUDIT.md §3` would be wrong; this one re-derived the state from code.

---

## 1. Headline claims (source-verified)

- **Field**: Current test/lint state of `main`
- **Value**: 618 passed / 8 skipped; ruff clean; CI green
- **Quote**: `618 passed, 8 skipped, 1 warning in 337.06s`
- **Source**: Local checkout, commit `e85d1c6` — command `TEST_SUPABASE_DB_URL=… python -m pytest tests/ -q -p no:cacheprovider` (this is the project's own repository, the primary source per rule 1.4)

- **Field**: Whether the README is internally consistent about MCP transport
- **Value**: No — it states both that Streamable HTTP exists and that it does not
- **Quote**: `- A **CLI** (`nexus`), an **MCP** surface (stdio + Streamable HTTP), an` … `API uses. Streamable HTTP is not implemented in this MVP.`
- **Source**: `README.md` lines 68 and 173 (commit `e85d1c6`)

- **Field**: Whether Streamable HTTP MCP is implemented (code is authoritative)
- **Value**: Yes — `POST /mcp` is served
- **Quote**: `if path == "/mcp":` … `return self._mcp_http()`
- **Source**: `orchestration/web_server.py` lines 1841–1842 (commit `e85d1c6`)

- **Field**: Whether the Self-QA engine exists
- **Value**: No — design only
- **Quote**: `Status: **design only.** Nothing in this document is implemented yet.`
- **Source**: `docs/SELF_QA_ARCHITECTURE.md` line 3 (commit `e85d1c6`)

- **Field**: Whether the MCP client exists
- **Value**: No — server only
- **Quote**: `The **client** (NEXUS connecting to external MCP servers) remains a design below and is **not** implemented.`
- **Source**: `docs/MCP_ARCHITECTURE.md` (commit `e85d1c6`)

- **Field**: Final Acceptance Report status
- **Value**: Not issued
- **Quote**: `Status: **NOT YET ISSUED.** This file is a placeholder created in Phase 0 so the`
- **Source**: `docs/FINAL_ACCEPTANCE_REPORT.md` line 3 (commit `e85d1c6`)

- **Field**: The UI's claimed file-format support vs the code
- **Value**: UI advertises PDF/DOCX/XLSX/PPTX; code previews text only
- **Quote**: `PDF, DOCX, XLSX, PPTX, CSV, TXT, JSON, XML, изображения, исходный код и` … `#: Extensions we will render as text in the preview pane. Anything else is`
- **Source**: `orchestration/web_ui.py` line 465 and `orchestration/file_lab.py` line 36 (commit `e85d1c6`)

- **Field**: Whether both GitHub confirmation mechanisms remain
- **Value**: Yes — the two flows were never unified
- **Quote**: `from .github_confirmations import Confirmations` … `from .nexus_actions import ActionStateError, ConfirmationMismatch, PendingActions`
- **Source**: `orchestration/web_server.py` lines 86–88 (commit `e85d1c6`)

---

## 2. What the "vision" is (the yardstick)

Three in-repo statements of intent, which do **not** fully agree:

1. **NEXUS-SPEC §3–§4** — target modules (Projects/Policy, Goal/Planning, Scheduler, **Account Manager**, **LLM Gateway**, Memory/Context, **Evaluation/QA**, Notifications/UI), a target Task lifecycle, and a target API v1 route list (marked "маршруты пока не существуют").
2. **MASTER_COMPLETION_AUDIT §8** — the Definition of Done (8 items) and a 9-phase roadmap.
3. **MASTER_COMPLETION_ROADMAP Phases 0–8** — the concrete phase plan.

I treat §8 as the acceptance gate (the user asked for it explicitly) and §3/§4 as the product vision.

---

## 3. AUDIT §8 acceptance criteria — status per item

| # | Criterion (§8) | Status | Evidence |
|---|---|---|---|
| 1 | No fake UI buttons; every action calls a real endpoint | **Met** | Every `data-*` attribute has a JS handler (`data-delfile/extract/preview/ws/wsdiff/wsdel/retry/run/…`); `tests/test_route_auth.py` and the browser E2E exercise them. *Caveat:* UI copy over-claims capability (§1 above) — a truthfulness defect, not a dead button. |
| 2 | CLI works, emits JSON, machine-readable exit codes, tested | **Met (shipped set)** | `orchestration/cli.py`; `EXIT = {…}` stable codes; `tests/test_cli_mcp.py`. **Gap:** the SELF_QA CLI (`nexus qa run`, `qa repair`, the 15-check `doctor`) does not exist — `doctor`/`qa report` are thin wrappers over `/healthz`, `/api/system`. |
| 3 | MCP client **and** server work; permissions enforced; integration-tested | **Half met** | Server: implemented (stdio + `POST /mcp`), tested (`test_mcp_*`). Client: **absent** (`grep mcp/client` → 0 hits). |
| 4 | No known P0/P1 vulns; no sandbox bypass; no credential leak | **Mostly met, documented open items** | Redaction, no-redirect clients, `0016` RLS on secret tables, route-auth test all pass. Open: legacy `USING (true)` RLS; single-owner auth; direct-transport repo selection is by *instruction*, not enforcement. |
| 5 | Concurrency, recovery, migrations, backup/restore exercised | **Met** | `tests/test_reliability.py` (2-connection workspace race + index), `tests/test_backup_restore.py` (real `pg_dump`→`pg_restore`), `tests/test_migration_d2.py`, `tests/test_duplicate_requests.py`. *Caveat:* the pooler case is skipped here (`TEST_POOLER_DSN` unset). |
| 6 | Browser E2E covers login, projects, files, tasks, reports, adaptivity | **Partial** | `tests/test_browser_e2e.py` covers login/logout, project, repo-link, task, workflow, evidence, re-verify, mobile overflow (360 px), Autonomous-Lab tab. **Missing:** file upload/preview/download/delete; a "reports" view; tablet width; the SELF_QA rule "fail on any console/network error" is not asserted. |
| 7 | Documentation matches the code | **Not met** | See §5 — README self-contradiction, stale phase board, stale "missing subsystems", stale test counts, unratified DECISIONS §6. |
| 8 | Integration preserves **both** implementations' functionality | **Met for files; not for GitHub** | `0011_nexus_files.sql` is a deliberate superset ("This file unifies the two independent 'project file' designs…"), and `App` holds both services (`self.files`/`self.opaque_files`). GitHub confirmations were **not** unified (both flows live). |

---

## 4. Vision capability gap map

| Vision module (SPEC §3/§4) | Current state (code) | Gap |
|---|---|---|
| Projects / Policy | `nexus_projects.py`, `nexus_project_members` (owner/member), 404 isolation, `authorize_dispatch` gate, DB triggers | **Met.** No membership-management UI; single configured owner. |
| Goal / Planning | `nexus_projects.create_goal`, `nexus_planning.py` (versioned DAG, cycle/foreign-ref validation, owner-only approve), `providers.py` cascade drafts | **Met** for authoring. Goal has only `text` — no `requirements[]/constraints/criteria/open_questions` (SPEC §4). **Plans still do not execute** (by design, stated). |
| Scheduler / Execution | `cloud_store.py` claim/lease/idempotency, `0015` partial-unique workspace lock, `cloud_dispatcher.py`, `lab_worker.py` (separate process), `nexus_lab.py` (`GitVerifier` → `completed_verified`/`completed_unverified`) | **Met and strong.** |
| **Account Manager** | Single `OPENHANDS_API_KEY` (`cloud_client.py:166`); GitHub *multi-account* exists (`nexus_github_accounts.py`) but OpenHands does not | **Absent** (multi-account OpenHands, rotation, per-account limits, fail-closed perms). |
| **LLM Gateway** | `providers.py`: ordered Gemini→Groq→OpenRouter→OpenHands, bounded backoff, `Retry-After`, auth vs transient classification | **Partial.** No circuit breaker, no leader/lease ownership, no idempotent handover (ROADMAP Phase 6). |
| Memory / Context | `nexus_context.py`: provenance (`agent_reported`→`independently_verified`), project/global scope, dedupe, supersede, `verified_only` | **Partial.** No FTS, no `knowledge_documents`, no embeddings — a deliberate comment: "No embeddings: the request explicitly said not to add a vector store". SPEC §3.6 wants "FTS до pgvector". |
| **Evaluation / QA** | Ed25519 Evidence Trust Engine (`evidence_trust.py`) + lab `GitVerifier`. No QA runner, no `EvidenceStore`, no evaluation lifecycle, no repair loop, no reviewer | **The largest gap.** `grep evaluations` → 0 hits; `docs/SELF_QA_ARCHITECTURE.md` = "design only". This is the "Evaluation Engine" SPEC repeatedly flags as unverified/missing. |
| Notifications / UI | `nexus_notifications` + Autonomous-Lab tab; Russian, dark-only | **Partial.** No realtime (no SSE/WebSocket), no push, no light/dark, no ru/en (ROADMAP Phase 7). |
| API v1 target routes | Implemented: projects, goals (create/list), plans (create/get/approve/plan-draft), tasks (create/list/get), evidence (list/get/verify), events, notifications | **Missing:** project-scoped `tasks/{id}/cancel|retry` (only legacy `/api/tasks/{id}/retry`, `/api/workflows/{id}/cancel`), `tasks/{id}/evaluations` (grep = 0). |

---

## 5. Documentation-vs-code drift (the repo's honesty system has slipped)

| Document | Claim (verbatim) | Reality |
|---|---|---|
| `README.md` | line 68 "MCP surface (stdio + Streamable HTTP)" vs line 173 "Streamable HTTP is not implemented in this MVP." | `POST /mcp` **is** implemented — self-contradiction. |
| `docs/MASTER_COMPLETION_STATUS.md` | Phase board: "2 Reliability & Security ⬜ not started", "3 CLI ⬜ not started", "5 MCP ⬜ not started" | All three are substantially delivered (lock, backup, CLI, MCP server). |
| `docs/MASTER_COMPLETION_AUDIT.md §3` | "CLI … No `cli.py`"; "MCP server Absent"; "Backup/Restore … 0 hits" | `cli.py`, `mcp_server.py`, `test_backup_restore.py` all exist. |
| `docs/NEXUS-AUDIT.md` | "433 теста, 1 пропущен" | This run: **618 passed, 8 skipped**. |
| `docs/AUTONOMOUS-LAB.md` | "**625 passed, 1 skipped** (was 557 before this work)" | This run: **618 passed, 8 skipped**. |
| `docs/MASTER_COMPLETION_DECISIONS.md §6` | DECISION-1 "unified migration as **0016**"; DECISION-2 "32 MiB/file, 512 MiB/project, ratio 20×" | `0016` is the secret-RLS migration; the unified file migration is `0011`. Code caps: `nexus_files.py` = **5 MiB**; `file_lab.py` = **32 MiB**; `archive_safety.py` = **256 MiB total, ratio 200×**. The decision was never ratified or applied. |
| `docs/SECURITY.md §5` | "bounded (5 MiB/file, 20 MiB and 100 files per project)" | True only for the *opaque* `FileService`; the File Lab path allows 32 MiB. Two subsystems, one documented. |
| `docs/FINAL_ACCEPTANCE_REPORT.md` | "NOT YET ISSUED" | Accurate, but it is the §8 gate deliverable and remains a placeholder. |

---

## 6. Prioritised, file-targeted backlog

### P0 — blocks the §8 acceptance gate or the vision's core

**B-01 · Self-QA / Evidence engine (Roadmap Phase 4; SPEC §3.7, §4 Evaluation)**
- *Gap:* no `orchestration/qa/`; `grep QaRunner|EvidenceStore|RepairLoop` = 0; `SELF_QA_ARCHITECTURE.md` = "design only".
- *Target files:* new `orchestration/qa/{diagnostics,runner,evidence,repair}.py`; `orchestration/cli.py` (`qa run`, `qa repair`, full `doctor`); `tests/test_qa_*.py`; update `docs/SELF_QA_ARCHITECTURE.md`.
- *Acceptance test:* a run produces a commit-bound, integrity-checked `EvidenceRecord`; a tampered/absent record is **not** accepted; the repair loop refuses test deletion/assertion weakening.

**B-02 · Documentation truth pass (AUDIT §8.7)**
- *Gap:* the drift table in §5.
- *Target files:* `README.md` (drop the line-173 contradiction), `docs/MASTER_COMPLETION_STATUS.md`, `docs/MASTER_COMPLETION_AUDIT.md` §3/§4, `docs/NEXUS-AUDIT.md`, `docs/AUTONOMOUS-LAB.md`, `docs/MASTER_COMPLETION_DECISIONS.md` §6, `docs/SECURITY.md`, `docs/FINAL_ACCEPTANCE_REPORT.md`.
- *Acceptance test:* every numeric/status claim in the docs is reproducible by a named command; the phase board reflects delivered phases.

**B-03 · Evaluation lifecycle + `…/tasks/{id}/evaluations` route (SPEC §4)**
- *Gap:* `evaluations` appears nowhere in code or migrations; task lifecycle lacks `evaluating`/`rework`.
- *Target files:* new migration; `orchestration/cloud_store.py` (`_ALLOWED`); `orchestration/web_server.py`; `tests/`.
- *Acceptance test:* `ACCEPTED` is impossible without all mandatory evidence; missing evidence ⇒ `BLOCKED`, never success.

### P1 — correctness, security, or a stated phase deliverable

**B-04 · Unify the GitHub confirmation flow (D-03, DECISION-3, Phase 1)**
- *Gap:* `github_confirmations.py` **and** `nexus_actions.py` both wired (`web_server.py:86–88, 350, 1872–1876`).
- *Target files:* `orchestration/github_confirmations.py`, `orchestration/nexus_actions.py`, `orchestration/web_server.py`, `tests/test_github_confirmations.py` + `tests/test_file_lab.py`.

**B-05 · MCP client (SPEC §3, Roadmap Phase 5)**
- *Gap:* client absent (`MCP_ARCHITECTURE.md` says so).
- *Target files:* new `orchestration/mcp/client.py`; `docs/MCP_ARCHITECTURE.md`; `tests/test_cli_mcp.py`.
- *Acceptance test:* real stdio + real Streamable-HTTP servers; scope/allow-list enforcement; redirect refusal.

**B-06 · Complete the Browser E2E (AUDIT §8.6 + SELF_QA §5)**
- *Gap:* no file upload/preview/download/delete, no reports view, no tablet width, no console/network-error assertions.
- *Target files:* `tests/test_browser_e2e.py`.

**B-07 · Project-scoped task cancel/retry + events pagination (SPEC §4)**
- *Gap:* only legacy owner-level routes exist; no cursor/pagination on `events`.
- *Target files:* `orchestration/web_server.py`, `orchestration/cloud_store.py`.

**B-08 · Legacy RLS tightening (D-07 / SEC-09)**
- *Gap:* `0001_init.sql:117–125` and `0002_sr_runtime.sql:46` still `USING (true)`.
- *Target files:* new migration; `orchestration/cloud_store.py`. **Blocker:** needs a user/organization model (stated in SPEC §7).

**B-09 · UI file-format honesty (or real extraction)**
- *Gap:* `web_ui.py:465` advertises PDF/DOCX/XLSX/PPTX; `file_lab.py:36` previews text only.
- *Target files:* `orchestration/web_ui.py`, `orchestration/file_lab.py`.

**B-10 · Sandbox policy for untrusted tests (SPEC §7; blocks B-01)**
- *Gap:* no isolated runner; `grep subprocess|docker` in `orchestration/` is empty (by design, ADR-004).
- *Target files:* `docs/SECURITY.md`, new runner module, `tests/`.

**B-11 · Autonomous-lab replanner**
- *Gap:* `nexus_lab.py:322` — "A human or a replan step must resolve it."
- *Target files:* `orchestration/nexus_lab.py`, `orchestration/nexus_planner.py`.

### P2 — vision completion, not blocking acceptance

| ID | Gap | Target files |
|---|---|---|
| B-12 | Observability: no correlation IDs, metrics, `audit_events` | `orchestration/web_server.py`; new migration |
| B-13 | Memory FTS / knowledge store (SPEC §3.6) | `orchestration/nexus_context.py`; new migration |
| B-14 | Multi-account OpenHands + role profiles + circuit breaker/leader handover (Phase 6) | `cloud_client.py`, `cloud_direct.py`, `providers.py`; new migration |
| B-15 | UI: light/dark, ru/en, realtime streaming (Phase 7) | `orchestration/web_ui.py`, `orchestration/pwa/` |
| B-16 | Server-side STT/TTS | `orchestration/voice.py` |
| B-17 | Multi-user IAM / membership-management UI | `nexus_projects.py`, `web_server.py`, `web_ui.py` |
| B-18 | CI: exercise the pooler path and the skipped runtime tests | `.github/workflows/tests.yml`, `tests/test_schema_bootstrap.py` |

---

## 7. Bottom line

The engineering core is in better shape than the control documents say: **618 green tests, clean lint, green CI**, with the durable dispatcher, the DB-enforced workspace lock, the Ed25519 Evidence Trust Engine, the autonomous lab, the CLI, the MCP server and the backup/restore drill all real. Of the eight §8 criteria, **1, 2, 5 and 8 are met** (8 with a GitHub caveat), **4 is met with documented open items**, and **3, 6 and 7 are not**.

The three things that actually block the §8 gate are, in order: **(B-01) the missing Self-QA / Evidence engine** — the "Evaluation Engine" the SPEC names as unverified and without which "passing tests" is not acceptance; **(B-02) the documentation drift**, which is cheap to fix and is itself a §8 criterion; and **(B-03) the absent evaluation lifecycle/route**. The remaining P1 items are one unification (GitHub), one missing half (MCP client), and one security decision (legacy RLS) that is genuinely blocked on a user/org model.

*This analysis was produced by an AI agent (OpenHands) on behalf of the user. Every claim is traceable to a command or a file:line in the checkout at commit `e85d1c6`.*
