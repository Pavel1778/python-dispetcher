# NEXUS — AI development control plane

Durable control-plane state for orchestrating OpenHands agent conversations,
with project-scoped execution, a CLI, an MCP surface and an Ed25519 Evidence
Trust Engine.

**3.0 candidate (not a release):** [acceptance and blockers](docs/nexus-3.0/ACCEPTANCE.md) · [managed OpenHands profiles and verification grants](docs/nexus-3.0/OPERATIONS.md).

A **project** owns repositories (many-to-many), tasks and workflows. A
**workflow** holds a goal. A **Sr** (senior) OpenHands conversation plans it and
delegates each unit of work to **Jr** tasks through client tools. A dispatcher
runs each Jr to completion, records the result, and feeds it back to the Sr.
Every result can be captured as **signed evidence** whose verdict is earned,
not granted. Durable lab runs already retry unverified work within their
existing policy and backoff; this is not an LLM-driven replanner and does not
claim an `AUTO_CORRECTED` evidence verdict.

```
project ──▶ repositories (explicit links)
   │
   └─▶ workflow ──▶ Sr conversation ──sr_create_task──▶ task ──▶ Jr conversation
                          ▲                                     │
                          └────────────── result ───────────────┘
   task/workflow result ──▶ Evidence Trust Engine (Ed25519)
                            ──▶ VERIFIED / UNVERIFIED / FAILED / BLOCKED
```

State lives in PostgreSQL; conversations run on OpenHands Cloud. The default
Cloud transport provisions a sandbox and drives its agent-server directly
(the `direct` transport); the legacy app-conversation endpoint is kept behind
`OPENHANDS_TRANSPORT=app`.

Self-QA's default host mode runs repository commands with the worker's OS
permissions; a scratch checkout is **not a security sandbox**. An optional,
fail-closed Docker runner is available for operator-provisioned pinned images;
read [the setup and limitations](docs/SELF_QA_SANDBOX.md) before enabling it.

For faster cross-process queue wakeups, optionally set `NEXUS_LISTEN_DSN` on
web and lab-worker processes to a **direct or session-pooled** connection to
that same PostgreSQL database. Migration `0045` sends type-only task/run
hints after commit; PostgreSQL remains the durable queue and periodic polling
still handles missed notifications, leases and OpenHands status. Repeated hints
are coalesced with a 0.5-second minimum interval per queue to limit retry storms
(the initial rescan on connection is separate).
Leave the variable unset to keep timer-only behaviour. A transaction-pooling endpoint is
not suitable for `LISTEN`; each enabled process uses one extra connection.
Monitor PostgreSQL notification queue usage: an exhausted queue can cause a
notifying transaction to fail at commit. See [RFC-001](docs/RFC-001_EVENT_DRIVEN.md) and
[performance measurements](docs/PERFORMANCE.md).

Start here:

- [docs/PRODUCT_STATUS.md](docs/PRODUCT_STATUS.md) — what works today, with the
  test that proves each claim, and what is honestly still missing.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the component map and how the
  parts interact.
- [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md) — run NEXUS on your own Debian 13
  box with one command (`scripts/nexus.sh`).
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — run, monitor, diagnose, recover,
  back up.
- [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) — per-subsystem
  readiness with the real evidence.
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) — what is not done and
  what is only proven against the stand-in servers.
- [docs/INTEGRATION_TESTING.md](docs/INTEGRATION_TESTING.md) — how to exercise
  the live OpenHands / GitHub / LLM / PostgreSQL.
- [docs/BRANCHES.md](docs/BRANCHES.md) — branching, merging and cleanup.
- [AGENTS.md](AGENTS.md) — repository layout and hard-won runtime facts.
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — every environment variable, run
  command, dependency and required permission in one place.
- [DEPLOY.md](DEPLOY.md) — deployment guide.
- [docs/SINGLE_VM.md](docs/SINGLE_VM.md) — one VM via Docker Compose (an alternative to the native `scripts/nexus.sh` path).
- [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md) — the backup policy and the restore drill.
- [docs/CLOUDFLARE_TUNNEL.md](docs/CLOUDFLARE_TUNNEL.md) — a temporary or stable public URL.
- [docs/PRODUCT_ROADMAP.md](docs/PRODUCT_ROADMAP.md) — what is next, and what is deliberately out of scope.
- [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — dependency direction and module boundaries.
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) — resource-usage baseline and verified orchestration, scheduler, and E2E HTTP speedups.
- [docs/performance/BASELINE.md](docs/performance/BASELINE.md) — measured startup/footprint/claim-path numbers.
- [docs/security/SECURITY_AUDIT.md](docs/security/SECURITY_AUDIT.md) — security findings and their classes.
- [CHANGELOG.md](CHANGELOG.md) — what changed, newest first.

