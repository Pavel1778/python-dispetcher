# MASTER ACCEPTANCE REPORT — NEXUS 3.0 independent audit

**Date:** 2026-11-20 · **Auditor:** OpenHands agent
**Overall verdict: BLOCKED on the Phase 2 candidate; ACCEPTED WITH CONDITIONS for what exists.**

Read `FORENSIC_BASELINE.md` first. Everything below was measured in this session;
nothing is carried over from the previous report without re-checking.

---

## 1. Repository and branch state

| Fact | Value |
| --- | --- |
| Remote | `github.com/Pavel1778/python-dispetcher` |
| `origin/main` | `e866666fb69a13e0edc0c7670b9f56e3561b34c0` |
| Audit branch | `nexus/3.0-audit` (created from `origin/main`, isolated) |
| Worktrees | 1 · Stashes: none · Tags: 10 `preservation/*` |
| Phase 2 candidate `nexus/3.0-foundation` | **does not exist** (404) |
| Reported commit `9c11f52` | **does not exist** (422) |
| Reported prior commit `5a9989b` | **does not exist** (422) |
| Brief's baseline `2edf3aa` | exists, but is **not** current `main` |

**The mission's candidate cannot be audited because it is not in this
repository.** Per the mission's own rule, no substitute branch was presented as
the candidate.

---

## 2. Exact candidate SHA and provenance

**None.** There is no candidate SHA: `9c11f52` resolves to nothing locally or on
GitHub. The audit was performed against `origin/main` `e866666`, which is real,
verified, and contains the four Rust crates that do exist.

---

## 3. Phase 2 acceptance verdict

**BLOCKED**, because six of the crates named in the brief (`nexus-store`,
`nexus-worker`, `nexus-api`) do not exist and the candidate branch is absent.
For the parts that do exist (claim, reclaim, parity), **ACCEPTED WITH
CONDITIONS**. Details: `PHASE2_ACCEPTANCE_REPORT.md`.

---

## 4. Verified components

| Component | Verdict | Evidence |
| --- | --- | --- |
| Python run lifecycle + state machine | **VERIFIED** | `_RUN_ENTERED_FROM`, `_apply_run_state`; transitions inspected |
| Lock ordering (no inversions) | **VERIFIED** | programmatic scan of 14 locking functions: **0 inversions** |
| Concurrent claiming, 1–64 workers | **VERIFIED** | `tests/test_concurrent_claims.py` — distinct grants, no duplicate lease, no lost task |
| 128-worker stress | **VERIFIED** | 256 tasks, each granted exactly once |
| Durable dispatch crash windows | **VERIFIED** | `tests/test_dispatch_durability.py` (9) + `test_start_handover.py` (9) |
| Fencing on every post-claim write | **VERIFIED** | `tests/test_race_protection.py` (13) |
| Auth on every route | **VERIFIED** | mechanical scan + `test_route_auth.py` (11) |
| No SQL injection | **VERIFIED (attacked)** | `tests/test_sql_injection.py`; 5 payloads stored as text |
| Project isolation (404) | **VERIFIED** | 40 tests |
| Secret redaction | **VERIFIED** | `test_github_client.py`, `test_github_accounts.py` |
| Rust claim + reclaim | **VERIFIED** | 13 claim tests, zero skips (probe + isolated DB) |
| Python↔Rust parity (11 of 30 areas) | **VERIFIED** | 9 parity tests |
| Rust fmt + clippy | **FIXED + VERIFIED** | was failing; now clean with `-D warnings` |

---

## 5. Discovered defects (all real, all fixed or documented)

| # | Defect | Where | Action |
| ---: | --- | --- | --- |
| 1 | `cargo fmt --all --check` failed | `nexus-cli/src/main.rs` | **fixed** (`cargo fmt`) |
| 2 | `clippy::manual_clamp` | `nexus-domain/src/retry.rs:112` | **fixed** (`clamp(0, 20)`) |
| 3 | `clippy::manual_range_contains` | `nexus-domain/tests/domain.rs:166` | **fixed** (`(60.0..66.0).contains`) |
| 4 | CI never ran `cargo fmt`/`clippy`, so 1–3 shipped green | `.github/workflows/tests.yml` | **fixed** (new step, `-D warnings`) |
| 5 | No concurrency test at the widths the brief requires | `tests/` | **added** 1–64 + 128 |
| 6 | No SQL-injection regression test | `tests/` | **added** |
| 7 | The brief's performance numbers (194.2/170.3 tasks/s) have no source in the repo | — | **documented NOT VERIFIED** |
| 8 | Point C can duplicate if the tag search fails | `nexus_lab.py:_adopt` | **documented as an open gap** (fix designed, not implemented) |

