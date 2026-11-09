# AGENTS.md

## What this repository is

This repository is the durable control plane for orchestrating OpenHands
agent conversations. It owns task state, leases, assignment and recovery.
It does not own the runtime: conversations execute in the OpenHands
agent-server.

## Layout

- `orchestration/control_plane.py` — SQLite task store: `submit_task`,
  `claim_task`, `transition`, `reclaim_expired`, `bind_conversation`,
  `set_result`. Enforces the state machine in `_ALLOWED`.
- `orchestration/openhands_client.py` — stdlib-only REST adapter for the
  agent-server. No external dependencies, on purpose.
- `orchestration/dispatcher.py` — one tick = reconcile → recover → advance →
  dispatch. This is the only place that decides task state from runtime
  signals.
- `INTEGRATION-MAP.md` — every confirmed API fact. Read it before changing the
  adapter.
- `CLOUD-INTEGRATION-MAP.md` — the same for the **Cloud V1** adapter. Read it
  before changing anything under `cloud_*`.
- `orchestration/cloud_client.py` — stdlib-only REST adapter for **OpenHands
  Cloud V1** (`/api/v1/app-conversations`, Bearer auth). Also the account-level
  sandbox calls (`start_sandbox`, `wait_for_sandbox`, `pause/resume/delete`).
- `orchestration/cloud_direct.py` — the **direct** transport: provision a
  sandbox, drive its agent-server. Implements the same client surface as
  `cloud_client.py`, so the dispatcher and Sr runtime do not know which is in
  use. `build_cloud_client` picks between them from `OPENHANDS_TRANSPORT`.
- `orchestration/local_runtime.py` — the same client surface against a *local*
  agent-server, for the end-to-end tests.
- `orchestration/cloud_store.py` — PostgreSQL/Supabase task store. Atomic claim
  via `SELECT ... FOR UPDATE SKIP LOCKED`; legal-path recovery via
  `force_transition`. Holds the sandbox/conversation route table.
- `orchestration/cloud_dispatcher.py` — Cloud dispatcher. Two-phase create
  (start task → poll → bind), allowlist, bounded retry, recovery.
- `orchestration/sr_tools.py` — Sr operations (create/list/status/report/
  revise/accept/cancel) with real, durable effects.
- `migrations/0001_init.sql` — Supabase schema. Idempotent; apply with `psql`.
- `tests/fake_cloud.py` — a real local HTTP server speaking the Cloud V1
  surface. Cloud tests use it, not mocks of the client.
- `tests/fake_runtime.py` — a real local HTTP server speaking the confirmed
  v1.50.1 surface. Tests use it, not mocks of the client.
- `tests/e2e_*.py` — manual scripts against the real runtime. Not collected by
  pytest (`python_files = test_*.py`).

## Commands

```bash
# Tests (needs the venv that has pytest; the system python does not).
# Cloud tests need a PostgreSQL DSN; without it they skip cleanly.
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/db" \
python \
  -m pytest tests/ -q -p no:cacheprovider

# Lint (same version as CI, pinned in requirements-dev.txt).
python \
  -m ruff check .

# Apply the Supabase schema (idempotent).
psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql

# Manual end-to-end against the live runtime (creates and deletes its own
# conversation; the three long-lived conversations are never touched)
python \
  tests/e2e_dispatcher_loop.py
```

## Runtime facts that cost real debugging to learn

These were found by running against the live server, not from docs:

1. **`agent_settings` is required on create.** Without it: `422 ... One of
   agent, agent_settings, or agent_profile_id must be provided`. The dispatcher
   fetches it from `GET /api/settings` once and forwards it, so it never needs
   to hold an LLM key.
2. **Creation is idempotent only if you supply `conversation_id`.** The
   dispatcher derives it with `task_conversation_id()` (uuid5 of the task id).
   A restart then gets `200` and the existing conversation, not a duplicate.
   Do not switch to a random uuid without replacing the recovery logic.
3. **`?kind=ConversationErrorEvent` on `events/search` returns nothing** even
   when the event exists. Fetch unfiltered and match `classification` on the
   client.
