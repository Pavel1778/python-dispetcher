# NEXUS — Master Completion Status

Living status board. Updated at the end of every phase. Facts, not intentions.

Last updated: Phase 0 (2026-11-08).

---

## Current position

| Field | Value |
|---|---|
| Active phase | Phase 0 (Recon) — complete; Phase 1 gated on owner decision |
| Working branch | `nexus/phase0-recon` |
| Base | `main` @ `bd31381` (unchanged, not touched) |
| Open PR | #19 (docs only) |
| Integration branch | not yet created (Phase 1) |

## Phase board

| Phase | Status | Evidence |
|---|---|---|
| 0 Preservation & Forensics | ✅ done | preservation tags + `MASTER_COMPLETION_AUDIT.md` |
| 1 Architecture & Integration | ⏸ gated | needs DECISIONS §6 |
| 2 Reliability & Security | ⬜ not started | — |
| 3 CLI | ⬜ not started | — |
| 4 Self-QA Engine | ⬜ not started | — |
| 5 MCP | ⬜ not started | — |
| 6 AI Orchestration | ⬜ not started | — |
| 7 Product Completion | ⬜ not started | — |
| 8 Full Acceptance | ⬜ not started | — |

## Verification log (reproducible)

| Check | Command | Result |
|---|---|---|
| Lint | `python -m ruff check .` | All checks passed |
| Tests (historical, `main` @ `bd31381`) | `TEST_SUPABASE_DB_URL=... python -m pytest tests/ -q` | 382 passed, 1 skipped — **superseded**; current tip `e85d1c6` is 618 passed / 8 skipped (`docs/GROUND-TRUTH-BUILD-LINT-TEST.md`) |
| CI (before fix) | Actions on `main` @ `bd31381` | ❌ `python 3.12.6` unavailable on runner |
| CI (after fix) | Actions run 42521226852 on `fix/ci-python-version` | ✅ **success** |
| Migration inventory | `ls migrations/` | `0001`..`0011` (no `0015`) |

## Defect board

| ID | Defect | Severity | Phase |
|---|---|---|---|
| D-01 | CI red on every branch | P0 (programme) | 1 — **FIXED** (PR #20, CI green) |
| D-02 | File-lab implemented twice, `nexus_files` schema collides | P0 (architecture) | 1 |
| D-03 | GitHub confirmation implemented twice | P1 | 1 |
| D-04 | Workspace claim is not a cross-process lock | P1 | 2 |
| D-05 | `direct` transport does not verify repo/branch | P1 | 2 |
| D-06 | `session_api_key` stored in DB | P2 (security) | 2 |
| D-07 | Legacy RLS `USING (true)` | P2 (security) | 2 |
| D-08 | No CLI / MCP / Self-QA / backup / multi-account | Feature gap | 3–6 |

## Open PRs

| PR | Branch | Purpose |
|---|---|---|
| #19 | `docs/branch-topology-env-table` | docs (Phase A) |
| #20 | `fix/ci-python-version` | CI fix (D-01) — CI green |
| #17 | `nexus/laboratory-files-github` | the pre-existing nexus branch PR |
| — | `nexus/phase0-recon` | Phase 0 documents (this work) |

## Blockers

- **Phase 1 is blocked** on the owner decision recorded in
  `MASTER_COMPLETION_DECISIONS.md` §6: migration numbering, file/archive
  limits, canonical GitHub flow. These touch schema, product limits and the
  public API.
- **All evidence is blocked** on D-01 (CI).

## Risks

| ID | Risk | Mitigation |
|---|---|---|
| R-01 | Choosing one file-lab implementation silently drops the other's features | Compatibility matrix required before merge (Phase 1) |
| R-02 | `0016` applied to an unknown schema | Safe refusal on unknown schema; backup; reversible |
| R-03 | Scope is very large | Strict phase gates; parallel streams with file ownership |
| R-04 | Render free tier sleeps, so the loop is not continuous | Documented; not a 24/7 SLA |
