# AUTONOMOUS EXECUTION ACCEPTANCE — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED WITH CONDITIONS.** The full pipeline (prompt → planner → DAG
→ scheduler → OpenHands → QA → evidence → acceptance → notification) is proven
end-to-end against real HTTP + real PostgreSQL + the fakes, and the live pieces
(agent execution, push, real GitVerifier → `completed_verified`) are each proven
against the live services. The one composition not yet run *live* is the
scheduler driving several tasks of one run on a live repository — that path is
proven on the fakes and is the recommended next live drill.

---

## 1. The pipeline, stage by stage

`tests/test_pipeline_e2e.py` drives the whole chain over real HTTP and real
PostgreSQL and asserts each stage's *effect*:

| Stage | Assertion |
| --- | --- |
| PLANNER → DAG | a dependency gates the dependent; the DAG is validated |
| SCHEDULER | claims only dependency-satisfied tasks; leases + fencing |
| OPENHANDS | a conversation is started with the task text (byte-for-byte for long specs) |
| EXECUTION | a text answer naming no commit is `completed_unverified` and the dependent is not started |
| QA / SelfQA | the project's checks run at the accepted commit; a failure downgrades |
| EVIDENCE | a signed record exists and re-verifies |
| ACCEPTANCE | `ACCEPTED` only when every task is `completed_verified` |
| NOTIFICATION | the completion message appears only after the run ends |

It also runs the same loop through the real `LabWorker` with `NEXUS_LAB_INLINE=0`,
proving the server accepts/plans while a separate worker advances.

## 2. Task states the orchestrator distinguishes

`completed` · `completed_verified` · `completed_unverified` ·
`completed_no_changes` · `blocked` · `failed` · `waiting` · `cancelled`, plus
`approval` gates for dangerous operations (`nexus_actions` confirmation
handshake). A run completes only when every task is `completed_verified`; an
unverified dependency blocks the run rather than spinning.

## 3. The agent cannot verify its own work

Proven live (`OPENHANDS_LIVE_EXECUTION.md` §7): a report that names no commit, or
a wrong commit, or an errored conversation never becomes `completed_verified`.
Only the real `GitVerifier`, reading the branch head from GitHub and matching it
to the reported commit **and** confirming the branch moved, yields
`completed_verified`.

## 4. Autonomous self-checking

OpenHands can invoke NEXUS self-check tools through the authorized surfaces:

- **Sr client tools** (`SrTools.tool_schemas()`): create/inspect/accept/reject
  child tasks — project-scoped, `workflow_id` substituted by the bridge.
- **MCP tools** (20, each with a scope): `list_evidence`, `verify_evidence`,
  `list_run_task_outputs`, etc. — no shell, SQL or eval; membership re-checked.
- **CLI** (62 commands) and the `qa`/`evidence` verbs.

None expose unrestricted host filesystem or administrative operations.

## 5. The live proof that exists

| Piece | Live? |
| --- | --- |
| Agent executes a task on the configured model | **yes** (`READY` conversation) |
| Agent clones, edits, tests, commits, pushes | **yes** (`lab-fix` @ `9afb666`) |
| Real `GitVerifier` → `completed_verified` | **yes** (remote_sha == named_sha, branch moved) |
| Negative controls (no/wrong commit, error) | **yes** |
| Scheduler driving several tasks of one run on a live repo | **not yet** — proven on fakes; next drill |

## 6. Condition

The remaining live drill — a multi-task run through `LabScheduler` on a
disposable repository — is the one composition not yet exercised against the
live services. Every constituent is verified; only their live composition
through the scheduler is outstanding, and it is covered by the fake+PostgreSQL
pipeline test. This is why the verdict is VERIFIED WITH CONDITIONS rather than
VERIFIED.
