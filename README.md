# NEXUS — AI development control plane

Durable control-plane state for orchestrating OpenHands agent conversations,
with project-scoped execution, a CLI, an MCP surface and an Ed25519 Evidence
Trust Engine.

A **project** owns repositories (many-to-many), tasks and workflows. A
**workflow** holds a goal. A **Sr** (senior) OpenHands conversation plans it and
delegates each unit of work to **Jr** tasks through client tools. A dispatcher
runs each Jr to completion, records the result, and feeds it back to the Sr.
Every result can be captured as **signed evidence** whose verdict is earned,
not granted.

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

Start here:

- [docs/MVP_RELEASE_REPORT.md](docs/MVP_RELEASE_REPORT.md) — what is
  implemented, tested and verified, and what still needs a live OpenHands key.
- [docs/AUTONOMOUS-LAB.md](docs/AUTONOMOUS-LAB.md) — honest per-stage status
  (implemented / tested / live verified / blocked / not implemented).
- [AGENTS.md](AGENTS.md) — repository layout and hard-won runtime facts.
- [docs/NEXUS-SPEC.md](docs/NEXUS-SPEC.md) — the target architecture.
- [CLOUD-INTEGRATION-MAP.md](CLOUD-INTEGRATION-MAP.md) — the Cloud API surface.
- [DEPLOY.md](DEPLOY.md) — deployment guide.

## What is ready today

Working and covered by tests (the suite passes against a disposable
PostgreSQL; the browser E2E needs Chromium, and the end-to-end Cloud scripts
are manual and need a live LLM):

- The **Dispatcher**: durable workflows/tasks, atomic claim with leases,
  idempotent submission and Sr start, bounded retry, recovery, and operator
  actions (cancel / resume / retry / rename) with a web UI.
- The **Sr→Jr bridge**: Sr runs on Cloud with `client_tools` and delegates to
  Jr tasks, which the dispatcher runs and feeds back.
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
- A **CLI** (`nexus`), an **MCP** surface (stdio + Streamable HTTP) and an
  installable **PWA** shell of the same dashboard.

Not ready (see `docs/NEXUS-SPEC.md` and `docs/DEVELOPMENT-LAB.md`): the NEXUS
plans do **not** dispatch work; there is no LLM planner/gateway or provider
cascade, no autonomous goal loop, no multi-account OpenHands routing, no
server-side speech recognition, and no native Desktop/Android/iOS packages. A
legacy `completed` status means the Dispatcher's own rules were met — it is
**not** verified QA evidence. Uploaded files are not yet delivered to agents.

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

## Evidence Trust Engine

Evidence is signed with Ed25519 over a canonical byte encoding. A verdict is
earned: `VERIFIED` requires a trusted, non-revoked key and a valid signature; a
record that merely exists is `UNVERIFIED`; tampering with a signed field is
`FAILED`. Project-scoped records sign `project_id` (format v2), so editing the
column afterwards breaks the signature; legacy records stay unscoped. HMAC
alone can never produce `VERIFIED`. The hash chain detects reordering by an
honest reader; the signature is the real integrity boundary.

## MCP

`python -m orchestration.mcp_server` speaks MCP over stdio and exposes a limited
tool set (`list_projects`, `get_project`, `list_repositories`, `list_tasks`,
`get_task`, `create_task`, `dispatch_task`, `get_execution_status`,
`list_evidence`, `verify_evidence`). There is no arbitrary shell, SQL or eval
tool. Every tool re-checks project membership through the same services the HTTP
API uses. Streamable HTTP is not implemented in this MVP.

## Cross-platform web client (PWA)

The dashboard installs as a PWA on supported Android, Windows and Linux browsers
after deployment over HTTPS. It is an online web client, not an APK. Install,
security and known blockers are in [docs/CLIENTS.md](docs/CLIENTS.md).

## Deployment

`requirements.txt`, `render.yaml`, `DEPLOY.md`. Start exactly one instance:
`python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN`.
Migrations are idempotent and applied on first connect unless `AUTO_MIGRATE=false`.
Back up before applying a new migration to a database with real data.

## NEXUS planning and operations

The current NEXUS increment stores versioned, manually authored DAG plans with
validated dependencies, criteria and owner approval. Plan approval does not
dispatch work. See [docs/NEXUS-OPERATIONS.md](docs/NEXUS-OPERATIONS.md) for
API contracts, UI usage, security limits, testing, and deployment precautions.
Migration `0010_nexus_plans.sql` is automatically applied on startup unless
`AUTO_MIGRATE=false`; review and back up the target database before deployment.

## NEXUS laboratory, files and GitHub (second increment)

The dashboard now has a **Лаборатория** tab and a **GitHub** tab:

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