Defects 1–6 were fixed on the audit branch with regression coverage. Defects 7–8
cannot be "fixed" by code alone: 7 is a missing measurement source, 8 is a design
trade-off recorded in `DISPATCH_RECOVERY_PROTOCOL.md`.

---

## 6. Fixes implemented

1. `cargo fmt --all` (formatting only).
2. `refusals.max(0).min(20)` → `refusals.clamp(0, 20)`.
3. `with_seed >= 60.0 && with_seed < 66.0` → `(60.0..66.0).contains(&with_seed)`.
4. `Rust fmt and clippy` CI step with `-D warnings`.

No `#[allow]` suppressions, no skipped tests, no weakened assertions.

---

## 7. New tests

| File | Cases | What it proves |
| --- | ---: | --- |
| `tests/test_concurrent_claims.py` | 12 + 1 opt-in | distinct grants / one lease / no lost task at 1–64 (and 128) |
| `tests/test_sql_injection.py` | 1 | hostile SQL through the API is stored as text |
| `tests/test_dispatch_durability.py` | 9 | every crash window of a dispatch |
| `pytest.ini` | — | registers the `slow` marker |

---

## 8. Full test results

| Run | Result |
| --- | --- |
| Full suite (this audit, PostgreSQL 17.11) | **2074 passed, 61 skipped, 0 failed** in 6m33s |
| Rust (`cargo test --workspace --locked --release`) | **30 passed** |
| Rust clippy (`-D warnings`) | **clean** |
| Rust fmt (`--check`) | **clean** |
| Python↔Rust (`test_rust_parity.py` + `test_rust_claim.py`, probe + isolated DB) | **22 passed, 0 skipped** |

The 61 skips are environmental: 38 Chromium (absent locally), 13 Rust-claim
(need the probe + isolated DB — run separately at 13/13), 4 Android SDK,
3 mingw, 1 pooler DSN, 1 section-F live variant. None counts as a pass.

---

## 9. PostgreSQL concurrency results

| Width | Outcome |
| ---: | --- |
| 1, 2, 4, 8, 16, 32, 64 | every task granted exactly once; no duplicate lease; nothing lost |
| 128 (opt-in) | 256 tasks, each once |

Two apparent failures during development were **the test's fault**, recorded
because they are instructive: the global in-flight cap (8) and the run iteration
budget (50) both limit a drain by design.

---

## 10. Python/Rust parity results

**11 of 30 areas proven, 5 partial, 14 blocked** (no Rust implementation).
See `PARITY_MATRIX.md`. The 22 existing parity/claim checks pass; the ≥100 target
is unreachable without 14 areas of missing Rust code, and was not faked.

---

## 11. Crash recovery results

All seven crash points A–G of the brief were mapped to the real code; each is
covered by a test or documented as a known gap (point C with a failed tag
search). The guarantee is stated honestly as **at-least-once with deduplication**,
because `start_conversation` has no idempotency key. See
`DISPATCH_RECOVERY_PROTOCOL.md`.

---

## 12. Security findings

**No critical or high defect in the audited Python scope.** Every handler gates;
public routes leak nothing; SQL injection was attacked and refuted; secrets are
redacted; SSRF guard observed refusing loopback. The Rust boundary is **BLOCKED**
— there is no Rust API. See `SECURITY_AUDIT.md`.

---

## 13. Performance comparison

| Workload | Python | Rust |
| --- | ---: | ---: |
| 200 tasks | 449.9 grants/s, p50 2.25 ms, 38.6 MiB | 481.4 grants/s, p50 2.01 ms, **3.1 MiB** |
| 500 tasks | 458.6 grants/s, p50 2.09 ms, 38.6 MiB | 459.8 grants/s, p50 2.13 ms, **3.1 MiB** |
| 400 tasks (earlier, same method) | 465.85 grants/s, p99 2.82 ms | 465.35 grants/s, p99 3.07 ms, 3.0 MiB |

**Throughput is a tie; Rust is ~12× lighter in memory.** The brief's 194.2 vs
170.3 tasks/s figures are **NOT REPRODUCIBLE** — no harness in the repository
produces "tasks/s". See `PERFORMANCE_REPORT.md`.

---

## 14. OpenHands integration status

| Item | Status |
| --- | --- |
| Python executor (app + direct transports) | **VERIFIED** in previous sessions; not re-exercised here (no key used) |
| Rust executor | **NOT IMPLEMENTED** |
| Live acceptance for Rust | **NOT ATTEMPTED** (mission forbids spending account resources) |

See `PHASE3_EXECUTOR_ARCHITECTURE.md` for the five polling rules a Rust executor
must reproduce.

