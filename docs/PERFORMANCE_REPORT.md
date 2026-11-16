# NEXUS — Performance Report (restored 2026-11-16)

Phase G. **This document deliberately carries no new benchmark numbers.**
Honesty over completeness:

- The 2026-11-15 re-measure (list_projects 1.24 ms, open_project 1.48,
  fetch_history 2.20, chat_turn 18.53, start_run 33.40, queue_claim 2.15;
  Python 3.11.2, PostgreSQL 16.2, `nexus_bench` DB, repeat 10 / 3 projects
  / 10 conversations / 40 msgs) was taken against the **pre-consolidation**
  tree whose store used a different connection strategy (no pool, then).
  Quoting those numbers for this base would misrepresent the pool-based
  architecture; the baseline does not transfer.
- This branch has not yet executed `tools/benchmark.py` against the
  consolidated tree; when it does, the result must be a **same-interpreter,
  same-dataset** comparison before/after — the comparison instrument is
  `tools/benchmark_compare.py`. Until then: **no perf claim**.
- The consolidation rewrote `docs/PERFORMANCE.md` with its own controlled
  before/after pair (precompressed static assets; served bytes byte-identical,
  proven by decompress assertion) and an explicit “no language migration”
  verdict with the evidence. That document — not this one — is the current
  performance baseline for this base; read it instead of the 2026-11-15
  appendix, which was not carried into it.
- CI verifies a fresh PostgreSQL run each merge; that is functional
  verification, not profiling.

## Environment this report covers

- Python 3.11.2, local PG via `pgserver` (PG 16.2, socket `/home/user/pgdata-nexus`),
  DSN via `TEST_SUPABASE_DB_URL`; `tests/_test_db.py` gives every pytest
  process its own schema (pre-existing mechanism, unchanged).

## Needed to make a real claim

1. Seed `nexus_bench` in **this** tree (`python tools/benchmark.py --out …`).
2. `python tools/benchmark_compare.py` vs a pinned baseline JSON.
3. Report p50/p95/p99 + dataset + interpreter + DB versions, and say when a
   comparison is cross-interpreter (then it is a band, not a delta).
4. Terminal-outputs list path: still **not benchmarked** — first-class
   candidate for the next benchmark addition.

## Verdict

Read paths are expected to remain single-digit ms with the pool layer
(fewer connection setups, not magic); this is an **expectation, not a
measurement**, and is labelled as such.