4. **`GET /api/conversations/{id}/events` needs a body** and returns 422
   otherwise; use `/events/search` or `/events/count`.
5. **The runtime classifies its own failures.** `classification.retryable` is
   authoritative: `false` means retrying cannot succeed, so the task is
   dead-lettered at once. Do not retry auth/config errors.
6. **`POST /run` returning 409 means already running**, which is success.
7. **The app-conversation endpoint cannot always provision a conversation.**
   On the live service `POST /api/v1/app-conversations` can reach
   `STARTING_CONVERSATION` and then fail with `HTTP 500` from the inner
   create-conversation call to the sandbox; a start with no initial message is
   `READY` but `send-message` returns `502`. The failure is on the account side
   (the runtime rejects the account's legacy string `security_analyzer`), not
   in this code. The `direct` transport (`cloud_direct.py`) sidesteps it: it
   starts a sandbox itself and creates the conversation on that sandbox's
   agent-server. That path was verified end to end and is the default.
8. **A bare sandbox defaults to a model whose key may be absent.** A
   conversation created on a fresh sandbox then fails with an LLM auth error.
   Pass `OPENHANDS_DEFAULT_MODEL` (or `llm_model=`) to force one the sandbox can
   reach; `openhands/deepseek-v4.1-flash` worked when `gpt-5.x` did not.
9. **`DELETE /api/v1/sandboxes/{id}` needs the id as a query param too**
   (`?sandbox_id=<id>`), or it returns `422 Field required`.
10. **`client_tools` is a local/agent-server feature.** The local agent-server
    accepts `client_tools` on create and emits `ActionEvent`s for them; the
    Cloud V1 app-conversation request has no such field. The Sr tool bridge
    therefore needs the agent-server path, which is what `direct` provides.

## Invariants to keep

- A missing answer is never completion. `finished` with an empty final response
  is `blocked`, not `completed`.
- A transport error keeps the lease and does not increment `attempt`.
- One action per task per tick (`_touched`). Without it a persistently failing
  task spins inside a single tick.
- The API key is read from the environment and never logged.

## Environment note

The runtime at `127.0.0.1:60000` in this sandbox has **no working LLM key**
(auth fails). That is why the e2e scripts assert the dead-letter path. A
successful-completion e2e needs a valid key and is not yet exercised.

## Sr tool bridge and web UI

- Sr acts only through **client tools**: `start_conversation(..., client_tools=...)`,
  then poll `events/search` for `ActionEvent` and deliver results with
  `send-message`. See `orchestration/sr_runtime.py`.
- Client-tool names **must not collide with built-in tool names** (422). Sr tool
  names carry an `sr_` prefix for this reason. Do not rename them back.
- `workflow_id` is **never** taken from the model; the bridge substitutes the real
  one. Keep `workflow_id` out of `SrTools.tool_schemas()`.
- `orchestration/web_server.py` is the UI (`python -m orchestration.web_server`).
  Run tests with `TEST_SUPABASE_DB_URL` set; without a DB the DB-backed modules
  skip cleanly.
- The account has a **daily conversation limit**; probing schemas can exhaust it.
  Prefer the fake Cloud server in `tests/fake_cloud.py` for iteration.

## Account capability inheritance (Cloud path)

- The web path starts Cloud conversations with only the fields it needs and
  **omits** `agent_profile_id`, `agent_type`, `plugins`, `agent_settings` and
  `conversation_settings`, so each conversation inherits the account's own
  active agent profile and its configured MCP servers/skills. Never write these
  fields to reconfigure the account; they are opt-in via
  `OPENHANDS_AGENT_PROFILE_ID` / `SrRuntime(plugins=...)`.
- `GET /api/capabilities` (read-only) reports what the account has
  **configured** (settings: MCP names, marketplaces, LLM key presence, provider
  tokens). `GET /api/v1/app-conversations/{id}/skills` reports what a specific
  conversation has **available**. Neither proves a tool ran; only an observed
  `ActionEvent` proves it was **exercised**. Do not conflate the three.
- Client tools need no browser: `SrRuntime.tick()` polls `events/search`,
  claims by the stable event `id`, executes, and delivers with `send-message`.
  `tests/test_cloud_integration.py` proves this against the fake server.
- The workflow verifier now reads the Sr view *first*: an Sr in `error`/`stuck`
  fails the workflow, `waiting_for_confirmation` blocks it, and an Sr that
  stopped (or vanished) with no child work blocks it — none of these may hang
  forever or be reported as success.

## Deployment

- `requirements.txt` (only `psycopg[binary]`), `render.yaml`, `DEPLOY.md`.
  Start: `python -m orchestration.web_server --host 0.0.0.0 --port $PORT --token $WEB_AUTH_TOKEN`.
  Run exactly one instance. Migrations are idempotent and applied by the
  process on first connect (recorded in `schema_migrations`), because the
  Render free plan has no shell to run `psql` in; `AUTO_MIGRATE=false` turns
  that off for environments that manage the schema out of band.

## Sr start retry (why a workflow is never left queued)

- Starting Sr is a Cloud call that can fail transiently (429 while the account
  is rate limited, a 5xx, a dropped connection). When it does, the workflow row
  exists with no `sr_conversation_id`.
- Nothing else revisits such a workflow: the Sr loop needs an
  `sr_conversation_id` to service a workflow, so before this it sat in `queued`
  forever. `SrRuntime._retry_sr_starts` now scans `pending_sr_starts()` each
  tick and starts Sr, with a persisted exponential backoff
  (`sr_attempt`/`sr_next_attempt_at`, migration `0005`).
- Give-up policy: after `sr_start_max_attempts` the workflow is `blocked` with
  the last error as evidence; a `CloudAuthError` (401/403) blocks at once,
  because retrying a bad key only burns quota. Never retry forever, never stay
  silent.
- `_find_orphan_sr` (tag lookup) is what makes a concurrent or crashed start
  safe: it reuses an existing Sr instead of creating a second one. Keep that
  path intact.
- Tests apply **every** file in `migrations/` (`sorted(os.listdir(_MIG))`), not
  a hardcoded list, so a new migration cannot silently go unapplied. The schema
  guard (`REQUIRED_COLUMNS`) must name any column the store depends on.

## Web auth (added on top of the machine token)

- Two independent paths: a browser **session** (`orchestration/web_auth.py`,
  PBKDF2 password + server-side session rows + double-submit CSRF) and the
  existing **Bearer** `WEB_AUTH_TOKEN` for machine clients. Never let one
  replace the other; a change to either must keep both test suites green.
- Sessions are stored server-side (`sessions` table, migration `0004_auth.sql`)
  so logout is immediate; only `HMAC(AUTH_SESSION_SECRET, token)` is stored.
  `AUTH_SESSION_SECRET` must be a separate secret from the Cloud API key.
- Public routes include `/login`, `/healthz`, `/static/*`, `/favicon.svg`,
  `/manifest.webmanifest` and `/service-worker.js`. Everything else,
  reads included, needs a session or a bearer token. `/healthz` must stay a
  one-line JSON with no configuration.
- The server refuses to bind `0.0.0.0` unless `WEB_AUTH_TOKEN` and all three
  `AUTH_*` variables are set (`web_server.main`).
- The UI (`orchestration/web_ui.py`) is static strings: dark theme, Russian,
  responsive. It must not embed any secret; actions must be implemented and
  accurately described (browser-local voice input need not call an endpoint).

## GitHub repository discovery and confirmed writes

- `orchestration/github_client.py` reads `GITHUB_TOKEN` and calls
  `GET /user/repos`. Discovery is read-only, but optional repository creation and visibility writes
  exist behind `GITHUB_ENABLE_WRITES` and a session-bound, one-use confirmation.
  Sync only writes the local allowlist. `_request` must stay the single place
  the token is attached; redirects must never forward the token.
- The token value must never be logged, returned by the API or rendered in the
  UI. `GitHubClient.__repr__` is redacted on purpose; keep it that way. Only
  `configured` (a bool) is exposed, and `/api/system` and `/api/capabilities`
  report presence only. `tests/test_github_client.py` asserts this.
- A `403` with `X-RateLimit-Remaining: 0` means the token is **valid** and only
  the quota is spent, so it maps to `GitHubUnavailable` (503), not
  `GitHubAuthError` (400). Asking the operator to rotate a working token is a
  wrong instruction; do not "simplify" this back to a plain 403 -> auth mapping.
- `sync_repositories` refreshes only what GitHub is authoritative about
  (default branch, provider). It must never re-enable a repository an operator
  disabled, nor clear `allowed_branches`. The whole list is fetched before
  anything is stored, so a GitHub outage leaves the allowlist unchanged.
- Archived repositories are skipped: GitHub makes them read-only, so a task
  dispatched into one could never land a change.
- `/api/capabilities` gathers the GitHub/discovery block **before** calling the
  account API, and returns it even on a 502 from Cloud. Whether the allowlist is
  usable is what the operator needs during a Cloud outage.
- `tests/fake_github.py` is a real HTTP server, not a mock of the client: the
  client under test makes real requests over a socket.

## One request, one workflow (idempotency)

- A user request must create exactly **one** workflow and **one** Sr. The
  incident that made this explicit: one submission of «Привет, создай новую
  сессию и назови ее: Тест1» produced three workflows with byte-identical goals,
  three Sr conversations and three Jr tasks, ~63s apart (three distinct
  `orchestratorsr` tags). Each Sr had called `sr_create_task` once, so Sr/Jr
  semantics were fine -- the duplication was at the workflow level.
- Root cause: `App.start_workflow` minted a fresh workflow id per call and
  started an Sr for it, and `workflows` had no idempotency key. A retried
  request (client timeout then resubmit, double submit, proxy retry) therefore
  made another workflow/Sr/Jr. `tasks.idempotency_key` existed; the workflow
  path needed the same.
- The fix is enforced in the database, not in the UI: `workflows.idempotency_key`
  is UNIQUE (migration `0006_workflow_idempotency.sql`) and
  `create_workflow_idempotent` races on it, so the loser of a concurrent pair
  reads the winner's row. `start_workflow` takes the request id from the
  `Idempotency-Key` header or a `request_id` body field; the UI generates one
  `crypto.randomUUID()` per logical submission and reuses it on retry. Do **not**
  move this to a client-side check -- a retry and a double submit must both be
  absorbed by the server.
- Starting Sr is claimed with `claim_sr_start` (`FOR UPDATE SKIP LOCKED` plus a
  lease, mirroring `claim_task`). Without it, two ticks (two Render instances, or
  a tick overlapping a slow inline start) both saw "no Sr yet" and both started
  one. `bind_sr` clears the lease; `release_sr_start` returns it on failure.
- `pending_sr_starts` excludes `blocked`: `record_sr_failure(give_up=True)`
  blocks a workflow and clears the backoff, so without the exclusion the scan
  would retry a permanently failed workflow forever and keep starting
  conversations for it.
- Do not "fix" duplicates by filtering them in the UI or by merging rows. The
  duplicates are real rows; hiding them leaves the extra conversations running.
  Existing duplicates are left untouched -- deleting user-visible history is an
  operator decision.
- Tests: `tests/test_duplicate_requests.py`. It asserts one workflow per request
  (sequential, concurrent, in-flight retry, after restart), one Sr per workflow,
  one task per Cloud tool event, and that read endpoints and ticks never create
  anything. Reverting the idempotency logic makes 6 of them fail, so they are
  load-bearing, not decorative.
- Deleting Cloud conversations is a separate, guarded action:
  `tools/delete_cloud_sessions.py` deletes only ids named on the command line and
  only with `--confirm`; `tools/classify_cloud_sessions.py` is read-only and
  separates dispatcher-created sessions (tags, or a dispatcher persona in the
  event stream) from the user's own.

## Client IP, redirects and login ceilings (security hardening)

Three defects were fixed here; each has a test that fails if the fix is undone.

- **The client IP was the leftmost `X-Forwarded-For` value.** A proxy only
  *appends*, so the leftmost entry is whatever the caller sent. Rotating it made
  every login attempt look like a new client and the (username, IP) lockout never
  engaged. `client_ip_from_values` now takes the rightmost *trusted* hop, joining
  every header line first: `headers.get` returns only the first line, so a caller
  that sends its own line and lets the proxy append a second one still owned the
  key. `TRUSTED_PROXY_HOPS` (default 1) is how many proxies we run behind; a
  missing/junk value falls back to 1, a negative value clamps to 0, and a header
  shorter than the hop count falls back to the socket peer rather than indexing a
  spoofable list. Do not "simplify" this back to `split(",")[0]`.
- **The HTTP clients followed redirects.** `urlopen` re-sends every request
  header, `Authorization`/`X-Session-API-Key` included, to whatever host a `3xx`
  names, so a redirect leaked the key. `CloudClient` refuses *any* redirect
  (`_NoRedirect`); `OpenHandsClient` follows one only while scheme, host and port
  all stay identical (`_SameOriginRedirect`, via `_origin`, which normalises
  default ports and refuses an unparsable one). `OPENHANDS_CLOUD_URL` is also
  validated as `http(s)` at construction, because `urlopen` would otherwise
  honour `file://`.
- **Login had no ceiling that a header could not move.** The durable lockout is
  keyed by (username, IP), which a caller defeats by varying the username.
  `_LoginThrottle` adds two in-memory ceilings: per IP
  (`LOGIN_MAX_ATTEMPTS_PER_IP`, 30/min) and endpoint-wide
  (`LOGIN_MAX_ATTEMPTS_GLOBAL`, 120/min). The global one is the backstop for a
  platform that overwrites `X-Forwarded-For`, so it must not depend on the IP.
  `purge_stale_login_attempts` (swept from the loop by `_maybe_sweep`, along with
  `purge_expired_sessions`) keeps `login_attempts` and `sessions` from growing
  forever. Tests: `tests/test_auth.py` (proxy/lockout section),
  `tests/test_dispatcher.py` (redirect section),
  `tests/test_cloud_integration.py` (base URL / redirect).

## Operator controls and the workspace check (PR #4; historical)

- Workflow lifecycle actions live in `cloud_store` (`cancel_workflow`,
  `resume_workflow`, `retry_task`) with routes in `web_server` and buttons in
  the detail panel of `web_ui`. The UI renders only the actions the current
  state can accept, so there are no buttons that fail by design. If you add an
  action, gate it the same way rather than relying on the server's 409.
- `cancel` is terminal and idempotent. It does **not** delete the Sr
  conversation: the transcript is the record of why work stopped. It does
  cancel in-flight child tasks, otherwise the Jr dispatcher keeps polling
  conversations for an abandoned workflow.
- `resume` requeues only children with attempts left (`attempt < max_attempts`).
  Resuming a workflow is not consent to spend an attempt a task does not have.
  A `completed`/`cancelled` workflow is refused with 409, never silently
  no-op'd.
- `retry_task` goes through `force_transition`, so a state with no legal route
  back to `queued` is refused instead of forced.
- `rename_sr` PATCHes the real Cloud conversation (`update_conversation`)
  *before* reporting success, so local and Cloud state cannot disagree. The
  workflow goal is never changed by a rename.
- `CloudClient.web_url` derives the browser URL from the API URL Cloud returns
  (`.../api/conversations/<id>` -> `<base>/conversations/<id>`). The UI read
  `w.sr_url` for a long time while the server never sent it, so the "open in
  Cloud" link silently never rendered; `workflow_detail` now sends it.
- **Workspace check, not a proven cross-process lock.** `claim_task` refuses (by id) or
  skips (when scanning) a candidate whose workspace is occupied. Without it two
  conversations edit one checkout and the loser's commits vanish with no error.
  Occupancy is the states `assigned|running|verifying|blocked|recovering` --
  `queued` must NOT count, or two queued siblings block each other before
  either starts. The check alone can race for different rows; see NEXUS-SPEC.
- Live verification (2026-10-20): `PATCH /api/v1/app-conversations/{id}` really
  renames (created a throwaway conversation, renamed it, confirmed the title,
  deleted it). A missing id gives `404 {"detail":"unknown_app_conversation"}`,
  which is how you tell the route exists but the resource does not.
- Render deploy state (checked 2026-10-20, corrected). The live service is
  `python-dispetcher-1.onrender.com`, behind Cloudflare (`server: cloudflare`,
  `cf-ray`) with Render's own edge (`x-render-origin-server`). The deployed
  `static/app.js` is byte-identical to `main` at the merge of PR #4
  (md5 `744dcf86b64982c6d28798c99137d126`, and it contains the cancel/resume UI
  that PR #4 added), so **a deploy did happen after PR #4** -- the earlier note
  claiming otherwise was wrong. Caveat: the live process reports Python 3.14.8
  while `render.yaml` pins `PYTHON_VERSION=3.12.6`, so the running service was
  likely created by hand in the dashboard rather than from the blueprint.
- PR #5 and PR #7 change **nothing observable without a session**: no
  `web_ui.py`/`static` change, and the login page and the unauthenticated 401s
  are identical before and after. So a deploy of them can be neither confirmed
  nor ruled out from outside; `GET /healthz` and the headers look the same
  either way. Confirm the running commit from the System screen
  (`/api/system` -> `build.commit`, which reads `RENDER_GIT_COMMIT`) after
  signing in, or from the Render dashboard.
- There is no `RENDER_API_KEY` or deploy hook in this environment, so a deploy
  cannot be triggered from here. Rebuilding by hand in the Render dashboard is
  the reliable path.
- **Server-side prepared statements are off by default** (`prepare_threshold=None`
  in `PostgresStore`). Do not re-enable them behind a pooler. The failure they
  cause needs a restart to appear, which is why it read as a random startup
  crash: a start runs one statement past psycopg's default threshold (5), the
  pooler keeps the server session, and the next start prepares the same
  generated name and dies with `DuplicatePreparedStatement: prepared statement
  "_pg3_0" already exists` inside `apply_migrations`. A fresh schema hides it,
  because the migration DDL makes psycopg send `DEALLOCATE ALL`; reproduce it on
  a *warm* schema. `tests/test_schema_bootstrap.py` has the end-to-end case,
  gated on `TEST_POOLER_DSN` (point it at a PgBouncer in `pool_mode=transaction`
  with `max_prepared_statements=0`; newer PgBouncer manages prepared statements
  itself and masks the bug, Supabase's pooler does not).
- Tests: `tests/test_workflow_actions.py` (actions, auth, `sr_url`, and the
  malformed-id 404 block) and the workspace-conflict block at the end of
  `tests/test_reliability.py`. The fake Cloud implements `PATCH`/`DELETE` so
  rename runs over real HTTP.
- **Sr lifecycle (PR #10).** One workflow must have exactly one Sr, and a
  childless workflow must not wait forever. Two traps, both proven:
  - `SrRuntime.ensure_sr` recovers in strength order: recorded
    `sr_conversation_id` -> recorded `sr_start_task_id` (`_resume_start_task`,
    the same recovery the Jr path does with `external_start_task_id`) -> tagged
    orphan search. **Never swallow a transient error from the orphan search**:
    `_find_orphan_sr` used to catch every `CloudError`, so a search outage read
    as "no orphan" and started a duplicate Sr. It now lets `CloudUnavailable`
    propagate so the retry backs off.
  - `WorkflowVerifier` blocks a childless workflow after
    `sr_idle_timeout_seconds` (default 1800s) using the durable
    `workflows.sr_started_at`, not `updated_at` (which moves on every event).
    Without it, an Sr the Cloud never advances past `idle`/`running` left the
    workflow non-terminal forever with no operator signal.
  - `bind_sr` stamps `sr_started_at` only when it first records a conversation
    id; a later re-bind must not reset the clock. `resume_workflow` clears it
    when Sr is gone.
  - The fake Cloud withholds `app_conversation_id` until the start task is
    READY, matching the live API. A test that sets `start_polls_required > 0`
    therefore exercises the real two-phase start; a caller that binds before
    READY is caught.
  - Regression tests: `tests/test_sr_lifecycle.py` (12 cases, including the
    duplicate-Sr reproduction and the full one-workflow->one-Sr->one-Jr
    acceptance chain).


## Branch topology and the unmerged nexus branch

- `main` is the integration branch and is green: `ruff check .` passes and the
  full pytest suite passes against a disposable PostgreSQL (`TEST_SUPABASE_DB_URL`).
  Verified 2026-11-08: `main` 382 passed / 1 skipped; the nexus branch 445 passed
  / 1 skipped.
- All five `arena/*` branches are already merged into `main` through PRs #13–#18
  (`arena/06dcb2d2` docs+NEXUS spec, `arena/22525405` NEXUS projects/goals,
  `arena/1d4f42a1` versioned DAG plans, `arena/5a479709` installable PWA,
  `arena/60148d7e` project file uploads + GitHub confirmation). Their tips are
  ancestors of `main`, so there is nothing left to merge from them.
- **`nexus/laboratory-files-github` is deliberately not merged.** It is not a
  fast-forward and not a clean merge: it and `main` implement the *same* feature
  twice, so a merge is a design choice, not a conflict resolution.
  - Both add `migrations/0011_*` with an **incompatible** `nexus_files` table
    (`main`: `name/size integer/sha256/content bytea/created_by`, 5 MiB cap;
    nexus: `filename/content_type/size bigint/sha256/data bytea/is_archive/
    uploaded_by`, 32 MiB cap) and both add `nexus_workspaces`/`nexus_workspace_files`
    on the nexus side. Applying both files would collide on the table name.
  - Both add a file service (`main` `nexus_files.py` FileService; nexus
    `file_lab.py` FileLabService) and both wire routes into `web_server.py`
    (main: `/api/github/prepare|confirm` + `GITHUB_ENABLE_WRITES`; nexus:
    `GitHubWriter`, `/api/v1/projects/{id}/github/*`, `nexus_actions.py`
    confirmation handshake).
  - Merge conflicts: `.env.example`, `AGENTS.md`, `README.md`,
    `orchestration/github_client.py`, `orchestration/web_server.py`,
    `orchestration/web_ui.py`, `tests/test_nexus_projects.py`.
  - The nexus branch is the superset in scope (archive safety with Zip-Slip/bomb
    rejection, development workspaces, voice notes, multipart parsing, a
    two-step action confirmation handshake, extra GitHub reads). `main`'s
    version is smaller and adds a GitHub `GITHUB_ENABLE_WRITES` gate instead.
  - Do **not** merge it by blindly taking one side: choosing nexus drops main's
    `github_confirmations.py`, `nexus_files.py`, `0011_nexus_files.sql` and their
    three test files; choosing main drops the whole nexus laboratory. The safe
    path is to pick nexus as the base (it is the broader implementation), then
    port any main-only behaviour and its tests, and renumber the migration.
    Confirm the choice with the operator before merging.
- `tools/delete_cloud_sessions.py` and `tools/classify_cloud_sessions.py` are
  the only guarded Cloud-conversation deletion paths; keep them CLI-only.

## D-2 project execution binding and the Evidence Trust Engine (MVP)

Added on `nexus/mvp-integration`. These are the invariants to keep.

- **Migration `0014_nexus_project_binding.sql` is additive.** It adds
  `nexus_project_repositories` (project↔repo many-to-many), nullable
  `project_id` on `tasks`/`workflows`, the canonical repository-identity trigger,
  and `nexus_evidence`. `0012`/`0013` are intentionally reserved for the
  deferred File Lab (D-1). The runner applies every `*.sql` in lexical order and
  does not require a contiguous sequence, so the gap is safe. **Never backfill
  legacy rows**: a task/workflow/evidence with no project stays unscoped.
- **`tasks.repository_id` is canonical.** A `BEFORE INSERT/UPDATE` trigger
  (`nexus_tasks_check_binding`) derives `selected_repository` from it, resolves
  it from the allowlist when only `selected_repository` is given, refuses a
  conflict, refuses a cross-project repository, and refuses a task whose
  workflow is in a different project. Do not "simplify" this to a Python check:
  a direct insert must be refused too.
- **`authorize_dispatch` is the single dispatch gate** (`nexus_binding.py`). It
  is re-run immediately before a conversation is created and before a task is
  queued, for the dispatcher, the web API, the CLI and MCP. The global allowlist
  is necessary but never sufficient for a project-scoped task. A failing check
  dead-letters the task with the reason as evidence — never a silent skip.
- **The Evidence Trust Engine is real crypto** (`evidence_trust.py`). Ed25519
  over a canonical encoding; a trusted keyring by `key_id`; revoked keys never
  verify. **Only a trusted asymmetric signature yields `VERIFIED`; HMAC can
  never produce it.** `UNVERIFIED` for unsigned/legacy/unknown-key/expected-
  commit-mismatch; `FAILED` for a tampered signed field; `BLOCKED` without the
  crypto library.
- **Project-scoped evidence signs `project_id`** (format v2). The exact signed
  bytes are stored in `signed_payload` and verification reads them verbatim, so
  editing the `project_id` column afterwards fails verification. Legacy records
  are format v1 and unscoped; never give them a fabricated project.
- **Project isolation is a 404, not a 403.** A non-member reading another
  project's resource gets "not found". `tests/test_route_auth.py` fails if a
  protected endpoint is added without `_require_read`/`_require_write`.
- **The CLI and MCP are thin clients.** `orchestration/cli.py` calls the HTTP
  API and never prints a secret; `orchestration/mcp_server.py` speaks MCP over
  stdio with no shell/SQL/eval tool and re-checks membership on every write.
  Streamable-HTTP MCP is not implemented; do not claim it is.
- **`/readyz` checks the database**; `/healthz` stays a one-line liveness probe.
  Both are public and expose no configuration.
- **Docker Compose doubles `$` in `.env`.** Compose interpolates `$` in an
  `env_file`, so `AUTH_PASSWORD_HASH` (which contains `$`) must be written with
  `$$` or the server refuses to boot with a clear "N separators" message.
- Tests: `test_project_binding.py`, `test_evidence_trust.py`,
  `test_dispatch_authorization.py`, `test_route_auth.py`, `test_cli_mcp.py`,
  `test_browser_e2e.py`, `test_migration_d2.py`. `docs/MVP_RELEASE_REPORT.md`
  states what is verified and what still needs a live OpenHands key.

## Additional MVP invariants (round two)

- **`OPENHANDS_DIRECT_REPO_MODE` governs repository selection on the direct
  transport.** The sandbox agent-server create call has no `selected_repository`
  field; the workspace starts empty. `prompt` (default) prepends a deterministic
  clone instruction, `refuse` raises so the task dead-letters, `allow` is the old
  silent-ignore behavior. `CloudClient.supports_repository_selection` is `True`,
  `CloudDirectClient`'s is `False`; both are surfaced in `/api/config` and the
  System screen. Never go back to silently ignoring the field.
- **The workspace lock is the database index, not the check.** Migration `0015`
  adds `uq_tasks_workspace_active` (partial unique on
  `(selected_repository, COALESCE(selected_branch,''))` over the active states).
  `claim_task` claims inside a `SAVEPOINT` and treats a `UniqueViolation` as
  "workspace taken". The `_workspace_holder` read is only the fast path.
  Repository-less tasks never contend (NULL is distinct in a unique index).
- **The sandbox session-key tables have RLS.** Migration `0016` enables RLS and
  revokes PUBLIC/anon/authenticated grants on `conversation_routes` and
  `cloud_sandboxes`. They hold a live `session_api_key`; do not add a client
  policy. The app connects as the owner and bypasses RLS.
- **MCP has two transports.** stdio (`python -m orchestration.mcp_server`) and
  Streamable HTTP at `POST /mcp`, which reuses the web server's session/CSRF or
  bearer auth. A notification returns 202, a parse error 400. The tool surface
  re-checks project membership either way.
- **`tests/test_backup_restore.py` is the restore drill.** A real
  `pg_dump`/`pg_restore` into a fresh database; keep it passing — it is the
  automated proof that a dump restores.
- **Migrations are now `0001`–`0011`, `0014`, `0015`, `0016`.** `0012`/`0013`
  stay reserved for the deferred File Lab. The runner applies `*.sql` in lexical
  order; a gap is fine.