> **A note on root-level reports.** Files such as `FINAL_RELEASE_REPORT.md`,
> `NEXUS_INTEGRATION_RECOVERY_FINAL_REPORT.md`, `PR46_PR47_FINAL_INTEGRATION_REPORT.md`
> and the ones under `reports/` are **historical audit artifacts** from earlier
> phases. They are kept for provenance and are not updated to match every
> commit; the living state is `COMPLETION_LEDGER.md`, this README, and the
> documents linked above.

## What is ready today

Working and covered by tests (the suite passes against a disposable
PostgreSQL; the browser E2E needs Chromium, and the end-to-end Cloud scripts
are manual and need a live LLM):

- The **Dispatcher**: durable workflows/tasks, atomic claim with leases,
  idempotent submission and Sr start, bounded retry, recovery, and operator
  actions (cancel / resume / retry / rename) with a web UI.
- The **Sr→Jr bridge**: Sr runs on Cloud with `client_tools` and delegates to
  Jr tasks, which the dispatcher runs and feeds back.
- **Chat-first routing**: a message is not a task. `orchestration/nexus_intent.py`
  classifies each chat turn as `chat` (answer), `analysis` (read the real state,
  change nothing) or `autonomous` (start a run), from the shape of the message,
  never from a single keyword. A question is answered in any mode and starts no
  run; an explicit command ("исправь …", "реализуй …") starts work; an ambiguous
  message gets one clarifying question; "продолжи" continues the conversation's
  existing run instead of creating a second. The conversation modes are `auto`
  (default), `ask`, `analysis`, `plan`, `code`, `autonomous` and `manual`.
- **Three ways to work, one router.** The conversation's *mode* is a default and
  the message decides the rest (see `docs/CHAT-FIRST.md`):
  - **Auto** (default) — the router decides per message. Understandable work is
    planned and run by itself; a question is answered; an unclear message is
    clarified. Autonomous by design, so it never asks permission it does not
    need.
  - **Manual** — NEXUS discusses, proposes a **plan**, and runs only on an
    explicit launch command ("Запускай", "Execute", "Run it"). A question about
    launching ("когда запустишь?"), a prohibition ("не запускай пока") and a
    condition ("запускай только после проверки") are not commands. The
    confirmation is bound to the plan's *version*: a plan that changed must be
    approved again. The dialogue state (`DISCOVERY … AWAITING_APPROVAL …
    EXECUTING …`) is stored, so it survives a restart, and `EXECUTING` is
    reachable only from `AWAITING_APPROVAL`.
  - **Chat** (`ask`/`analysis`) — answers, explanations and analysis. No agent
    is ever started.
- **Runs in the interface.** A run that completed successfully leaves the active
  list by itself (its history, tasks and report stay in the **archive**,
  `GET /runs?archived=1`). A failed, blocked or paused run stays visible: it
  needs attention. A run that ended without completing (cancelled, failed)
  writes an honest report to its own chat at once — never as a success. A
  blocked run tells its chat why, immediately.
- **Chat command system**: slash commands (`/chat /plan /autorun /audit /fix
  /test /review /deploy /research /status /help`), four hashtag groups
  (technology, quality, execution, restriction) and typed parameters
  (`--repo`, `--branch`). Restrictions have priority — `#read-only` downgrades
  an execute command to a read-only analysis, and `/deploy #no-deploy` is
  refused. One parser serves both typed commands and interface buttons, so they
  cannot drift apart. See `docs/COMMANDS.md`; `GET /api/commands` is the
  vocabulary.
- **Conversation management**: pin/unpin (durable, a pinned section at the top),
  archive/restore, soft delete/restore (a real, reversible delete that leaves
  the project, the repository and the transcript alone), search by title and by
  the text of messages, and a self-contained JSON export. The studio sidebar has
  a pinned section, an archive and a trash.
