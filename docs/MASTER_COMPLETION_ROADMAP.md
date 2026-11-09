# NEXUS — Master Completion Roadmap

Companion to `MASTER_COMPLETION_AUDIT.md` (what is) and
`MASTER_COMPLETION_STATUS.md` (where we are). This file is the plan.

Rules in force (brief §19): never modify `main` directly; never rewrite
history; never force-push; no production migrations; no deploy without explicit
approval; no secrets in logs/UI/evidence. Every phase gets its own branch.

---

## Phase 0 — Preservation & Forensics  ✅ done

Deliverables: preservation tags, `MASTER_COMPLETION_AUDIT.md`, this roadmap,
status, decisions, and the CLI / Self-QA / MCP architecture designs.
No code changes to `main`.

Exit criteria: baseline frozen and re-verified; audit written from the code.

---

## Phase 1 — Architecture & Integration  ⏸ gated

Scope:

1. Fix CI (`.github/workflows/tests.yml` python pin) — separate small PR.
2. Unify the File Laboratory (main's `nexus_files` + nexus's `file_lab`,
   `archive_safety`, `multipart`, `voice`) into one service, no duplication.
3. Unify the GitHub flow (main's `github_confirmations` + nexus's
   `nexus_actions`/`GitHubWriter`) into one confirmation handshake.
4. Author migration `0016` reconciling both `0011` schemas, additive and
   reversible, with a backup step and a safe refusal on an unknown schema.
5. Delete the superseded duplicate modules/tests only after the unified path
   is green on both former test sets.

**Gate:** owner decision on DECISIONS §6 (numbering, limits, GitHub flow).
**Branch:** `nexus/integration` off `main`.

Exit criteria: one file service, one GitHub confirmation flow, both former test
suites pass against the unified code, `0016` applies cleanly to a database that
has either `0011`, and is a no-op on a database that already has the unified
schema.

---

## Phase 2 — Reliability & Security

Scope: workspace claim as a real cross-process lock (DB constraint or
transactional lock); sandbox execution policy documented and enforced; backup /
restore drill on a disposable database; authorization review per project;
concurrency and recovery tests; close SEC-08 (DB secrets) and SEC-09 (legacy
RLS).

Exit criteria: negative security tests pass; restore drill verifies tables,
sequences, constraints, indexes, FKs.

---

## Phase 3 — CLI

Scope: `nexus` CLI backed by the existing services/API (no duplicated business
logic). Commands per the brief §4.1. Requirements: `--help`, colour with
`--no-color`, `--json`, machine-readable exit codes, timeouts, works without a
TTY, secrets never printed. Integration tests.

Exit criteria: every documented command maps to real functionality; CLI test
suite green; `CLI_REFERENCE.md` matches `--help` exactly.

---

## Phase 4 — Self-QA Engine

Scope: `nexus doctor` (component statuses with severity, root cause, action,
evidence ref); `nexus qa run` (18 categories, real integrations not mocks);
Browser E2E via Playwright; Evidence Engine (run id, commit SHA, branch, env,
commands, exit codes, logs, artifacts, integrity check); `nexus qa repair`
(bounded, no test deletion, no assertion weakening, no auto-merge);
independent reviewer with PASS / PASS_WITH_CONDITIONS / FAIL / BLOCKED.

Exit criteria: a full run produces verifiable, commit-bound evidence; the repair
loop is bounded and refuses to mask failures.

---

## Phase 5 — MCP

Scope: MCP client (stdio + Streamable HTTP), MCP server exposing the tool groups
in the brief §6.2, security (authz per project, scopes, validation, size limits,
timeout, rate limit, audit, no arbitrary shell, SSRF/traversal guards), and the
Integrations UI.

Exit criteria: client and server integration-tested; permissions enforced;
destructive actions require confirmation.

---

## Phase 6 — AI Orchestration

Scope: fallback OpenHands orchestrator with health checks, timeout, circuit
breaker, leader/lease ownership, idempotent handover (no double execution);
multi-account (`OPENHANDS_API_KEY_2..N`), selection, balancing, rotation,
credential isolation; configurable agent role profiles.

Exit criteria: handover never double-runs a task; account rotation proven under
simulated failure; secrets never surface.

---

## Phase 7 — Product Completion

Scope: file formats (TXT/MD/PDF/DOCX/XLSX/CSV/JSON/YAML/XML/HTML/source/ZIP),
content extraction, archive inspection, AI analysis; voice + text input
(ru/en, editable transcript, pluggable STT/TTS); UI design system, light/dark,
responsive, real-time status, diff/log/QA/evidence views; project isolation
end to end; unified GitHub connector.

---

## Phase 8 — Full Acceptance

Scope: full E2E, security audit, regression, recovery drill, independent
acceptance audit. Produces `FINAL_ACCEPTANCE_REPORT.md`.

Exit criteria: every item in AUDIT §8 is demonstrably met, with reproducible
evidence, not merely a green pytest.

---

## Critical dependency chain

```
Phase 0 ─► Phase 1 (needs owner decision) ─┬─► Phase 2 ─┐
                                            ├─► Phase 3 ─┼─► Phase 4 ─► Phase 8
                                            ├─► Phase 5 ─┘
                                            └─► Phase 6
Phase 7 depends on Phases 1–2.
```

Nothing in Phases 3–8 can produce trustworthy evidence until **CI is fixed**
(Phase 1 item 1).

---

## Parallel workstreams (brief §18)

Independent after Phase 1 contracts freeze; each owns disjoint files.

| Stream | Owns | Depends |
|---|---|---|
| A. Integration & DB | `orchestration/file_lab*`, `nexus_files*`, `migrations/0016`, `archive_safety`, `multipart` | decision §6 |
| B. Sandbox & Security | `docs/SECURITY.md`, workspace lock, `cloud_store` claim | Phase 1 |
| C. CLI | `orchestration/cli*.py`, `docs/CLI_REFERENCE.md`, `tests/test_cli*` | Phase 1 |
| D. Self-QA & Evidence | `orchestration/qa*.py`, `docs/SELF_QA_ARCHITECTURE.md` | C |
| E. MCP | `orchestration/mcp*.py`, `docs/MCP_ARCHITECTURE.md` | Phase 1 |
| F. Frontend & UX | `orchestration/web_ui.py`, `orchestration/pwa/` | Phase 1 |
| G. AI Orchestration | `orchestration/sr_runtime.py`, `cloud_*`, accounts | Phase 1 |
| H. Docs & QA | `README.md`, `AGENTS.md`, `DEPLOY.md`, `INTEGRATION-MAP.md`, `CLOUD-INTEGRATION-MAP.md` | all |

Owner-of-file rule: one writer per file per stream; changes to a shared file
(`cloud_store.py`, `web_server.py`) are serialized through the coordinator.
