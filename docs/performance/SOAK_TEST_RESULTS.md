# NEXUS — Soak Test Results

> **STATUS: MEASURED.** A 60-minute soak of the real server under a steady read
> load with periodic bursts and controlled database faults (Stage 4 of the
> independent acceptance).
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

- **Commit:** `00819e6` (after the audit fixes; branch
  `engineering/performance-evolution`)
- **Environment:** PostgreSQL 17.11, Python 3.13.15, isolated database
  `nexus_soak`, 4 vCPU / 16 GB. No production contact.
- **Instrument:** `benchmarks/soak_test.py`
- **Shape:** 8 concurrent readers; every 3rd sample a 200-member zip upload;
  every 5th sample all database connections dropped from the server side (a
  controlled outage), then recovery.

---

## 1. Result (MEASURED)

| Metric | Value |
|---|---|
| Duration | 60 min |
| Total reads | **2,282,863** |
| Read errors | 136 (all at the moments connections were dropped — expected) |
| Controlled DB faults | 35 |
| DB connections | **constant 9** (pool max 8 + the sampler) |
| Threads | 23–29 |
| File descriptors | 27–34 |
| Pool `waiting` (max) | 1 |
| Read latency p50 / p95 / p99 | **12.3 / 17.3 / 20.3 ms** |

**No unbounded growth in threads, file descriptors or database connections.**
The database connection count never moved from 9; the pool never queued more
than one waiter. The system recovered from all 35 controlled outages by itself
(the 136 errors are the in-flight requests at the instant of a drop, which is
what a real database restart does to any server).

## 2. Memory — a metric artefact, resolved (VERIFIED)

The soak script first reported RSS rising 71.9 → 162.1 MB. That number is
`ru_maxrss`, the **peak**, which only ever grows and therefore cannot show a
leak. A separate probe read **current** RSS (`VmRSS`) under the same workload:

| Workload | Current RSS start → end | Verdict |
|---|---|---|
| 8 readers, 6 min | 70.9 → 71.0 MB | flat |
| 8 readers + periodic uploads, 9.5 min | 71.5 → 72.0 MB | flat (plateau after ~400 s) |

**The process is steady; there is no memory leak.** The soak script is fixed to
report current RSS (`VmRSS`), so a future run reports the real figure.

## 3. What the soak proves

- **No connection leak:** the pool count is constant across 2.28M reads.
- **No descriptor or thread leak:** both oscillate in a fixed band.
- **Recovery:** 35 forced outages, each recovered automatically; the next read
  succeeds.
- **Latency stays low under sustained load:** p99 20.3 ms over an hour.

## 4. Limitations (UNKNOWN / BLOCKED)

- The 136 read errors are counted, not hidden; they are the in-flight requests
  at a forced drop, not a defect. A production deployment would see the same
  from a database restart.
- A multi-hour (>60 min) soak was not run; the 60-minute run is flat and the
  memory probes show a plateau, but a multi-day soak is out of scope here.
- Real OpenHands/LLM latency is not exercised (stand-in services only).