---

## 15. SelfQA and evidence status

**VERIFIED** in earlier sessions (`tests/test_selfqa.py`, `test_evidence_trust.py`,
`test_lab_evidence.py` — 37 evidence tests pass in this audit). The distinguishing
rule holds: a textual agent answer is never proof; `completed_unverified` is
never reported as verified; a missing GitHub credential yields
`completed_unverified`, not a false success.

---

## 16. API migration readiness

**REJECTED.** The Python surface is 65 routes + 19 MCP tools + 7 client tools +
15 CLI commands; the Rust surface is **zero routes**. A matrix with no target is
not a migration plan. See `API_MIGRATION_MATRIX.md`.

---

## 17. CI status

The Rust job builds `--locked`, runs 30 tests, and now runs `fmt`/`clippy` with
`-D warnings`. The Python job uses PostgreSQL 17 and Chromium. **A green run on
`main` does not prove a candidate branch** — status must be quoted per SHA. The
audit branch's own CI is reported in the hand-over message, not assumed here.
See `CI_RELEASE_READINESS.md`.

---

## 18. Remaining blockers

1. **The Phase 2 candidate does not exist.** Nothing Phase-2-specific can be
   accepted until it is supplied (remote URL or PR number).
2. **No Rust worker / API / store.** Phases 1 (Rust), 3 (Rust), 6 (API) are
   blocked on code that was never written.
3. **The brief's performance figures have no source.**
4. **`cargo audit` / `cargo deny` has not been run** — dependency vulnerabilities
   are an open item.
5. **Cross-platform Rust build is unverified** (CI is Ubuntu only).
6. **Point C duplication risk** when the tag search fails.

---

## 19. Exact changed files

```
.github/workflows/tests.yml               (adds Rust fmt+clippy step)
pytest.ini                                (registers the `slow` marker)
rust/crates/nexus-domain/src/retry.rs     (clamp)
rust/crates/nexus-domain/tests/domain.rs  ((a..b).contains)
rust/crates/nexus-cli/src/main.rs         (cargo fmt)
tests/test_concurrent_claims.py           (new, 12 + 1 cases)
tests/test_sql_injection.py               (new, 1 case)
docs/nexus-3.0/FORENSIC_BASELINE.md       (new)
docs/nexus-3.0/PHASE2_ACCEPTANCE_REPORT.md (new)
docs/nexus-3.0/PARITY_MATRIX.md           (new)
docs/nexus-3.0/DISPATCH_RECOVERY_PROTOCOL.md (new)
docs/nexus-3.0/SECURITY_AUDIT.md          (new)
docs/nexus-3.0/PERFORMANCE_REPORT.md      (new)
docs/nexus-3.0/PHASE3_EXECUTOR_ARCHITECTURE.md (new)
docs/nexus-3.0/API_MIGRATION_MATRIX.md    (new)
docs/nexus-3.0/CI_RELEASE_READINESS.md    (new)
docs/nexus-3.0/MASTER_ACCEPTANCE_REPORT.md (this file)
```

`cargo fmt --all` may also have reformatted other Rust files; the diff is
formatting only and is listed in the PR.

---

## 20. Exact commits created

Recorded in the hand-over message once pushed (the audit branch
`nexus/3.0-audit`). No commit touches `main`, no branch is deleted, no history is
rewritten.

---

## 21. Recommended next action

1. **Owner: supply the real Phase 2 candidate** (remote URL or PR number). Without
   it, Phases 1 (Rust), 3 (Rust), 6 (API) and Gate A cannot proceed.
2. **Owner: confirm the baseline.** `main` is `e866666`, not `2edf3aa`.
3. **Then:** build `nexus-worker`/`nexus-api` against the contracts in
   `PHASE3_EXECUTOR_ARCHITECTURE.md`, and extend the parity suite to the 14
   blocked areas.
4. **Independently:** run `cargo audit`, and add the concurrency suite to CI.

### Gate verdicts

| Gate | Verdict |
| --- | --- |
| A — Phase 2 correctness | **BLOCKED** |
| B — Rust reliability | **NOT VERIFIED** (no Rust worker) |
| C — Python/Rust compatibility | **ACCEPTED WITH CONDITIONS** (11/30 areas) |
| D — Distributed execution safety | **ACCEPTED WITH CONDITIONS** (Python path only) |
| E — Security | **ACCEPTED WITH CONDITIONS** (Python scope only) |
| F — Phase 3 readiness | **BLOCKED** |
| G — Production migration readiness | **REJECTED** |

**No gate was granted by invention. No merge, deploy, or `main` change was made.**
