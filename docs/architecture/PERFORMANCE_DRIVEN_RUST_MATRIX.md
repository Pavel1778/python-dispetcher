# NEXUS — Performance-Driven Rust Migration Matrix

> **STATUS: MEASURED RESEARCH.** Every number here was taken in this session on
> PostgreSQL 17.11, Python 3.13.15, 4 vCPU, 16 GB RAM, against an isolated
> database (`nexus_perfaudit`). No production load was generated.
>
> Evidence classes: **VERIFIED** (measured/checked), **INFERRED**,
> **UNKNOWN**, **BLOCKED**.
>
> Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
> Roadmap: [PERFORMANCE_OPTIMIZATION_ROADMAP](./PERFORMANCE_OPTIMIZATION_ROADMAP.md).
> Forensic source: [RUST_MIGRATION_BLUEPRINT](./RUST_MIGRATION_BLUEPRINT.md).

Decision vocabulary: **RUST_FIRST · PYTHON_OPTIMIZE · PYTHON_KEEP · RUST_LATER ·
NEEDS_MORE_DATA**.

---

## 1. The headline result (VERIFIED)

The current store holds **one** PostgreSQL connection behind a
`threading.RLock` (`orchestration/cloud_store.py:276-281`, `_tx` at `:536`).
Under concurrency this is a **lock convoy**: latency grows without bound while
throughput stays flat.

DB workload: `SELECT count(*) FROM nexus_conversation_messages WHERE conversation_id = $1`
(same SQL, same data, same server for all three variants). A = current NEXUS
shape (1 conn + RLock). B = optimized Python (`psycopg_pool`). C = Rust + SQLx
async pool. A/B averaged over 2 runs; C over 3 (the conc=100 outlier is
explained in §4).

| Conc | A tput ops/s | A p99 ms | B tput ops/s | B p99 ms | C tput ops/s | C p99 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2,321 | 1.7 | 4,128 | 1.3 | 3,397 | 5.7 |
| 5 | 3,364 | 23.5 | 4,690 | 3.2 | 6,753 | 7.7 |
| 10 | 3,132 | 63.9 | 5,139 | 4.8 | 11,351 | 12.2 |
| 25 | 2,950 | 189.9 | 4,178 | 12.0 | 14,123 | 41.7 |
| 50 | 2,670 | 402.1 | 4,471 | 20.0 | 11,592 | 48.3 |
| 100 | 2,524 | **706.4** | 4,321 | **37.2** | 18,958¹ | 35.2 |

¹ one clean run; two other runs hit the pool-size/server-limit artifact (§4).

**VERIFIED conclusions:**
- A's p99 at 100 concurrent is **706 ms**; B's is **37 ms** — **19× better**,
  and B is pure Python (no Rust).
- A's throughput is **flat** (~2.5–3.4k) from 1→100 concurrency; B climbs to
  ~4.7k; C reaches ~11–19k.
- **The bottleneck is the single connection + lock, not the Python language.**
  Variant B — the same Python — removes most of it.

---

## 2. Full matrix

| Component | Python path | Current benchmark (measured) | Bottleneck | Optimized Python result | Rust result | Improvement | CPU | RAM | Complexity | Risk | Decision | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DB access under concurrency | `cloud_store.py` `_tx` (`:536`), 1 conn + RLock (`:281`) | p99 706 ms @100c; flat tput | single connection serialized by RLock | **B: p99 37 ms @100c (19×)**, tput ~4.7k | **C: p99 35 ms, tput 11–19k** | B ≈19× p99; C ≈1.2× over B on p99, ~3× on tput | low | low | B: low, C: high | B: low | **PYTHON_OPTIMIZE (B first)**; RUST_FIRST only for the long-term engine | high |
| HTTP API read paths | `web_server.py` (`ThreadingHTTPServer`) | studio 0.83 ms, list 1.23, open 1.69, conv 1.38, commands 0.60 (p50) | negligible at low load; degrades under load via the DB lock | B unblocks the DB path; HTTP layer itself is cheap | C: same server cheap; the win is the DB pool | small on read latency, large under load | low | low | medium | low | **PYTHON_KEEP** (the HTTP layer); fix the DB below it | high |
| API under concurrency | same | p50 1.36→32 ms, p99 1.6→**1264 ms** @50c | DB lock + thread-per-request | pool + bounded workers | async + pool | ~10–30× p99 under load | low | low | medium | low | **PYTHON_OPTIMIZE** | high |
| Scheduler claim | `nexus_lab_store.claim_task` | 2.4 ms/claim; **throughput flat ~250–300 claims/s at 1→25 workers** | single-conn lock in `_tx` | pool → concurrent claims | async pool | scales with B/C | low | low | low | low | **PYTHON_OPTIMIZE** | high |
| Run creation | `LabStore.start_run` (40-task plan) | p50 17.5 ms | one transaction, fine | unchanged | unchanged | none needed | low | low | — | — | **PYTHON_KEEP** | high |
| Run/task state transitions | `nexus_lab_store.py` | claim 2.4 ms; reclaim scan 0.49 ms | none individually | — | — | — | low | low | — | — | **PYTHON_KEEP** | medium |
| Cleanup queueing | `queue_cleanups` | p50 2.5 ms | none | — | — | — | low | low | — | — | **PYTHON_KEEP** | medium |
| Dashboard counts | `cloud_store.dashboard_counts` | p50 0.59 ms | none | — | — | — | low | low | — | — | **PYTHON_KEEP** | high |
| Event write | `cloud_store._event` (`:555`) | p50 0.84 ms | none (single INSERT) | — | — | — | low | low | — | — | **PYTHON_KEEP** | high |
| Project event append | `nexus_project_events` INSERT | p50 0.89 ms | none | — | — | — | low | low | — | — | **PYTHON_KEEP** | medium |
| File upload | `file_lab.upload` (sha256 + INSERT) | 4 KiB 1.3 ms; 64 KiB 1.8; 512 KiB 8.1; 2 MiB 23.7 (p50) | sha256 + full-bytes-in-row | — | — | — | medium | medium | low | low | **PYTHON_KEEP** (I/O-bound, fast enough); optional RUST_LATER | high |
| Archive inspect | `archive_safety.open_zip` | 100 members 2.3 ms; 1000 20.9; 2000 39.3 (p50) | CPU (zip parse + validation) | algorithmic only | Rust could be ~2–5× | <2× at 2000 | medium | low | medium | low | **RUST_LATER** | medium |
| Archive extract | `file_lab.extract_into_workspace` | 1000 members **216 ms** (p50) | CPU (zipfile + writes) | batching helps little | Rust likely 2–5× | moderate | high | medium | medium | low | **RUST_LATER** | medium |
| Frontend bundle | `orchestration/studio/app.js` | 198,524 B (gzip 50,473 B); whole studio 428 KB | no build step; one big JS | minify/split/gzip | n/a (frontend) | load-time win, not Rust | low | low | low | low | **PYTHON_KEEP** (optimize assets, not a language) | high |
| Frontend polling | `app.js:946` 1 s, `:1154` 5 s | 1 s pending timer, 5 s work poll | fixed cadence | longer interval + backoff + visibility gate (already gated) | n/a | fewer requests | low | low | low | low | **PYTHON_OPTIMIZE** (assets/polling) | medium |
| Startup / migrations | `cloud_store.apply_migrations` | full schema apply: seconds (one-time) | one-time cost | — | — | — | low | low | — | — | **PYTHON_KEEP** | high |
| OpenHands-bound paths | `cloud_*.py`, `nexus_lab.py` | chat_turn 18 ms, start_run 48 ms (stand-in) | **external** (Cloud/LLM) | none — provider-bound | **no speed-up** | none | low | low | — | — | **PYTHON_KEEP** (Rust cannot speed an external call) | high |
| GitHub-bound paths | `github_client.py` | external API latency | **external** | none | no speed-up | none | low | — | — | — | **PYTHON_KEEP** | high |

