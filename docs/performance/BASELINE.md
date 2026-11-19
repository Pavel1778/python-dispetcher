# Performance Baseline — NEXUS (2026-11-19, branch `arena/aa1abcbc-python-dispetcher`)

Measured, reproducible numbers. Hardware: 2-CPU cloud sandbox, 7.6 GB RAM,
PostgreSQL 16.2 (local). CI runs PostgreSQL 17. No Docker in this environment.
The app is **I/O-bound** (HTTP ↔ PostgreSQL); CPU is almost never the wall.

## Startup & footprint (measured)

| Metric | Value | How |
|---|---|---|
| Cold import `orchestration.web_server` | ~0.39–0.45 s | `python -X importtime -c "import orchestration.web_server"` (3 runs) |
| Idle VmRSS of the server (empty DB, `nexus_smoke2`) | **~52 MB** | `grep VmRSS /proc/<pid>/status`, stable over repeated reads |
| Threads (inline lab + HTTP pool + loops) | ~9 | `ps -o nlwp` |
| Boot → "listening" | within seconds of process start (migrations were already applied; first connect applies them otherwise) | smoke log |

## Request & DB latency

| Metric | Value | Source |
|---|---|---|
| `GET /healthz` | 200, sub-ms server-side (no DB) | smoke |
| `GET /readyz` | 200, one `SELECT 1`-class round-trip | smoke |
| Claim path (`_BRANCH_FREE` probes), before `0041` | Q1 active probe **1.40 ms**; Q2 abandoned probe **11.51 ms** (seq scan) | `EXPLAIN (ANALYZE, BUFFERS)`, 60k-task scratch cluster |
| Claim path, after `0041` | Q1 **0.037 ms**; Q2 **0.008 ms**; Q3 ≈ 0.004–0.006 ms | same harness, post-index |
| JSON parse, stdlib `json`, 1.3 MB / 1200 objects | **0.83 ms** | quick benchmark here |
| JSON parse, `orjson` (3.13.0, measured earlier) | ~2.8–2.9× faster (9.2 → 3.2 ms on a trajectory-like payload) | recorded in `docs/PERFORMANCE.md`; **not adopted** (stdlib-only policy, canonical evidence byte-identity) |
| sha256, 256 KB | 0.212 ms | hashlib (OpenSSL) — already C |
| Full test suite (fresh DB) | ~40 min, 1974+ tests | `/home/user/full_suite_arena*.log` |

## Database

- Fresh DB: 42 migrations → 51 public tables; `apply_migrations` idempotent;
  changing an applied file's body is a hard refusal (`MigrationError`) unless a
  registered correction exists.
- Claim transactions use row locks (`FOR UPDATE`) with a 90 s lease (planner
  claims 3 s); concurrent claims never double-take (`test_concurrent_claims_never_take_the_same_task`).
- Pool: psycopg_pool with per-connection health check (`_pool_check_connection`).

## Frontend budget (not locally measurable — CI-only Chromium)

Studio is served as static assets with pre-compressed gzip bodies (`web_ui.STATIC_GZIP`);
CSP `script-src 'self'`; browser E2E passes in CI at `eff7cd2`.

## What "faster" means here

The single biggest measured win of this increment is the `0041` index set
(seq scan → index-only-ish bitmap heap reads on the claim path). JSON-tokenising
is 0.83 ms for 1.2k objects — Amdahl's law leaves nothing on the table for a
Rust JSON component. Hashing is OpenSSL. Therefore: **no Rust component is
adopted** (see `docs/RUST_MIGRATION_ASSESSMENT.md`, 2026-11-18 re-validation).
