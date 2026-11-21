# NEXUS 3.0 candidate — review handoff

**Decision: review as a draft, not a release or a Rust cutover.** Base `main@20eb8bffc3e89f08e24522aa98dba0e62fc6e070`; product tested at `c6b1a9931ec86df66f018e30f48dac9a81a280b2` (tree `daf440b3cb7fb3db587ad3a0e38d66636d121ff2`). The final evidence commit adds only this report and evidence. All database and runtime drills used disposable local services. No hosted accounts, deployments or production data were changed.

| Gate | Result and boundary |
|---|---|
| A. Source lineage | #54 and #55 are in the base. [Reachable refs and crate inventory](evidence/inventory.json); no recoverable `nexus/3.0-foundation`, `9c11f52` or `5a9989b`. Not a reconstruction of missing code. |
| B. Authority | [Architecture decision](ARCHITECTURE_RECONCILIATION.md): Python/PostgreSQL own grants, dispatch, verification and the API. `nexus-storage` is the single Rust storage candidate, not a second production writer. |
| C. Rust decisions | 230 distinct state pairs, zero disagreements; the deliberate run self-loop mutant disagreed on 7. 49 run pairs and 100 SQLite task pairs compare against actual persisted states; 81 workflow pairs compare against the Python validator. Not 230 migrated operations. |
| D. Rust build/storage | [Exact commands and results](evidence/rust.json). Original formatting passed. Original locked workspace build, Clippy and tests blocked in this environment by missing registry packages; the three-crate path-patched subset passed 30 Rust tests and Clippy. No new local SQLx/PG result. Do not run the storage probe on an execution queue. |
| E. Schema and restore | [PG 16.2 upgrade drill](evidence/upgrade.json): one new migration/table, existing catalog and live grant preserved; new table RLS, no grants for `anon`/`authenticated`; second apply empty; old reader and restore comparison passed. Not mixed-worker downgrade approval. |
| F. Dispatch recovery | [Actual upstream lost-reply drill](evidence/upstream_lost_reply.json): one accepted create, kill before ACK, failed first read, two replacements, `sending → uncertain → bound`, fences `1 → 2 → 2`, one execution and one iteration. Compares against the original launch despite a new desired revision. No permission to resend when proof is unavailable. |
| G. Profile scope | Deployment/project/pending-task precedence, connection pinning, revision fencing, required-profile mode. Unusable configuration blocks create; it does not silently choose a new model. |
| H. Native sessions | [Readback](evidence/upstream_sessions.json) from actual PyPI agent-server, SDK and tools 1.60.1. New web, worker and agent processes; native credential rotated in place; prior profile restored, revision 2 used for a new session and revision 1 retained by the first. Worker checked two operator-staged fixture checkouts. No model key in the evidence. |
| I. External execution | Positive paid-model inference, live GitHub mutation, managed hosted Direct and multi-account live failover **not established**. Managed Cloud App is refused. Local workspace checking is not provisioning or OS isolation. |
| J. Self-QA and signing | Focused test ran a real check at a test commit, signed its bound receipt and independently re-verified it. A policy skip is not a passed check. Untrusted arbitrary code must not run on the control-plane host. |
| K. Reviewer surface | Optional expiring project-bound verification bearer only on `POST /mcp`. Focused tests exercise membership revocation, denied write/foreign project, expected-commit mismatch and read-only verification. Not an independent human review. |
| L. Wire/auth | [Wire comparison](evidence/compatibility.json): nine existing HTTP cases equal; four stdio replies match HTTP. MCP tools/list adds `get_execution_profile`; no old schema removed. Legacy owner bearer is **not** the scoped grant. |
| M. Studio | Nine existing browser views had identical pixels and PNG hashes at base and candidate; `app.js` changed. This comparison covers the reference views, not the new expanded form. Three viewport tests separately exercise actual save and refused sync. Not native-device acceptance. |
| N. Performance | [All trials](evidence/performance.json). Same 2-CPU sandbox, PG 16.2, no other acceptance workload during timing. No general speedup claim; see below. |
| O. Release validation | 30 pre-PR targeted tests passed on the product SHA, including signed QA, rate-limit reasons, Studio static guards, native sessions and scoped reviewer. Ruff and JS syntax passed. The full local rerun had **not finished at PR handoff**. Historical CI on #54/#55 is not CI on this candidate. Await exact-head CI, especially the locked Rust/SQLx job. |

## What the measurements say

Three 100-task serial fixture runs per binary (actual worker, DB, HTTP socket doubles, 101 signatures checked each). Median throughput **30.29 → 29.56 tasks/s**; claim-to-task-signature p50/p95/p99 **30.329/40.567/50.282 → 31.672/40.336/53.255 ms**. Worker RSS **52,404 → 52,652 KiB**, CPU user + system **1.3047 → 1.3370 s**; SQL execute calls **5,244 → 5,344**, transactions **2,528 → 2,628**. In a separate 2-task SIGKILL DAG (three trials), median relaunch-to-child bind **306.67 → 318.90 ms**, request-to-signed-acceptance **0.9353 → 1.0045 s**. No model/remote latency or project QA build in these measurements.

Median successful grants/s at widths 1/4/16/64: base **289/330/281/248**; candidate **252/292/282/256**. These are 64 claims in one run at each width, active-task cap disabled, not production parallel model sessions. The corresponding authenticated project read at width 64 had candidate median p95 **1.44 s** (base **1.45 s**). Pool and tail measurements are included in the raw trials. This host is not a 4 CPU / 8 GB deployment.

## Reproduce safely

[Operator contract, limits and rollback](OPERATIONS.md). Use `tools/with_disposable_postgres.sh`, not a shared DSN. Install Python test dependencies and Chromium; build the Rust workspace using `.github/workflows/tests.yml` with registry access. Pass `NEXUS_RUST_BIN`, `NEXUS_EDGE_PROBE`, `NEXUS_CLAIM_PROBE`, `NEXUS_RUN_128=1` and `NEXUS_TEST_AGENT_PYTHON` (an interpreter with agent-server 1.60.1) **inside** the wrapper to run all available gates. For the native relay drill:

```sh
bash tools/with_disposable_postgres.sh python docs/nexus-3.0/evidence/upstream_lost_reply.py \
  /absolute/path/to/agent-python /tmp/new-lost-reply-result.json
```

The subprocesses and relay bind only loopback. The relay counts actual create responses, withholds the first, then refuses a read during takeover. It does not implement a fake agent-server. Only summary measurements are retained: no native volumes, credentials, raw provider responses or database dumps.