- **Stop generation**: the answer in flight can be stopped at once — the cancel
  aborts the live provider call, keeps whatever was produced with a "stopped"
  mark, and leaves the chat immediately usable. It is a separate action from
  cancelling a run: stopping a text answer never touches an autonomous run.
- **Run history**: hide/show a finished run, remove it from history (its own
  record only — the goal, the chat, the evidence and the repository stay), and
  retry *only* the tasks that failed.
- **Authentication**: browser session (PBKDF2 + server-side sessions + CSRF)
  and a machine bearer token, plus login rate limiting and proxy-aware client
  IP handling.
- **Project execution binding (D-2)** and the **Ed25519 Evidence Trust
  Engine**: a result is `VERIFIED` only from a trusted, valid signature.
- **GitHub discovery** (read-only) with an allowlist, and **optional
  confirmed writes** (create / change visibility) behind a session + CSRF +
  one-use confirmation and `GITHUB_ENABLE_WRITES`.
- **NEXUS foundation**: projects and goals, manually authored versioned DAG
  plans (validated, owner-approved), and bounded project file uploads with a
  file laboratory (workspaces, archive safety, voice notes).
- The **Autonomous Lab**: enter a goal, analyze the repository, plan via the
  provider cascade, and let a **durable loop** - inside the server by default,
  or a separate worker process - dispatch tasks to OpenHands, verify the real
  Git result, record evidence and decide the next step. A task is completed
  only when independently verified.
- **GitHub multi-account**: several accounts with encrypted PATs and an explicit
  per-project binding.
- **OpenHands multi-account and failover**: any number of accounts with encrypted
  API keys, a selection pool (manual / auto / failover / load-balancing), health
  tracking, and an end-to-end failover that switches a failed task to another
  account with a context hand-off (see `docs/FAILOVER.md`). A **diagnostics**
  report (`python -m orchestration.cli openhands diagnostics`) checks every
  integration point and marks each VERIFIED / FAILED / BLOCKED / UNVERIFIED.
- A **CLI** (`nexus`), an **MCP** surface (stdio + Streamable HTTP), an
  installable **PWA**, and native client packages: Debian `.deb`, Android
  `.apk`, Windows `.exe`.

Not ready (see `docs/AUTONOMOUS-LAB.md` for the per-item status): a *bounded*
run stops on a failed dependency rather than authoring corrective tasks itself
(a durable run retries the task, but there is no LLM-driven replanner that
invents new work), there is no out-of-band push notification (a finished run's
report arrives as a chat message), and there is no native iOS app. A legacy
`completed` status means the Dispatcher's own rules were met — it is **not**
verified QA evidence.

## Quick start (clean environment)

```bash
# 1. Dependencies (psycopg[binary], cryptography)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. A PostgreSQL database (any 14+). Migrations apply on start by default.
export SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus"

# 3. Owner login, machine token, evidence key
python -m orchestration.web_auth hash       # -> AUTH_PASSWORD_HASH
python -m orchestration.web_auth secret     # -> AUTH_SESSION_SECRET
python -m orchestration.evidence_keygen     # -> NEXUS_EVIDENCE_* values
export AUTH_USERNAME=owner
export AUTH_PASSWORD_HASH='pbkdf2_sha256$...'
export AUTH_SESSION_SECRET='...'
export NEXUS_EVIDENCE_KEY_ID=nexus-lab
export NEXUS_EVIDENCE_PRIVATE_KEY='...'
export NEXUS_EVIDENCE_KEYRING='nexus-lab:...'
export WEB_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
# Optional, for real execution: export OPENHANDS_API_KEY=...

# 4. Run the single instance (web UI + dispatcher loop)
python -m orchestration.web_server --host 0.0.0.0 --port 8000 --token "$WEB_AUTH_TOKEN"
```

Open `http://localhost:8000`, sign in, create a project, link a repository,
create a task, and watch the execution state. `GET /healthz` is liveness;
`GET /readyz` adds a real database round-trip.

> **One virtual machine, everything on it (PostgreSQL + app + worker):**
> see [docs/SINGLE_VM.md](docs/SINGLE_VM.md) — `cp .env.example .env`, fill the
> secrets, `docker compose up -d --build`. No external database service needed.

