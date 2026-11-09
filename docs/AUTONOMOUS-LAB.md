# NEXUS Autonomous Lab — delivery report

This report states what is implemented, tested, live-verified and *not* done in
the `nexus/autonomous-completion` line. Status labels are used precisely:

**IMPLEMENTED** · **UNIT-TESTED** · **INTEGRATION-TESTED** · **LIVE-VERIFIED** ·
**BUILT (not runtime-tested)** · **NOT IMPLEMENTED** · **BLOCKED**

An agent's own text is never evidence. "LIVE-VERIFIED" means a real request to a
real external service was made and its result observed.

---

## Git

- Starting commit (verified): `556734732eddeb6029c394aeabf28480092ac744` (`main`).
- Working branch: `nexus/autonomous-completion`.
- Preservation tags kept (13). No history rewritten, no force-push.

## Autonomous Lab

| Component | Status | Where |
|---|---|---|
| Goal lifecycle (draft…cancelled) | **INTEGRATION-TESTED** | `migrations/0018`, `nexus_lab_store.py` |
| Run + run-task store, one active run per goal | **INTEGRATION-TESTED** | `nexus_lab_store.py`, `tests/test_lab_store.py` |
| Atomic claim with lease + lease recovery | **INTEGRATION-TESTED** | same (concurrency test caught a real lock bug) |
| Dependency gating on `completed_verified` | **INTEGRATION-TESTED** | `tests/test_lab_loop.py` |
| Planner (validated DAG from the cascade) | **INTEGRATION-TESTED** | `nexus_planner.py`, `tests/test_planner_worker.py` |
| OpenHands dispatch + poll | **LIVE-VERIFIED** | `nexus_lab.py` (real conversation ran to `finished`) |
| Independent Git verification | **LIVE-VERIFIED** | `GitVerifier` against the real repo |
| Replanning decision (complete/block/deadlock) | **INTEGRATION-TESTED** | `LabScheduler.tick` |
| Durable worker, separate process | **INTEGRATION-TESTED** | `lab_worker.py`, restart/recovery tests |
| Project context (versioned, observed) | **INTEGRATION-TESTED** + **LIVE-VERIFIED** analysis | `nexus_context.py`, `nexus_analysis.py` |
| Unified memory with provenance | **INTEGRATION-TESTED** | `nexus_context.py` |
| Notifications | **INTEGRATION-TESTED** | `nexus_lab_store.py`, UI |
| Autonomous Lab web UI | **LIVE-VERIFIED** (Chromium journey) | `web_ui.py`, `tests/test_browser_e2e.py` |

**Not implemented in the loop:** an LLM-driven *replanner* that authors new tasks
after a failed verification (today a failed/deadlocked dependency blocks the run
for a human); cost/token budgets (iteration/attempt/deadline budgets exist); and
multi-run concurrency within one goal (one active run per goal by design).

## Integrations

| Integration | Secret | Auth | Real request | Notes |
|---|---|---|---|---|
| OpenHands (executor + last-resort orchestrator) | present | **passed** | **passed** | `health()` true; a conversation ran to `finished`. No GitHub provider token on the account, so it cannot clone a repo itself. |
| GitHub (PAT) | present | **passed** | **passed** | `GET /user`, tree/contents reads, multi-account add/verify. |
| Supabase | present | **passed** (token) | **project INACTIVE** | The only project is not serving; tests use a local PostgreSQL. |
| Gemini | **absent** | not tested | not tested | cascade falls through |
| Groq | **absent** | not tested | not tested | cascade falls through |
| OpenRouter | **absent** | not tested | not tested | cascade falls through |
| Figma | present | passed | read-only | unused by NEXUS |
| `FROM_API_KEY` | absent | — | — | no intended use found in the repository |

The provider cascade is exercised against a real local HTTP server per provider
shape; **no live LLM-provider call was made** because no provider key is set.

## Clients

| Artifact | Path | Status |
|---|---|---|
| Web + E2E | `orchestration/web_ui.py` | **LIVE-VERIFIED** (Chromium) |
| Debian `.deb` | `dist/nexus-client_2.0.0_all.deb` | **BUILT + install/uninstall verified** |
| Android `.apk` | `dist/nexus-2.0.0.apk` | **BUILT + signature/manifest verified**; **BLOCKED** runtime (no `/dev/kvm`) |
| Windows `.exe` | `dist/nexus-client.exe` | **BUILT + PE/version verified**; **BLOCKED** runtime (no Windows host) |

## Tests

- `ruff check .` → **All checks passed**
- `pytest tests/ -q` → **625 passed, 1 skipped** (was 557 before this work)

## Remaining blockers (genuine, distinguished)

- **Missing credentials:** no `GEMINI_API_KEY` / `GROQ_API_KEY` /
  `OPENROUTER_API_KEY` → the cascade cannot be live-verified.
- **Unavailable infrastructure:** no `/dev/kvm` (no Android emulator), no Windows
  host (no `.exe` run), Supabase project `INACTIVE` (no live DB).
- **Account limitation:** the OpenHands account has no GitHub provider token, so
  it cannot clone a repository; a full live code-change acceptance needs that
  token (or the `direct` transport with an authenticated sandbox).
- **Unfinished implementation:** the autonomous *replanner* (auto-authoring
  corrective tasks) and push notifications.

## Installation

```bash
# Server + worker
pip install -r requirements.txt
export SUPABASE_DB_URL=... WEB_AUTH_TOKEN=... AUTH_USERNAME=... AUTH_PASSWORD_HASH=... AUTH_SESSION_SECRET=... NEXUS_SECRET_KEY=...
python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN
python -m orchestration.lab_worker --worker-id lab-1

# Debian client
sudo dpkg -i dist/nexus-client_2.0.0_all.deb && NEXUS_URL=https://nexus.example.com nexus-client

# Android
adb install dist/nexus-2.0.0.apk

# Windows
nexus-client.exe https://nexus.example.com
```

## Truthfulness summary

Implemented + tested + live-verified: the OpenHands dispatch/verify path, GitHub
reads and multi-account, project context analysis. Implemented + integration-
tested (not live): the scheduler, planner, worker, memory, notifications, UI,
cascade. Built but not runtime-tested: the APK and the `.exe`. Not implemented:
the autonomous replanner, push notifications, multi-account OpenHands routing,
and a native iOS app (excluded by the brief).
