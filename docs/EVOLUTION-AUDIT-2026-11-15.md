# NEXUS — Evolution Audit (2026-11-15)

Branch `arena/b10d2a83-python-dispetcher`, base `28aa026` (PR #42 merge).
This audit is the state of one snapshot, not a blanket certification.
Full suite on this branch: **1915 passed / 49 skipped / 0 failed**, run twice —
38m34s at `fc026be`, and **36m54s at `9cc5c4b`** (after the studio edit,
`EXIT=0`, `/home/user/pytest_full2.log`).

## Where NEXUS stands on the EVOLUTION axis

1. **Audit** — substantially done: SECURITY-AUDIT-2026-11-14, GROUND-TRUTH
   build/lint/test doc, MASTER_COMPLETION_* series, this audit, REPORT A/B.
   Consolidated gap list below.
2. **Debt closure** — closed this branch: terminal-output recovery end to end
   (tests 45/45), pause-semantics honesty in the UI, perf re-measure, DB test
   isolation (per-process DBs / flock, landed before PR #42 merge).
3. **Run-time acceleration** — NOT started, deliberately. The wall-clock of a
   run is agent/LLM latency; NEXUS-side paths are single-digit ms. Levers that
   exist but are unmeasured: worker poll interval, lease renewal cadence, event
   page sizes, conversation batching. No acceleration is claimed.
4. **Autonomous AI Development Laboratory** — NOT reached. The lab loop exists
   (durable runs, scheduler, guards, GitVerifier); the *autonomous verification*
   layer (Self-QA) is design-only.

## Open debts / gaps (prioritised)

| # | Item | Severity | State after this branch |
| --- | --- | --- | --- |
| 1 | Self-QA harness (build/lint/test per generated project) | High — this is the missing leg of autonomy | design doc exists; unimplemented |
| 2 | Browser/mobile E2E (incl. outputs panel, pause copy) | Medium | static-only pass done; needs CI/device |
| 3 | Duplicate migration prefix `0038_*.sql` | Low (inert with current runner) | verified both apply on fresh DB; runner is filename-keyed + checksummed; no rename without runner audit |
| 4 | Benchmark the terminal-outputs list path | Low | not in `tools/benchmark.py` yet |
| 5 | Run-time acceleration | Low (unproven need) | requires a measured bottleneck first |
| 6 | Live OpenHands round (credentials) | Medium | NOT done; nothing live verified |
| 7 | CI gating (push/PR → suite) | Medium | not configured; merge safety rests on manual runs |
| 8 | Rust rewrite | — | rejected for now (no full rewrite); at most targeted PyO3 after separate justification |
| 9 | `.exe`/Android build verification | Medium | packaging files exist; not built here |

## What is already solid

- Durable runs (crash/lease/reclaim/cycle budget) — tested vs fakes.
- Project-scoped isolation, SSRF policy on outbound calls, path lexically
  validated everywhere (terminal-output path recomputed from event content).
- Test DB isolation; 1915-test suite green; no assertions weakened.
- Honest pause semantics (UI no longer implies pause stops running
  conversations).

## Honest bottom line

NEXUS is a strong, tested single-tenant agent orchestrator with a working lab
loop and excellent evidence discipline. It is **not yet** an autonomous
development laboratory: the missing capability is autonomous verification
(#1) and a live-verified round (#6). Any claim beyond that would be marketing,
not engineering.