The CLI is a thin client over the same API:

```bash
export NEXUS_URL=http://localhost:8000 NEXUS_TOKEN="$WEB_AUTH_TOKEN"
python -m orchestration.cli doctor
python -m orchestration.cli project create "My project"
python -m orchestration.cli project repo add <project_id> owner/name
python -m orchestration.cli task create "Implement X" --project <project_id> \
  --repository owner/name --branch main
python -m orchestration.cli evidence list --project <project_id>
python -m orchestration.cli qa verify --project <project_id>
```

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# The pytest suite reads TEST_SUPABASE_DB_URL and skips the DB-backed modules
# cleanly when it is unset. Use a disposable database.
export TEST_SUPABASE_DB_URL="postgresql://postgres@127.0.0.1:5432/ohp_test"
python3 -m pytest -q                     # unit + integration + browser E2E
python3 -m ruff check .

# The end-to-end Cloud scripts read SUPABASE_DB_URL, are not collected by
# pytest, and need a live LLM.
python3 tests/e2e_local_sr_jr.py         # real Sr→Jr on a local agent-server
python3 tests/e2e_cloud_sr_jr.py         # real Sr→Jr on OpenHands Cloud
```

The browser E2E (`tests/test_browser_e2e.py`) drives Chromium through the real
user journey and is skipped when Playwright/Chromium is absent.

## Project execution binding (D-2)

`tasks.project_id` and `workflows.project_id` are nullable and are **not**
backfilled: legacy rows stay unscoped. A project's repositories are an explicit
many-to-many link (`nexus_project_repositories`); a repository can be shared by
several projects. `tasks.repository_id` is canonical and is kept in agreement
with `tasks.selected_repository` by a database trigger that refuses a conflict
rather than guessing. Before any dispatch, `authorize_dispatch` re-checks that
the project exists, the task and workflow share it, the repository is linked,
enabled and on an allowed branch, and the caller is a member. The same gate
protects the web API, the CLI, MCP and the background dispatcher.

The autonomous lab goes through the same rule: `authorize_run_task` is called
when a run is started and **again immediately before each conversation is
created**, so a link removed in between still stops the work and the task is
blocked with the reason - never silently redirected to a default repository. A
run for one project can never reach another project's repository
(`tests/test_run_binding.py`, the regression for run `2ae6adc3`).

## Evidence-based acceptance

A task is `completed_verified` only when the conversation finished, the head of
the task's branch (read from GitHub now) is a commit the agent named, **and the
branch actually moved** during the attempt. The head SHA alone does not prove a
new change appeared: an agent whose push was refused names the head it found, so
a change-requiring task (`implementation`, `bugfix`, `documentation`) whose
branch did not move is `completed_unverified` and is reworked. A task whose kind
does not change the code (`verification_only`, `research_audit`) is accepted as
`completed_no_changes` - an honest no-change result, not a claim of a change.
This separates the nine things that used to be conflated: the agent reported
success, the runtime completed, the workspace verified, a change was produced,
committed, pushed, tests passed, independent verification passed, and the task
was accepted.

## Project and conversation lifecycle

Projects and conversations can be created, renamed, archived, moved to the
trash, restored and purged. A delete is a **soft delete**: the row and its data
stay, and a restore brings it back. A **purge** is the separate, explicit,
irreversible step, refused while a run is active, and it never touches the
external GitHub repository or any other project. The studio has context menus
for both, with a confirmation that names what is inside. See
[docs/PROJECT-LIFECYCLE.md](docs/PROJECT-LIFECYCLE.md).

## Performance and observability

`orchestration/tracing.py` times each stage NEXUS controls (dispatch, poll,
verify) with one secret-free span shape; `GET /api/observability` reports queue
depth, stuck tasks, worker heartbeat and per-stage latency, and the studio shows
it under **Настройки → Диагностика**. `tools/benchmark.py` measures the queue
path over real PostgreSQL. See [docs/PERFORMANCE.md](docs/PERFORMANCE.md) and
[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md). The "run running, task pending,
attempt 0" symptom is answered by `queue.claimable_now` and `stuck_tasks` - a
due task nobody claims is a missing or dead worker, and it is now visible.

**Prometheus.** `GET /metrics` serves the exposition format (`text/plain;
version=0.0.4`) behind the same session/bearer auth as every other read:
`nexus_tasks_claimed_total{worker}`, `nexus_evidence_verifications_total{verdict}`,
`nexus_openhands_api_latency_seconds{account,transport}`, the database-pool
gauges, queue/stuck/worker gauges and HTTP count/latency by normalised path.
The registry is `orchestration/nexus_metrics.py`; instrumentation never raises
and never labels a secret. A 12-panel Grafana dashboard is in
[docs/dashboards/nexus-overview.json](docs/dashboards/nexus-overview.json).

**Self-healing.** With `NEXUS_REPLANNER=1` and a provider key, a task that does
not verify gets one short corrective instruction from the provider cascade (at
most three per task, stored on the task so the ceiling survives a restart). The
correction is delivered to the next attempt and signed into the Evidence Trust
Engine as `AUTO_CORRECTED`; a provider outage falls back to the deterministic
rework note and never stops a run (`orchestration/replanner.py`).

## Evidence Trust Engine

Evidence is signed with Ed25519 over a canonical byte encoding. A verdict is
earned: `VERIFIED` requires a trusted, non-revoked key and a valid signature; a
record that merely exists is `UNVERIFIED`; tampering with a signed field is
`FAILED`. Project-scoped records sign `project_id` (format v2), so editing the
column afterwards breaks the signature; legacy records stay unscoped. HMAC
alone can never produce `VERIFIED`. The hash chain detects reordering by an
honest reader; the signature is the real integrity boundary.

## MCP

NEXUS exposes MCP over **two transports**, both served by the same code
(`orchestration/mcp_server.py`):

- **stdio** — `python -m orchestration.mcp_server` (JSON-RPC 2.0 over stdin/stdout);
- **Streamable HTTP** — `POST /mcp` on the web server, reusing the browser
  session/CSRF or the `WEB_AUTH_TOKEN` bearer.

The tool set is limited (`list_projects`, `get_project`, `list_repositories`,
`list_tasks`, `get_task`, `create_task`, `dispatch_task`,
`get_execution_status`, `list_evidence`, `verify_evidence`, `list_runs`,
`get_run`, `cancel_run`, `list_project_executors`, `assign_project_executor`).
There is no arbitrary shell, SQL or eval tool. Every tool re-checks project
membership through the same services the HTTP API uses, and each tool carries a
scope (`orchestration/nexus_policy.py`, `MCP_TOOL_SCOPES`) that the caller's role
must hold. A notification returns `202`; a parse error is `400`.

## Cross-platform web client (PWA)

The dashboard installs as a PWA on supported Android, Windows and Linux browsers
after deployment over HTTPS. It is an online web client, not an APK. Install,
security and known blockers are in [docs/CLIENTS.md](docs/CLIENTS.md).

## OpenHands integration

NEXUS drives OpenHands conversations to execute work, verifies the result
independently, and can route work across several accounts. Start here:

- [docs/OPENHANDS_INVENTORY.md](docs/OPENHANDS_INVENTORY.md) — the full
  inventory of the OpenHands surface NEXUS depends on (API, version,
  authorization, conversations, events, status, runtime, workspace, tools,
  limits, errors, timeouts, availability) and how the current client maps onto
  it, with the live versions and the live probe result.
- [docs/OPENHANDS_SETUP.md](docs/OPENHANDS_SETUP.md) — the two transports, the
  required environment (including the `OPENHANDS_DEFAULT_MODEL` trap), and how
  to verify a real conversation runs.
- [docs/OPENHANDS_ACCOUNTS.md](docs/OPENHANDS_ACCOUNTS.md) — the account pool,
  the four selection modes, and the per-account controls.
- [docs/DURABLE-RUNS.md](docs/DURABLE-RUNS.md) — runs that do not give up:
  work cycles and waits, the report in the chat, cancelling and stopping an
  agent for real, the executors of a project, several repositories per project.
- [docs/FAILOVER.md](docs/FAILOVER.md) — what happens when an account fails and
  how the context is handed to the next one.
- [docs/WORKER_SETUP.md](docs/WORKER_SETUP.md) — who advances autonomous runs
  (the server itself, or a separate worker process), and the heartbeat that
  proves the loop is alive.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom-first fixes.
- [docs/TESTING.md](docs/TESTING.md) — what is real in each test layer.

One command checks every integration point:

```bash
NEXUS_URL=https://nexus.example NEXUS_TOKEN="$WEB_AUTH_TOKEN" \
  python -m orchestration.cli openhands diagnostics          # structural checks
