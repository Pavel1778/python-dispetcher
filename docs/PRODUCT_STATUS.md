# Product status

What NEXUS does today, what it does not, and how each claim is checked. Status
words are the ones the tests and diagnostics use, not aspirations.

Legend: **VERIFIED** (proven by a test or a real run) · **IMPLEMENTED** (real
code, exercised against the local stand-ins) · **PARTIAL** · **BLOCKED**
(external dependency) · **NOT IMPLEMENTED**.

## The development loop

| Capability | Status | Evidence |
|---|---|---|
| Create a project, connect a GitHub repository, bind a branch | VERIFIED | `tests/test_studio_api.py`, `tests/test_nexus_projects.py` |
| Chat with intent routing (a question never starts a run) | VERIFIED | `tests/test_intent_router.py` |
| Plan a task into a validated DAG | VERIFIED | `tests/test_planner_worker.py`, `test_long_task.py` |
| Dispatch to OpenHands, poll, verify against the real branch | VERIFIED | `tests/test_lab_loop.py`, `test_pipeline_e2e.py`; live run recorded in `NEXUS_2_0_FINAL_ACCEPTANCE_REPORT.md` |
| Restart / duplicate a run; cancel; retry failed tasks | VERIFIED | `tests/test_run_history.py`, `test_cancel_lock_order.py` |
| Project isolation (404, never 403, at every boundary) | VERIFIED | `tests/test_project_isolation.py`, `test_project_isolation_routes.py` |
| Evidence Trust Engine (Ed25519, re-verifiable) | VERIFIED | `tests/test_evidence_trust.py`, `test_lab_evidence.py` |
| Self-QA engine (isolated checkout, real exit codes) | VERIFIED | `tests/test_selfqa.py` |
| **Self-QA as an automatic run gate** | VERIFIED | `tests/test_quality_gate.py`, `test_quality_gate_policy.py`; per project (`auto`/`on`/`off`, default `auto`), `NEXUS_SELFQA_GATE` overrides deployment-wide |
| Multi-account pool, failover, load balancing | IMPLEMENTED | `tests/test_openhands_pool.py`, `test_openhands_failover.py` |
| Two-live-account failover | BLOCKED | one OpenHands key available |
| Bounded autonomous replanning (new corrective tasks) | PARTIAL | a durable run reworks a task; it does not yet invent new tasks |

## Interfaces

| Interface | Status |
|---|---|
| Adaptive web Studio (desktop → phone) | VERIFIED — `tests/test_browser_e2e.py` at six widths |
| CLI (`nexus …`) | VERIFIED — `tests/test_cli_mcp.py` |
| MCP server (stdio + Streamable HTTP) | VERIFIED — `tests/test_cli_mcp.py` |
| PWA | VERIFIED (served, installable) |
| Debian `.deb` | VERIFIED by `dpkg-deb` inspection and a real install |
| Android `.apk` | IMPLEMENTED — builds and passes `apksigner`; **not** runtime-tested (no emulator) |
| Windows `.exe` | IMPLEMENTED — builds; PE/`.rsrc` inspected; **not** runtime-tested (no Windows host) |

## Rust core

The pure decisions (state machines, DAG validation, plan hash, retry
arithmetic, the claim decision) are implemented in Rust with a proven parity
contract against Python (`tests/test_rust_parity.py`, mutation-tested). **Rust
is not in the request path; Python is authoritative.** See `docs/RUST_MIGRATION.md`.

## Self-hosting

| Capability | Status | Evidence |
|---|---|---|
| One command to run/stop/restart/status/logs | VERIFIED | `scripts/nexus.sh`, `tests/test_self_hosting.py` |
| Backup (pg_dump + SHA-256) and restore into a scratch DB | VERIFIED | `tests/test_self_hosting.py` (real round-trip) |
| systemd units | IMPLEMENTED | `packaging/systemd/` |
| Temporary public URL (Cloudflare quick tunnel) | IMPLEMENTED | `docs/CLOUDFLARE_TUNNEL.md`; requires `cloudflared` |
| Named tunnel (stable hostname) | IMPLEMENTED | `docs/CLOUDFLARE_TUNNEL.md`; needs a Cloudflare account + domain |

## External blockers (not defects)

- A second live OpenHands key (two-account failover).
- A GitHub provider token on the live OpenHands account (a live task that must
  push ends `completed_unverified`; correct behaviour).
- An Android emulator and a Windows host (runtime testing of those packages).
- Owner authorisation to merge, tag or deploy.

Keep this table in step with `docs/KNOWN_LIMITATIONS.md` and the diagnostics.