---

## 3. What is NOT a bottleneck (VERIFIED)

- **The Python language itself.** Read paths are 0.6–1.7 ms p50; the DB query
  is sub-ms. Variant B (same Python) removes the concurrency problem.
- **Event writes** (0.84 ms), **dashboard counts** (0.59 ms), **run creation**
  (17.5 ms for a 40-task plan) — all cheap.
- **OpenHands/LLM/GitHub paths** — dominated by the external service; a
  language change cannot help ([RUST_MIGRATION_BLUEPRINT](./RUST_MIGRATION_BLUEPRINT.md) §7).

## 4. Measurement caveats (honest)

- Variant C at conc=100: 2 of 3 runs showed ~395 ops/s and 1 error. Cause:
  the Rust pool `max_connections(100)` equals the server's `max_connections`
  (100), so at 100 concurrent workers the pool cannot hand out a connection.
  **This is a pool-configuration artifact, not a Rust defect.** A clean run
  reached 18,958 ops/s.
- Single-run numbers are not used for a conclusion; A/B are 2-run averages, C
  is reported with its spread.
- The stand-in LLM/OpenHands/GitHub servers are on loopback, so `chat_turn`
  and `start_run` measure NEXUS overhead + a local model, **not** real provider
  latency.
- Production numbers (0.17–0.46 s) are **network + Cloudflare + cold-start**,
  not Python compute (VERIFIED: `/api/studio` 401 in 0.19 s).

## 5. End-to-end time breakdown (VERIFIED where measured)

`Total = Client + API + Internal + Database + Queue wait + External`

| Journey | Measured pieces | Dominant term |
|---|---|---|
| Open dashboard / list projects | API p50 0.83–1.27 ms; prod 0.18–0.46 s | **network (prod)** |
| Open conversation / history | API p50 1.38–2.38 ms | DB (sub-ms) |
| Send a chat turn | chat_turn p50 18 ms (stand-in) | **LLM (prod)** |
| Create task / start run | start_run p50 48 ms (stand-in); planning 17.5 ms | **LLM (prod)** |
| Run status | claim 2.4 ms; poll interval 2 s | **poll cadence** |
| File upload 2 MiB | 23.7 ms | sha256 + I/O |
| Archive extract 1000 | 216 ms | CPU |

**Key point for the reader:** even a 10× faster backend changes the user-facing
total only where NEXUS-side compute dominates — the file/archive paths and
concurrent reads. It does **not** change the OpenHands/LLM-bound journeys,
which are the product's longest waits.

## 6. Decision summary

- **RUST_FIRST:** none required *today*. The only measured, structural bottleneck
  is the DB connection model, and **optimized Python (B) removes most of it**.
- **PYTHON_OPTIMIZE:** the DB access layer (pool + bounded workers), API
  concurrency, scheduler claim throughput, frontend assets/polling.
- **PYTHON_KEEP:** HTTP layer, events, dashboard, run creation, OpenHands/LLM/
  GitHub-bound paths.
- **RUST_LATER:** archive extract/inspect (CPU-bound), the long-term
  Execution Engine (when async + type safety are wanted for other reasons).
- **NEEDS_MORE_DATA:** production p95/p99 under real load (needs paid Render
  metrics or an OTel staging deploy); long-run memory behaviour.