NEXUS_URL=... NEXUS_TOKEN=... \
  python -m orchestration.cli openhands diagnostics --live    # + a real conversation
```

Each check earns one of four statuses — `VERIFIED`, `FAILED`, `BLOCKED`,
`UNVERIFIED` — so the report never claims a check passed that did not run. The
live probe runs on the account it selected (that account's base URL, key,
transport and model), so a `VERIFIED` is evidence about *that account*.

To see which tools are available to NEXUS and which to OpenHands:

```bash
NEXUS_URL=... NEXUS_TOKEN=... \
  python -m orchestration.cli openhands tools --live
```

It reports the NEXUS surface (the MCP tools and the `sr_` client tools) and the
OpenHands surface (the agent's own tool catalog, read from the agent-server and
mapped to terminal / workspace files / Git / testing / browser, plus the
account's configured MCP server names). The same three checks appear in the
diagnostics report as `nexus_tools`, `openhands_tools` and `mcp_configuration`.
Without `--live` the agent catalog is `UNVERIFIED` (reading it on the direct
transport may provision a sandbox); a listed tool is not proof it ran.

To move a running task to another account by hand:

```bash
python -m orchestration.cli openhands switch \
  --project <project> --run <run> --task <task> --account <account>
```

## Deployment

`requirements.txt`, `render.yaml`, `DEPLOY.md`. Start exactly one instance:
`python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN`.
Migrations are idempotent and applied on first connect unless `AUTO_MIGRATE=false`.
Back up before applying a new migration to a database with real data.

The web server advances autonomous runs on a thread of its own unless
`NEXUS_LAB_INLINE=0`; a separate worker (`python -m orchestration.lab_worker`)
can advance them too, and `render.yaml` and `docker-compose.yml` define one. See
[docs/WORKER_SETUP.md](docs/WORKER_SETUP.md) and
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for the full variable list.

## NEXUS planning and operations

The current NEXUS increment stores versioned, manually authored DAG plans with
validated dependencies, criteria and owner approval. Plan approval does not
dispatch work. See [docs/NEXUS-OPERATIONS.md](docs/NEXUS-OPERATIONS.md) for
API contracts, UI usage, security limits, testing, and deployment precautions.
Migration `0010_nexus_plans.sql` is automatically applied on startup unless
`AUTO_MIGRATE=false`; review and back up the target database before deployment.

## NEXUS laboratory, files and GitHub (second increment)

These features are reachable through the API, the CLI and MCP. The studio
(`orchestration/studio/`) is a single chat application and does **not** show
them as screens: it has no Лаборатория or GitHub tab. The create/visibility
flow is still two-step on the server (propose -> confirm).

- **File laboratory** - upload documents, images, source code and archives into
  a project; preview text and images; download; delete. Bytes are stored in
  PostgreSQL, never on the server's filesystem.
- **Development workspaces** - extract an archive into a durable, project-scoped
  file tree, edit a file and see a unified diff. The workspace is data, not a
  shell: nothing uploaded is executed.
- **Archive safety** - Zip Slip, symlinks, device/FIFO entries and archive bombs
  (by entry count, size and compression ratio) are rejected before a single row
  is written.
- **Voice notes** - record in the browser and store the audio. There is no
  server-side speech recognition; a browser-supplied transcript is labelled
  `browser` and is not a server-verified result.
- **GitHub** - read branches, commits, issues and pull requests; create a
  repository and change its visibility **only after a separate confirmation**.
  Making a repository public is refused without an explicit acknowledgement.

See [docs/NEXUS-LABORATORY.md](docs/NEXUS-LABORATORY.md) for the API and
[docs/NEXUS-AUDIT.md](docs/NEXUS-AUDIT.md) for the documentation-vs-code audit.
Migration `0011_nexus_files.sql` is additive and auto-applied unless
`AUTO_MIGRATE=false`. Still **not** implemented: a sandboxed terminal/test
runner, server-side speech recognition, agent streaming, independent QA, and
plan execution. None of these is claimed as done.
