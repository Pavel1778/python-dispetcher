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
- `orchestration/restore_point.py` — safe restore points. A restore point is a
  **git bundle** of every ref plus a JSON record (HEAD, branch, dirty state,
  SHA-256) under `.restore-points/` (gitignored, never pushed). Create, list and
  verify through `nexus restore-point …`; it is local and needs no API. See
  "Safe restore points" below.
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
- The UI is the studio in `orchestration/studio/` (see "NEXUS Studio" at the end
  of this file): static files, Russian, dark and light, responsive. It must not
  embed any secret; every control must call a real endpoint and say accurately
  what it does.

## NEXUS laboratory, files, voice and guarded GitHub (second increment)

- `orchestration/archive_safety.py` is the only place an uploaded archive is
  read. It refuses traversal, absolute paths, drive letters, UNC, symlinks,
  hardlinks, device/FIFO entries and bombs (entry count, total/per-file size,
  compression ratio) **before any row is written**. It never writes to disk;
  it yields validated `(path, bytes)` pairs. Do not "stream" extraction to the
  filesystem later -- the whole point is that no member name becomes a path.
  The ratio ceiling only applies above `ratio_floor_bytes` (1 MiB), so a tiny
  file with a big ratio is not rejected for nothing.
- `orchestration/file_lab.py` stores uploaded bytes in PostgreSQL
  (`nexus_files`, `nexus_voice_notes`, `nexus_workspace_files`), never on the
  server. A filename is metadata: directory separators are stripped, and a
  workspace path is normalized and re-checked by a DB `CHECK`. `original_data`
  keeps the pre-edit bytes so `workspace_diff` shows the imported version.
- `orchestration/nexus_actions.py` is the confirmation handshake for actions
  that must not happen on one click. Only `SHA-256(token)` is stored; the token
  is returned exactly once. A retried confirm is `409` (never a second side
  effect). Expiry is checked at confirm time.
- `orchestration/github_client.py`: `GitHubClient` reads and also carries the
  legacy ``create_repository``/``set_visibility`` used by the confirmation flow;
  `GitHubWriter` is the guarded writer for the Integrations tab.
  `set_repository_visibility(private=False)` raises unless
  `acknowledge_public=True`, which only the confirmed-action handler sets -- so
  even a caller that forgot the confirmation step cannot publish a repository.
  `gh.writes` on the fake server proves an unconfirmed action never left.
- `orchestration/multipart.py` exists because `cgi` was removed in Python 3.13.
  It does not honour `filename*=`, only the plain `filename="..."` form.
- **Trap:** `Handler._json()` returns `None`. A dispatcher that returns its
  result as the "handled" signal therefore reports "not handled" after already
  writing a response, and the caller sends a second response (a 404 after a
  201). `_post_project_resource` returns an explicit `bool` for this reason; do
  not collapse it back to returning `self._json(...)`.
- The UI had **Лаборатория** and **GitHub** tabs for these. The studio removed
  both tabs; the routes below are unchanged and are reachable through the API,
  the CLI and MCP. The create/visibility flow is still two-step on the server
  (propose -> confirm): nothing is sent to GitHub before the confirmation.
- **Two upload designs share `nexus_files` and `/files`.** The unified
  `migrations/0011_nexus_files.sql` makes the table a superset: an opaque row
  (main) carries `name`/`content`/`created_by`; a File Lab row carries
  `filename`/`data`/`uploaded_by`/`content_type`/`is_archive`. `App` holds both
  services (`files` = `FileLabService`, `opaque_files` = `FileService`).
  `GET /files/{id}` disambiguates by provenance (`FileService.is_opaque`):
  an opaque row is a raw attachment, a File Lab row is a JSON preview. Do not
  collapse this to one service without reworking both contracts and their tests.
- Tests: `tests/test_archive_safety.py` (no DB; every rejection by name),
  `tests/test_file_lab.py` (real HTTP + PostgreSQL + `tests/fake_github.py`),
  `tests/test_lab_files.py` (the opaque design), and the extended
  `tests/test_github_client.py`. The fake GitHub now implements
  `POST /user/repos`, `PATCH /repos/{owner}/{name}` and the detail sections, and
  records writes in `gh.writes`.
- `tests/test_nexus_projects.py::store` truncates **every** table that
  references `nexus_projects`. Adding a new project-scoped table means adding
  it there too, or the truncate fails with a foreign-key error.

## GitHub repository discovery and confirmed writes

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
  `resume_workflow`, `retry_task`) with routes in `web_server` and buttons on
  the studio's «Агент» tab. The UI renders only the actions the current
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
- **Migrations are `0001`–`0011`, `0014`, `0015`, `0016`.** The runner applies
  `*.sql` in lexical order; a gap (`0012`/`0013`) is fine. File Lab's tables
  were folded into `0011_nexus_files.sql` when that branch was integrated, so
  the gap is intentional and permanent.

## LLM orchestrator cascade (provider failover)

- `orchestration/providers.py` holds ordered orchestrator adapters: Gemini
  (default), Groq, OpenRouter, then OpenHands. `ProviderCascade.plan` returns
  the first provider that answers with a valid plan. Only configured providers
  are tried; an unconfigured one is skipped, not failed.
- **Failover is for provider/transport problems only.** 401/403, 400/422 and a
  malformed body fail that provider immediately (retrying a bad key only burns
  quota); transport errors, 5xx and 429 are retried with bounded exponential
  backoff and honour `Retry-After`. A *coding* result is never a failover
  reason -- that decision belongs to the executor/verifier path.
- **Orchestrator ≠ executor.** These adapters only *propose* a plan as JSON;
  OpenHands remains the executor. `OpenHandsOrchestratorProvider.executor` is
  `False` and it posts a conversation, not a chat completion -- never let it
  execute a plan it produced.
- Keys are read from the environment (`GEMINI_API_KEY`, `GROQ_API_KEY`,
  `OPENROUTER_API_KEY`, `OPENHANDS_API_KEY`), never logged, returned, or put in
  an exception message; `Provider.__repr__` is redacted. `/api/system` reports
  provider *presence* only.
- The cascade is reachable at `POST /api/v1/projects/{id}/goals/{gid}/plan-draft`
  and via the **«Черновик через ИИ»** button on the plan panel. It **stores
  nothing and dispatches nothing**; the draft must be saved and approved as
  before. A provider outage is a `503` with the per-provider reasons, never a
  fabricated plan.
- Tests: `tests/test_providers.py` (real local HTTP server per provider shape),
  `tests/test_plan_draft.py` (real web server + PostgreSQL).

## Provider fallback diagnostics (this session)

The cascade above had no runtime *diagnostic*: `openhands_diagnostics` verified
OpenHands accounts, the pool, Cloud, queues and the worker, but nothing checked
the Gemini/Groq/OpenRouter/OpenHands-orchestrator providers. Section 8 of the
acceptance text asks for exactly that, so it was added. Keep these invariants:

- **`Provider.probe()` is the one real check.** It makes a *read-only* request
  (`_models_path()` — `/models` for Gemini/Groq/OpenRouter, the conversation
  *count* for the OpenHands orchestrator) and returns a `ProbeResult`, never
  raising: `ok` (key accepted, and the configured model is in the key's list
  when the endpoint lists models), `auth` (401/403), `unavailable` (transport,
  5xx, 429), `rejected` (400/422 or the model is not available), `blocked` (no
  key — nothing was proved). `ProviderCascade.probe_all()` probes every provider
  in cascade order. **Never send a prompt from a probe**: it costs tokens and
  could be mistaken for a plan.
- **Two checks, two claims.** `check_providers` reports *configuration* by
  presence only (names + order, no call, no key) — `VERIFIED` there means
  "configured", which is all it claims. `check_provider_probe` is the earned
  one: opt-in with `live=True`, `UNVERIFIED` otherwise, and it maps each
  `ProbeResult.kind` to `VERIFIED`/`FAILED`(auth, rejected)/`BLOCKED`
  (unavailable). A provider is **never** reported connected without this real
  call — the request's "do not declare a provider connected without a real
  check".
- **Model switching ≠ account switching.** The provider cascade is a different
  mechanism from the OpenHands account pool; the checks are separate and the
  detail text says which is which. Do not fold them together.
- **No key ever leaves.** `ProbeResult` and the report carry the provider name,
  model and a non-secret detail; the `/api/providers` route and `nexus
  providers` never return a key (asserted in the tests).
- Surface: `GET /api/providers[?live=1]` (`App.provider_status`) and the CLI
  `nexus providers [--live]` (exit 6 when a key was refused or a model is
  missing). The checks are also part of the `POST /api/openhands/diagnostics`
  report.
- Tests: `tests/test_provider_diagnostics.py` (25 cases over real local HTTP
  servers: config presence, key/model checks, timeouts, switching, context
  preservation, return to primary, the two diagnostic checks, no key leak) and
  the provider block in `tests/test_openhands_pool_http.py`.

## Autonomous lab (context, memory, runs, worker)

- `orchestration/nexus_context.py` — versioned project-context snapshots and the
  unified memory. `analyze_tree` is a pure function (no I/O); memory carries a
  `provenance` label and only `test_verified`/`independently_verified` count as
  verified. A repeat memory update only strengthens provenance; an agent cannot
  downgrade a verified fact by re-asserting it. Migrations `0017`.
- `orchestration/nexus_analysis.py` — reads a repository tree + a few files
  through the read-only GitHub client to build a context summary. It never asks
  a model and never writes. `0019` adds the tree/contents reads the client uses.
- `orchestration/nexus_lab_store.py` — runs, run tasks and notifications. The
  invariants live here: one active run per goal (partial unique index), atomic
  claim with a lease, **a task is claimable only when every dependency is
  `completed_verified`** (computed in SQL, one row locked at a time — locking the
  whole pending queue was a real bug the concurrency test caught), lease
  recovery, and `set_run_state_internal` for the worker (which is not a project
  member). Migration `0018`.
- `orchestration/nexus_planner.py` — validates a provider plan (ids, deps,
  cycles, unknown refs) before storing it. `PLANNER_SYSTEM` asks for JSON only.
- `orchestration/nexus_lab.py` — `OpenHandsExecutor` (dispatch + poll; **never**
  decides completion), `GitVerifier` (checks the *real* remote branch/commit; a
  finished conversation with no observable commit is `completed_unverified`) and
  `LabScheduler` (reclaim → claim → dispatch → poll → verify → record → decide).
  A run completes only when every task is `completed_verified`; an unverified
  dependency deadlocks the graph and the run is `blocked`, not left spinning.
- `orchestration/lab_worker.py` — the loop that advances runs. It holds no
  per-run state, so a restart is a non-event: it re-reads the active runs and
  continues. The web server runs it on a thread of its own (`App._run_lab`)
  unless `NEXUS_LAB_INLINE` is `0`/`false`/`no`/`off`; `python -m
  orchestration.lab_worker` runs the same loop as a separate process, and the two
  may run side by side (every claim is a lease). See "Autonomous runs on the
  deployed service" at the end of this file.
- **Two-phase OpenHands start is awaited.** `POST /api/v1/app-conversations`
  returns a start task; the conversation id appears only at `READY`.
  `OpenHandsExecutor.dispatch` polls until READY and returns the real
  `app_conversation_id`. A live dispatch proved the earlier version returned the
  start-task id and then polled it as a conversation ("missing"). Do not remove
  the wait.
- Routes: `POST …/context/refresh`, `POST …/goals/{gid}/run`,
  `POST …/runs/{id}/{pause,resume,cancel}`, `GET …/runs|runs/{id}|context|memory|notifications`,
  plus `GET/POST /api/github/accounts` and `POST …/github/account`.
- UI: the **Автономная лаборатория** tab drives the whole loop and shows
  run/task state, context, memory and notifications. Status badges distinguish
  running/waiting/blocked/failed/completed/verified; the UI never fakes progress.
- Tests: `tests/test_context_memory.py`, `tests/test_lab_store.py`,
  `tests/test_lab_loop.py`, `tests/test_planner_worker.py`,
  `tests/test_lab_http.py`, and the autonomous journey in
  `tests/test_browser_e2e.py`.

## GitHub multi-account

- `orchestration/nexus_github_accounts.py` — one account per row, the PAT
  **Fernet-encrypted** with `NEXUS_SECRET_KEY` and never returned by any method.
  Identity is verified via `GET /user` before the account is trusted; a rejected
  token marks the account `invalid`. A project binds to exactly one account and
  `client_for_project` **never falls back** to another. Migration `0019`.
- Without `NEXUS_SECRET_KEY` the service refuses to store an account rather than
  write a plaintext token.
- Tests: `tests/test_github_accounts.py` (includes the never-leak-the-token
  assertion and the HTTP routes).

## Client packages

- `packaging/debian/build-deb.sh` — real `.deb` via `dpkg-deb` (launcher,
  desktop entry, icons, postinst/postrm). Verified by `dpkg-deb --info/--contents`
  and a real `dpkg -i`/`dpkg -r`. `tests/test_deb_package.py`.
- `packaging/android/build-apk.sh` — real signed APK via the SDK tools
  (aapt2 → d8 → zipalign → apksigner), no Gradle. `MainActivity` is a native
  WebView around the same dashboard; it stores only the backend URL. **Use
  build-tools 35**: 34's d8 crashes on the inner `WebViewClient` class. Verified
  by `apksigner verify`, `aapt2 dump badging` and `apkanalyzer dex packages`;
  **not runtime-verified** (no `/dev/kvm`, so no emulator). `tests/test_android_apk.py`.
- `packaging/windows/build-exe.sh` — real PE32+ GUI `.exe` cross-compiled with
  mingw-w64, with an embedded `VERSIONINFO` resource. Verified by reading the PE
  headers and `.rsrc`; **not runtime-verified** (no Windows host).
  `tests/test_windows_exe.py`.

## Live verification (2026-11-09)

- **OpenHands**: `CloudClient.health()` is `True`; a real conversation ran
  `running → finished` through `OpenHandsExecutor` and returned its report. The
  account has an LLM key but **no GitHub provider token**, so OpenHands cannot
  clone a repository on its own.
- **GitHub**: `GET /user` and repository analysis work against the real API; the
  real repo's head was read as `556734732edd`. Multi-account add/verify worked
  with the real PAT and did not leak it.
- **Supabase**: the only project (`xwooledhoyuaaxkccnjd`) is **INACTIVE**; the
  access token is valid but the database is not serving. Tests use a local
  PostgreSQL, not this project.

## NEXUS AI IDE transformation (branch `nexus/ai-ide-transformation`)

Turns the dispatcher dashboard into an AI development workspace. At the time the
old panels were kept as secondary tabs behind the sidebar. **The studio rewrite
(last section) removed them and this shell**; the services described here are
unchanged.

- **`orchestration/secrets.py` is the single credential cipher.** Every stored
  secret (a GitHub PAT, an OpenHands API key) is Fernet-encrypted with
  `NEXUS_SECRET_KEY`. A missing key is a hard refusal (`SecretsError`), never a
  plaintext write. `nexus_github_accounts` and `nexus_openhands_accounts` both
  use it; do not add a second crypto path.
- **`orchestration/nexus_openhands_accounts.py`** — one OpenHands account per
  row: label, base URL, transport (`direct`/`app`), optional default model,
  encrypted key, verified identity, discovered capabilities. At most one
  `is_default` per principal (partial unique index). `client_for_project` never
  falls back to another account or the process-wide key.
- **`nexus_github_accounts.client_for_account`** is what makes the repository
  picker multi-account: the repository list comes from the named account's own
  token. There is no cap on the number of accounts.
- **`orchestration/nexus_binding.py` — `ProjectBindingService`** is the project's
  explicit repository+branch binding. `set_repository` also writes the
  project↔repo link (the thing `authorize_dispatch` checks), so binding and
  authorization cannot drift. Changing a binding is always an explicit action,
  never a side effect.
- **`orchestration/nexus_conversations.py`** — persistent conversations,
  messages, attachments and agent sessions, all project-isolated (a non-member
  gets 404, not 403). A NEXUS conversation is **not** an OpenHands conversation;
  `nexus_agent_sessions` keeps that mapping durable.
- **`orchestration/nexus_chat.py`** — the chat brain. `ask`/`plan` use the
  provider cascade's text path; `code`/`autonomous` start real OpenHands work.
  A provider outage is an `error` message, **never** a fabricated answer; the
  user turn is persisted first so it survives an assistant failure.
- **`providers.py`** gained `complete_text`/`text` for prose. `complete` still
  reports the real `attempts` count (the retry loop returns `(text, attempts)`).
- **Migration `0020_nexus_ai_ide.sql`** is additive: `nexus_openhands_accounts`,
  `nexus_conversations`, `nexus_conversation_messages`,
  `nexus_conversation_attachments`, `nexus_agent_sessions`, and nullable
  `repository_id`/`selected_branch`/`openhands_account_id` on `nexus_projects`.
  No backfill. `REQUIRED_COLUMNS` in `cloud_store.py` names the new tables.
- **`orchestration/web_ui_ide.py`** held the shell's CSS and JS. It was deleted
  by the studio rewrite. The lesson it recorded still holds and is why the
  studio delegates every event from the document: **one missing element must not
  abort the whole interface** — a plain `$('#x').onclick` on a missing id did
  exactly that.
- **The Unicode fix.** `github_client.repo_path` percent-encodes every path
  segment as UTF-8; the raw form raised `UnicodeEncodeError: 'latin-1' codec`
  from `http.client`. The fake GitHub decodes percent-encoded segments the way
  GitHub does, so the regression tests are real. `multipart._decode_header_value`
  decodes a part header as UTF-8 first (falling back to latin-1).
- Tests: `test_github_multi_account.py`, `test_openhands_multi_account.py`,
  `test_conversations.py`, the IDE-shell block in `test_web_server.py`, and the
  chat journey in `test_browser_e2e.py`. Route-auth coverage was extended to
  every new endpoint.
- **Not delivered / limitations.** The IDE editor is read-only (files are read
  through the GitHub API at the bound ref); there is no local terminal because
  execution happens in OpenHands Cloud; the live multi-account and chat paths
  were not exercised against a real OpenHands key in this sandbox.

## NEXUS Studio (front-end rewrite)

The whole interface was replaced by one application: chat history on the left,
the conversation and its composer in the middle, a workspace on the right with
two tabs («Файлы», «Агент»). Settings and the repository connection are dialogs.
There are no dashboards, section tabs or forms outside that.

- **Files.** `orchestration/studio/`: `index.html` (shell + icon sprite),
  `login.html`, `app.css`, `boot.js` (theme before first paint), `markdown.js`,
  `highlight.js`, `app.js`, `favicon.svg`, `fonts/` (Inter and JetBrains Mono,
  latin + cyrillic, OFL-1.1 with the licence texts). No framework, no build step,
  no third-party script, nothing loaded from another origin.
- **`orchestration/web_ui.py` only reads those files.** `STATIC` is a closed
  mapping URL -> bytes; the server looks a path up in it and never maps a URL
  onto a directory. A new asset is a new line in `_FILES` **and** must match the
  `package-data` globs in `pyproject.toml` (`tests/test_studio_ui.py` checks
  both). Pages reference assets as `?v=<ASSET_VERSION>` (a content hash).
- **Content-Security-Policy.** `_html` sends `default-src 'self'; script-src
  'self'; style-src 'self'; ...`. Consequences that are easy to trip over:
  - no inline `<script>`, no `style="..."` attribute, no `on*=` handler, in the
    pages **or in HTML built in a string**. Set sizes through the CSSOM
    (`el.style.width = ...`), state through classes and attributes;
  - Playwright's `page.wait_for_function` does not work (it evaluates a string).
    Wait on selectors; `page.evaluate` is fine.
- **Untrusted text.** Model output and repository files reach `innerHTML` only
  through `markdown.js` / `highlight.js`, which escape every source character and
  emit a fixed set of tags; everything else goes through `esc()`. Links are
  http/https/mailto only; an image becomes a link (nothing is loaded
  cross-origin). `tests/studio_render_test.js` holds the hostile corpus.
- **Events** are delegated from `document` through `data-action` and the
  `ACTIONS` table in `app.js`. `tests/test_studio_ui.py` fails when a control has
  no handler, a handler has no control, or the script asks for an element id or
  an icon that does not exist. Keep new controls in that scheme.
- **Navigation** is `#/p/<project>/c/<conversation>`. `navigate()` routes at
  once; while a route is in flight the composer is held, so a message cannot land
  in the conversation being left.
- **Colour tokens** live at the top of `app.css` for both themes. The contrast of
  every text/surface pair is asserted (4.5:1) in `tests/test_studio_ui.py`; change
  a token and that test tells you which pair broke.
- **Server changes made for the studio** (`web_server.py`, `nexus_chat.py`,
  `providers.py`):
  - `GET /api/studio` — what is configured, booleans and names only; it never
    calls the Cloud client (unlike `/api/system` and `/api/config`);
  - `POST /api/v1/projects/{id}/repository` — `App.connect_repository`: look the
    repository and branch up on GitHub, extend the allowlist, bind. `account_id`
    is the identity: a uuid = that stored account, explicit `null` = the server
    `GITHUB_TOKEN` (and the account is unbound), key absent = keep;
  - `App.github_for_project` — project GitHub reads (`/github/repositories/...`,
    context refresh) use the project's bound account. A revoked bound account is
    a 400, never a silent switch to the server token;
  - `GET /api/github/accounts/{id}[/repositories[/{owner}/{name}/branches]]` and
    `GET /api/openhands/accounts/{id}[/capabilities]` — the handlers existed but
    `do_GET` never dispatched them, so they answered 404;
    `GET /api/github/repositories/{owner}/{name}/branches` is new;
  - a chat turn passes `project_id`/`principal` to the dispatch hooks. Before,
    `autonomous` always failed with `KeyError` and a `code` workflow was created
    outside any project (its project-scoped view was a 404);
  - `complete_text` no longer asks Gemini/Groq/OpenRouter for JSON output;
  - an untitled conversation is named after its first message; `PATCH
    .../conversations/{id}` accepts `archived` (the only way to restore one).
- **Features that have routes but no screen now:** lab file uploads, workspaces
  and voice notes; evidence; plan approval; GitHub create-repository and
  visibility confirmations; the legacy task form and the queue; the system
  diagnostics page; the allowlist editor; OpenHands account management.
- **Limits, stated in the UI where they bite.** Stored OpenHands accounts are not
  used for execution: workflows run under the server `OPENHANDS_API_KEY`
  (nothing calls `openhands_accounts.client_for_project`). Files are read-only
  and come from the GitHub API at the bound branch; the tree stops at 500
  entries and a file at 200 KB. A reply arrives as one response, not a stream.
  Autonomous runs are advanced by the server itself (see `lab.inline` in
  `/api/studio`); with `NEXUS_LAB_INLINE=0` they wait for
  `python -m orchestration.lab_worker`, and the run's card says so.
- **Tests.** `tests/test_studio_ui.py` (structure, packaging, contrast; no
  database), `tests/studio_render_test.js` (Node), `tests/test_studio_api.py`
  (the server changes, with the fake GitHub recording which token each read
  used), `tests/test_browser_e2e.py` (Chromium: the journey, hostile text,
  failure and retry, phone and five other widths). The browser tests take
  `E2E_CHROMIUM_EXECUTABLE` / `E2E_CHROMIUM_LD_LIBRARY_PATH` for a Chromium that
  is not Playwright's own; outside CI they skip when no browser starts.
- **Not verified.** A live OpenHands account, a real GitHub token and a real LLM
  provider: every test uses the local fake servers. Safari and Firefox were not
  run; only Chromium was.

## After the studio merge: CI, the database connection, and what is still open (2026-11-11)

This section records one working session. The "still open" list at its end is
the part to read first: those defects were reproduced and are **not fixed**.

### CI (`.github/workflows/tests.yml`)

- **The job runs on `ubuntu-26.04`, not `ubuntu-latest`.** While that label
  migrates it resolves to either 24.04 or 26.04, and they are not
  interchangeable here: the backup/restore drill dumps the PostgreSQL 17 service
  with the image's own client tools, and `pg_dump` refuses a server newer than
  itself. 24.04 ships `pg_dump` 16 (`test_backup_restore` fails with "server
  version mismatch"), 26.04 ships 18. Both were run side by side: 26.04 gave
  732 passed / 4 skipped, 24.04 gave that one failure.
- **No system packages are installed up front.** `playwright install
  --with-deps` installs Chromium's libraries through apt before it downloads the
  browser, which made every run depend on the package mirrors. On 2026-11-11
  that part did not finish: three attempts on `main` right after PR #25 was
  merged failed after 7-10 minutes, and when the step was split, `playwright
  install-deps` ran into its 8-minute limit on both images while the Python
  packages had installed in 7 seconds. Which apt command hung was not
  established (the log could not be read). The images already have the
  libraries, so the job downloads Chromium, starts it once
  (`tools/ci_browser_probe.py`, the same launch options as
  `tests/test_browser_e2e.py`) and falls back to `playwright install-deps` only
  if the browser did not start. That fallback is the old command and was not
  exercised: the browser started on both images.
- **One command per step, each with a time limit**, so the step list says which
  command failed.
- **The outcome is written as annotations**: the runner image, the Chromium
  build, pytest's summary line, failed test ids, and the reasons for skips. The
  log of a run is not always retrievable (it expires; from a sandbox the log
  host may be unreachable), the annotations are:
  `gh api repos/<owner>/<repo>/check-runs/<job id>/annotations`.

### The store replaces a dropped connection (`orchestration/cloud_store.py`)

- `PostgresStore` holds **one** connection for the life of the process, and the
  driver never reconnects by itself. Before this change, once the server end
  went away (a database restart, a pooler recycling its clients, a network
  reset) every call failed with `the connection is lost` until the process was
  restarted, while `/healthz` kept answering 200.
- `_tx()` now checks the connection before each transaction. A healthy idle
  connection has nothing waiting to be read, a dropped one does, so the check is
  one `poll` and no round trip; only when input is waiting is the connection
  asked (`SELECT 1`). A dead connection is replaced with one opened with the
  same arguments (row factory, prepared-statement policy).
- The check replaces a connection **only on evidence**. If it cannot be made, the
  connection is used as before. It can add a reconnect; it must never take a
  working connection away (`tests/test_store_reconnect.py` holds both
  directions, including a notification arriving on a healthy connection).
- A call that is **in flight** when the connection dies still fails, once; the
  next call reconnects. Nothing is retried on the caller's behalf: a statement
  that may already have run must not be run twice.
- When the database cannot be reached the store raises `StoreUnavailable`, the
  HTTP layer answers `503 {"error": "database unavailable"}` (never the driver's
  text, which names hosts and ports), and further attempts are refused for
  `RECONNECT_BACKOFF_SECONDS` (2 s). Every caller waits on the same lock, so
  without the back-off an outage becomes a queue of requests each spending the
  whole connect timeout.

### Two smaller fixes

- `PostgresStore.transition()` returns **the row it wrote** (`UPDATE ...
  RETURNING *`). It used to read the task again after committing, which answers
  "what is it now?": with the dispatcher loop running, a retry reported
  `assigned` instead of `queued` about one time in seven, and that was the
  intermittent failure of `test_workflow_actions.py::test_retry_route`.
- `CloudDirectClient.base_url` exists. `/api/system` and `/api/config` read
  `client.base_url`; the direct transport (the default) had no such attribute,
  so both answered 500. (The same commit is in PR #23.)

### Still open - reproduced, not fixed

> **Status, 2026-11-12.** Items 1-4 and 6 were fixed in PR #27 (the gate is
> `authorize_run_task`, called when a run is started and again before every
> task; `LabScheduler.tick` reads the run's state; `claim_task` enforces the
> iteration budget and the deadline; transitions go through
> `_RUN_ENTERED_FROM`; the login-ceiling test no longer depends on the clock).
> Item 5 is fixed by the section "Autonomous runs on the deployed service"
> below. Item 7 is still open. The descriptions are kept as the record of what
> was wrong.

1. **An autonomous run may target a repository nobody allowed.**
   `POST /api/v1/projects/{id}/goals/{goal}/run` takes `repository` and `branch`
   from the request body and stores them on the run tasks; `LabScheduler` /
   `OpenHandsExecutor.dispatch` start the conversation without
   `authorize_dispatch`. Reproduced: a run for `someone-else/private-thing`,
   branch `prod`, with an **empty** allowlist and no project link, was accepted
   (201) and its first task reached the Cloud start call. The chat's
   "autonomous" mode passes the project's bound repository, which was checked
   when it was connected; by the code it is not checked again at dispatch, so a
   repository disabled afterwards would still be used (not reproduced). The
   dispatcher path (`cloud_dispatcher.py`, `mcp_server.py`) does call
   `authorize_dispatch`; the lab path has to as well.
2. **Pausing a run does not pause it.** `LabScheduler.tick` never reads the
   run's state. Reproduced: after `.../runs/{id}/pause` the next tick claimed and
   dispatched a task, and the "paused" run went on to `completed`. By the code, a
   cancelled run differs only in that the worker no longer selects it
   (`ACTIVE_RUN_STATES`); a conversation already started is not stopped.
3. **Run budgets are stored and never enforced.** `max_iterations`,
   `deadline_at`: nothing reads them, and `bump_iteration` has no caller (the UI
   shows "итерация 0 из 50" for ever). Reproduced: `max_iterations=1` with a
   deadline one second in the past ran every task to the end.
4. **Any run state can be set from any other.** `pause` / `resume` / `cancel`
   write the state unconditionally: a cancelled run can be resumed (`cancelled`
   -> `running`), and its goal has already been marked cancelled.
5. **`render.yaml` starts only the web server.** Autonomous runs advance only
   under `python -m orchestration.lab_worker`, so on the deployed service a run
   stays queued. `DEPLOY.md` describes the second service; the blueprint does
   not define it.
6. **`tests/test_auth.py::test_global_ceiling_holds_when_the_ip_is_untrustworthy`
   is load-sensitive.** It needs 120 failed logins inside one 60 s window; on a
   busy machine the loop takes longer and the test fails. Same on `main` before
   any of this.
7. **Documents that still describe the interface removed by PR #25:**
   `README.md` ("Quick start", "What is ready today"), `docs/AUTONOMOUS-LAB.md`,
   `docs/NEXUS-AUDIT.md`, `docs/NEXUS-SPEC.md`, `docs/MASTER_COMPLETION_AUDIT.md`,
   `docs/MASTER_COMPLETION_ROADMAP.md`. They were not rewritten in this session.

## OpenHands account pool, failover and diagnostics (this session)

Added to make NEXUS drive several OpenHands accounts, switch executors on
failure, and prove the integration with one command. The invariants to keep:

- **`orchestration/openhands_pool.py`** is the selection policy and the only
  place the four modes live (`manual`, `auto`, `failover`, `load_balancing`).
  "Usable" = `state='active'` AND `enabled` AND health not `unavailable`; a
  `degraded` account stays selectable. `manual` never falls back. Health and
  `active_tasks` are written by the callers that make real calls — observed, not
  guessed. Migration `0022` adds the columns and `nexus_openhands_pool`.
- **`orchestration/openhands_router.py`** chooses an account per task and builds
  the hand-off context. `tried_accounts` (on the task) is what makes failover
  terminate: an account that failed is never selected for that task again. The
  account a task last *ran* on is not excluded - a result that did not verify
  is tried again there - and a start the service refuses (429) is not a
  failure; both are item 13 of "Autonomous runs on the deployed service".
  `build_account_router` returns `None` when the principal has no stored
  account, so a single-key deployment keeps its exact previous behaviour.
- **A Cloud conversation cannot move between accounts.** A switch is a *new*
  conversation on the new account with the context passed in the first message
  (`handoff_message`). The old account is never polled again: `poll` uses
  `_client_for_task`, which is bound to the task's account. Migration `0024`
  records every switch in `nexus_task_handoffs`.
- **`OpenHandsExecutor.dispatch` raises `DispatchError`** carrying the account
  that failed, so the scheduler can exclude it. A failed *start* requeues the
  task (transport problem); a **vanished** conversation (the executor is gone)
  requeues via `requeue_for_failover` from `running` too, which
  `release_task` (dispatched only) could not do. When the pool has nothing left,
  the run is **blocked** with the reason — never spun.
- **`orchestration/openhands_diagnostics.py`** is the one diagnostic engine.
  Four statuses, earned: `VERIFIED` (ran and worked), `FAILED` (ran and failed),
  `BLOCKED` (could not run: no credential/resource — a 429 is BLOCKED, not
  FAILED), `UNVERIFIED` (not run — the live probe is opt-in). `report.ok` is
  true unless something FAILED. The `queues_leases` check does a real
  claim→release→re-claim on a throwaway project it deletes; the live probe
  creates and deletes exactly one conversation.
- **Worker heartbeat (migration `0023`).** `LabWorker` registers and heartbeats
  every tick (`register_worker`/`heartbeat`/`stop_worker`); `live_workers` is
  how the diagnostics tell a live worker from a dead one — a lease cannot.
  `lab_worker: FAILED` means active runs exist with no fresh heartbeat. The web
  server's own loop registers too, as kind `inline` (`App._run_lab`).
- **`OPENHANDS_DEFAULT_MODEL` is required in practice.** Verified live: a bare
  sandbox fails with `LLMAuthenticationError` (`classification.kind=auth`,
  retryable=false); with `openhands/deepseek-v4.1-flash` the same task finishes.
- **`render.yaml` and `docker-compose.yml` now define the worker service**, so a
  deployed autonomous run is not left queued (this closes the earlier open item).
  The web service advances runs as well, by default; on the live service it is
  the only one that does (`nexus_lab_workers` was empty on 2026-11-12).
- New endpoints: `GET/POST /api/openhands/diagnostics` (`live` opt-in),
  `GET/POST /api/openhands/pool`, and per-account `enable`/`priority`/`remove`.
  CLI: `nexus openhands accounts|pool|diagnostics [--live]`.
- Tests: `tests/test_openhands_pool.py`, `tests/test_openhands_failover.py`
  (the load-bearing end-to-end failover + hand-off), `tests/test_openhands_pool_http.py`,
  and the worker-heartbeat cases in `tests/test_lab_store.py`. Docs:
  `docs/OPENHANDS_SETUP.md`, `OPENHANDS_ACCOUNTS.md`, `FAILOVER.md`,
  `WORKER_SETUP.md`, `TROUBLESHOOTING.md`, `TESTING.md`.
- **Live verification (2026-11-11):** against the real key — account capabilities
  and health VERIFIED; the live conversation probe VERIFIED (create, message,
  events, a shell command echoed, workspace file upload + SHA-256 read-back) on a
  reused running sandbox; and a NEXUS run task dispatched through the pool
  router to real Cloud reached `finished` with the marker echoed. Creating a new
  sandbox returned `429 daily_sandbox_limit_reached` (account quota), reported
  BLOCKED. No second OpenHands key is available, so a *two-live-account*
  failover is BLOCKED; the failover logic is proven end to end against real
  PostgreSQL with honest transport fakes.

## Task texts of any size (PR #27, 2026-11-11)

An autonomous run used to be refused above 20 000 characters
(`text exceeds 20000 characters`: the limit of `nexus_goals.text`). The text of
a task is now a **specification**, stored apart from the goal.

- **Storage** (`migrations/0021_nexus_specs.sql`, `orchestration/nexus_specs.py`):
  `nexus_specs` holds the text verbatim with its SHA-256 (the database checks
  the pair), `nexus_spec_sections` / `nexus_spec_requirements` the index
  (sections `S-NNN`, addressable pieces `R-NNNN`, every non-empty line in
  exactly one piece), `nexus_spec_links` the cross-references,
  `nexus_spec_stages` the parts it is planned in. `nexus_goals.spec_id` points
  at it; the goal keeps a label. `nexus_run_tasks.spec_ref` says which lines,
  bytes and pieces a task was planned from. 0021 is applied in production:
  schema changes go into a new migration.
- **Planning** (`orchestration/nexus_spec_planner.py`): one part is planned
  inside the request; several are planned in the background (`App._run_planning`),
  each a row with a 90-second lease, so the state survives a restart. A
  transient provider failure is retried; "too large" or too many tasks splits
  the part; a permanent failure stops with the reason in the chat. What is
  covered is decided by the application, not the model: a piece that reads like
  an obligation and that the model left out gets a `verify` task.
- **Hand-over** (`orchestration/nexus_spec_delivery.py`): a task is not started
  without the original. Up to `NEXUS_SPEC_INLINE_CHARS` it is in the first
  message between `<<<NEXUS-TASK-TEXT <sha12>` and `NEXUS-TASK-TEXT <sha12>>>>`;
  longer, it is a file in the sandbox (`spec-<sha12>.md` + index), read back and
  compared before the conversation is created. A transport that cannot place a
  file is covered in the next section.
- **HTTP**: text routes take up to `NEXUS_SPEC_MAX_CHARS`; a body over the
  route's cap is 413 (answered first, then drained - `Handler._linger`).
  `POST .../goals/<id>/run`: 201 started, 202 planning in the background, 409
  already running (with `run_id`), 422 no plan can be made, 503 no provider
  gave a usable plan. `GET .../specs/<id>` and `/content`, `/sections`,
  `/requirements`, `/stages`. `ask` / `plan` / `code` take 200 000 characters
  per message and say which mode takes more.
- **Settings**: `NEXUS_SPEC_MAX_CHARS` (1 000 000), `NEXUS_SPEC_INLINE_CHARS`
  (20 000), `NEXUS_SPEC_MESSAGE_CHARS` (60 000), `NEXUS_SPEC_STAGE_TOKENS`
  (24 000), `NEXUS_SPEC_SANDBOX_DIR`, `<PROVIDER>_CONTEXT_TOKENS`.
- **Tests**: `tests/test_long_task.py` (eight sizes up to 1 000 000 characters,
  compared byte for byte at the agent), `tests/test_long_task_cases.py` (what can
  go wrong), `tests/long_task_kit.py` (a planning model that answers from the
  prompt it is actually sent).

## Autonomous runs on the deployed service (2026-11-12)

On the live service every run was planned and none ever started a task: eight
runs, 64 tasks, all `pending`, attempt 0. Then the loop was switched on there
(`NEXUS_LAB_INLINE=1`, on the code of PR #27), and its first half hour is the
evidence for several items below: of six runs one completed and one went on
verifying task after task, two stopped on a task that "did not verify" (items 6
and 12), two on the thirty-minute limit (item 11). What was found:

1. **Nobody advanced runs.** `render.yaml` is one web process. The lab loop
   ran only as `python -m orchestration.lab_worker` or with
   `NEXUS_LAB_INLINE=1`, and neither was set. **The server now advances runs
   unless `NEXUS_LAB_INLINE` says no** (`web_server.lab_inline_default`). Tests
   keep it off for the session (`tests/conftest.py`) and turn it on where the
   loop is the subject.
2. **The live service is on the `app` transport** - by the evidence in its
   database (two-phase starts on 2026-11-09, almost no `conversation_routes`
   rows); the dashboard was not seen, and the code default is `direct`. That
   transport cannot place a file, so a long text could not be handed over and
   the task was refused. Now
   (`nexus_spec_delivery.prepare(..., files=False)`): up to
   `NEXUS_SPEC_MESSAGE_CHARS` the text is in the message whole; longer, each
   task's message carries the part it was planned from between
   `NEXUS-TASK-PART <sha12 of the part>` lines, with the outline of the whole,
   and says that the rest is not in the conversation (`delivery.mode = part`).
   Between them the tasks are given every byte; no single agent is. The
   `direct` transport remains the one that gives every agent the whole text.
3. **A run planned before specifications** has tasks with no `spec_ref`. Its
   agent is now given the request its goal holds (`prepare_request`,
   `delivery.source = goal`). This never refuses a start.
4. **The agent's answer was read from messages only.** An agent also ends by
   calling the `finish` tool, and then its report is the tool's `message`. On
   the live service four of thirteen finished attempts were recorded as
   "finished with no final response"; this is the likely cause (those
   conversations were not inspected). `CloudClient.final_response`
   now applies the SDK's own rule (`get_agent_final_response`) and reads the
   events newest first (`recent_events`, `sort_order=TIMESTAMP_DESC`, with a
   fallback for a server that ignores or refuses the parameter), so the end of
   a conversation longer than a thousand events is reached.
5. **A 429 cost a task its attempts.** The account has a daily conversation
   limit (20 when it was last seen). Three refused starts within seconds made
   the task `failed`, the run `blocked`, and "resume" answered "every task has
   used all its attempts". `CloudRateLimited` (a `CloudUnavailable`) is now
   handled by `LabStore.postpone_task`: the attempt and the iteration are given
   back and the task waits (`refused_start_wait`: `Retry-After`, 15 minutes for
   a daily limit, else a minute). The wait is a live lease on a `pending` task;
   `has_claimable` ignores the lease, so a wait is not taken for a deadlock.
6. **The agent was told "if configured to push".** `GitVerifier` accepts a task
   only when the head of its branch on GitHub is a commit the report names. On
   the live service one agent merged and committed and reported "complete
   locally" (`reported commit is not the branch head`), another changed nothing
   and named no commit (`no verifiable commit reported by the agent`); both
   runs stopped. Rule 6 of `OpenHandsExecutor.instructions` now says what
   counts: push to the task's branch, never force-push, unpushed work does not
   count, and if nothing had to change, name the current head.
7. **A start is waited for five minutes**, not eighty seconds
   (`start_poll_attempts`). A start took 33 and 49 seconds when measured;
   giving up early leaves a conversation nobody follows and starts another.
8. **`direct` transport: a recorded sandbox was reused for ever.**
   `cloud_sandboxes` said RUNNING for a sandbox three weeks old.
   `CloudDirectClient._still_running` asks the Cloud before reuse.
9. **Two tasks could work on one branch at once** - two runs, or two
   independent tasks of one run. The verifier compares the branch head with
   the commit the report names, so each moved the head under the other and the
   one read second was "not the branch head". `LabStore.claim_task` now takes a
   task only when nothing else is at work on its repository and branch
   (`_BRANCH_FREE`, with an advisory lock on the branch against two workers
   deciding at the same moment). Two live runs were waiting on one branch.
10. **A conversation with no `execution_status`** (the service cannot ask the
    sandbox at that moment) was settled as "did not finish" and the task
    blocked. It is now asked again on the next pass, as the workflow dispatcher
    always did; the task's time limit bounds the wait.
11. **A task had thirty minutes.** `timeout_seconds` defaulted to 1800 and
    nothing set another value. Two first tasks were still at work after that,
    were recorded as `failed` ("timed out after 1800 s; the conversation was
    left running"), and their runs stopped while the agents went on. The
    default is two hours (`DEFAULT_TASK_TIMEOUT_SECONDS`,
    `NEXUS_TASK_TIMEOUT_SECONDS`); migration 0025 changes the column default
    and gives every task that may still run and carries the old default the
    new one, without touching `updated_at` (the limit is counted from it).
12. **A stopped run said "a dependency is not verified" and nothing else.**
    `LabScheduler._why` now adds the first task that stopped it and the
    verifier's reason (`LabStore.first_unverified`); the studio shows a task's
    reason on its own line.
13. **The account pool and these rules.** `main` gained the account pool
    (previous section) while this was being written; three things did not hold
    together when the two were merged, each shown by a test first
    (`tests/test_openhands_failover.py`, `tests/test_lab_inline.py`):
    - The server's own loop never registered in `nexus_lab_workers`, so the
      diagnostics said "active run(s) exist but no worker heartbeat is fresh"
      on the very deployment where that loop is the only thing advancing runs.
      It registers as kind `inline` and marks itself stopped on the way out.
    - A refused start (429) from a pool account took the failure path: an
      attempt spent, the account marked `unavailable`, and with one account the
      run blocked on "no usable OpenHands account". `DispatchError.refused`
      now carries the 429 and `LabScheduler._start_refused` handles it with or
      without a pool: nothing is spent and the account becomes `degraded`
      (still selectable). If another account can take the task it does, at
      once (`postpone_task(failed_account_id=)`); if none can, the task waits
      and is then offered to the whole pool again (`forget_tried`).
    - The account a task had run on was excluded from its next attempt, so
      with one account in the pool "resume" after a result that did not verify
      blocked the run. `AccountRouter.select_for_task` excludes only
      `tried_accounts`, and the first message is a hand-off only when the
      account really changes (`nexus_lab._last_account`). The hand-off message
      now ends with rule 6 (where the work has to be pushed).

    True of the pool as merged, and not changed here: `build_account_router`
    asks for the accounts of `machine:default`, while an account added in the
    studio belongs to `user:<login>`, so on a deployment with a login the pool
    is never switched on (and the live service has no stored account). Nothing
    in the loop calls `AccountPool.record_success`, so an account marked
    `unavailable` by a failed start stays that way. The router is built once,
    when the loop starts.

    **Status (automatic mode switching).** Two of the above are now closed:
    `build_account_router` gates on `OpenHandsAccountService.has_any_accounts()`
    (any principal), so the pool — and the four selection modes — are switched
    on for a deployment whose accounts belong to `user:<login>`. And the loop
    calls `AccountRouter.note_success` whenever a poll observes a terminal
    conversation, so an account is not written off for ever. The failover
    sequence's step 6 ("проверить его авторизацию") is now enforced:
    `OpenHandsExecutor.dispatch` calls `AccountRouter.ensure_authorized` on a
    *switch* (a task with `tried_accounts`, or an operator's
    `preferred_account_id`), so a revoked key fails over again instead of
    starting a conversation that would only fail inside the sandbox. Tests:
    `tests/test_openhands_pool.py::test_the_pool_activates_when_any_principal_has_an_account`,
    `tests/test_openhands_failover.py::test_a_switch_to_an_account_with_a_rejected_key_fails_over_again`
    and `::test_a_first_start_is_not_probed_for_authorization`.

Still true, and worth knowing before changing this:

- **Render's free plan stops the process after about 15 minutes without a
  request**, and the loop with it. Conversations already started go on in the
  Cloud and are collected when the service wakes; nothing new starts until
  then. The studio keeps the service awake only while it is open in a visible
  tab on a working run (`scheduleWorkPoll`). A run that has to work unattended
  needs a paid instance or an outside request to `/healthz` every few minutes.
- Cancelling a run, its deadline, or a task's time limit does not stop a
  conversation that is already running. "Resume" after a time-out starts a
  second conversation; the first may still push.
- A run on the branch a service deploys from redeploys that service on every
  push. A run was started on `main` of this repository.
- Nothing limits how many runs work at once on different branches. On one
  branch tasks take turns, but the order between runs is whoever asks first,
  and anything else that pushes to the branch (a person, a bot) still moves
  the head under a task.
- Not established on the live service: what `execution_status` a conversation
  reports after its sandbox has been paused. If it is empty, a task whose
  result was not read in time waits out its time limit and is started again.
- On the `direct` transport a conversation's id is derived from its title and
  first message; a second attempt of the same task has the same id.
- `delivery.mode = part` is weaker than a file: an agent cannot read what a
  cross-reference points at outside its part.
## Diagnostics and account-pool defects fixed (this session)

Three real defects were found by running the diagnostics against the live
service and reading the pool code. Each has a regression test that fails if it
is undone. Keep these invariants:

- **The live probe runs on the account it selected, not on the process env.**
  `OpenHandsDiagnostics._live_client` used `build_cloud_client(store)`, so the
  probe exercised whatever `OPENHANDS_*` named. With no `OPENHANDS_DEFAULT_MODEL`
  in the environment the conversation ended `error` (LLM auth) even though the
  stored account had a working `default_model`. It now calls
  `OpenHandsAccountService.execution_client_for_account`, which is the **single**
  place an execution client is built (base URL + decrypted key + per-account
  transport + `default_model`). `lab_worker._client_for_account` and
  `AccountPool.client_for` delegate to it too. Do not add a second builder.
- **Load is derived, not counted.** `begin_task`/`end_task` had no production
  caller, so `active_tasks` stayed 0 and `load_balancing` could not balance.
  `AccountPool._live_load` reads in-flight tasks from `nexus_run_tasks`
  (`dispatched`/`running`/`verifying`, account-bound) and `select` ranks on that
  plus the manual counter. A counter nobody decrements would drift after a crash.
- **The pool must learn from success.** Nothing called `record_success`, so an
  account marked `unavailable` by one transient failure stayed excluded forever.
  `LabScheduler.tick` now calls `AccountRouter.note_success` when a poll observes
  a terminal conversation (`finished`/`error`/`stuck`).
- **Manual executor switch.** `POST /api/v1/projects/{p}/runs/{r}/tasks/{t}/switch`
  (`{"account_id": ...}`), CLI `openhands switch`, backed by
  `LabStore.switch_task_executor`. The task must be in flight (409 otherwise);
  the current account is recorded tried and the target stored as
  `preferred_account_id` (migration `0025`). `AccountRouter.select_for_task`
  honours the preference when the target is usable; `mark_running` clears it once
  the task is dispatched, so a switch applies to exactly one hand-off. The old
  conversation is not stopped (its transcript is the record); the workspace lock
  keeps two executors off one checkout.
- **`REQUIRED_COLUMNS` names `nexus_run_tasks`** with the columns the store
  depends on, including `preferred_account_id`. A new column the store reads must
  be added there.
- Verified live 2026-11-12: diagnostics `cloud_api`/`accounts`/`queues_leases`
  and the full live probe (`conversation_create`, `message_and_events`,
  `run_command`, `workspace_rw`) VERIFIED on the direct transport; a real
  autonomous run through the worker + pool created a Python module, ran pytest,
  broke and fixed it, and committed — recorded `completed_unverified` because no
  repository was bound. `test_backup_restore.py` and `test_browser_e2e.py` were
  run with the PostgreSQL client tools and Chromium installed.

## Durable autonomy, reports, cancel/hide, multi-executor and multi-repo (branch `nexus/durable-autonomy-multiconn`)

The run loop no longer gives up after a few failures, a finished run reports
back into the chat that asked for it, a cancelled run leaves the interface, a
project may use several OpenHands and several GitHub repositories. Migration
`0026_durable_autonomy.sql` is additive. Invariants to keep:

- **A run retries; it does not fail for good.** When a cycle ends with work left
  (`_decide` returns `retrying`), `LabStore.schedule_retry` puts the run in
  `retrying` with `retry_at = now() + retry_backoff_seconds(n)` and
  `start_retry_cycle` (on the next tick after the wait) restarts every task that
  did not verify with a fresh per-cycle `attempt` budget and bumps `cycle`. A
  run's total work is bounded by `max_retry_cycles` and a lifetime
  `total_iterations`; when the cycles are spent the run is **blocked**, never
  failed silently. `NEXUS_MAX_RETRY_CYCLES` / `NEXUS_RETRY_BASE_SECONDS` /
  `NEXUS_RETRY_MAX_SECONDS` tune it.
- **A permanent failure blocks at once.** `release_task(..., permanent=True)`
  records `{"permanent_failure": true}` in the task's evidence;
  `has_retryable_failure` is what tells a run that a cycle cannot change the
  outcome (a `DeliveryError` the transport cannot fix) from one that might. A
  429 is not permanent: `postpone_task` gives the attempt and the iteration back.
- **The state machine has no exit from a terminal state.** `_RUN_ENTERED_FROM`
  never lists `completed`/`failed`/`cancelled` as a source. `retrying` is entered
  from `running`/`blocked` and leaves for `running` (cycle restart) or `blocked`
  (cycles spent). A person's "resume" of a `blocked` run requeues the retryable
  tasks and resets `retry_attempt`/`iteration`; of a `retrying` run it restarts
  the cycle at once.
- **One run, one report** (`orchestration/nexus_reports.py`). A completed run's
  report is built from its own rows and written by the pass that settled it
  (`nexus_run_reports.run_id` is UNIQUE, so a second write is a no-op). Delivery
  to the source chat is durable: a `pending` report is picked up by any later
  pass, a failed delivery is retried with a backoff, and `status` only moves
  forward from `pending`, so a report is never posted twice. The report is only
  written for a **verified** finish; a blocked/failed run writes none.
- **Cancel is terminal, idempotent and hides the run.** `LabStore.cancel_run`
  settles the run and sets `hidden_at`, so `list_runs` (and the UI) drop it while
  every row is kept; `cancel_requested_at` drives the loop's executor signal
  (`LabWorker._signal_cancelled` → `pause`, or `delete_conversation` where there
  is no pause), recorded once per conversation. Cancelling twice keeps the first
  timestamps.
- **`nexus_project_executors.py` is the project's executor allowlist.**
  `ProjectExecutorService` holds one primary (a partial unique index) and ordered
  fallbacks; `AccountPool` restricts a project to its assignments when it has
  any, and keeps the old behaviour otherwise. An account of another principal is
  refused.
- **A project uses several repositories, each with its own account.**
  `ProjectRepositoryService.link` takes `account_id`, `label` and `make_primary`
  (one primary per project); `project_github` resolves the reading identity per
  repository from the link, falling back to the project's single bound account,
  then the server token.
- **The mobile scroll fix is `flex: none` on `.work`.** A card is a flex item of
  the column `.work-list` and carries `overflow: hidden`; without `flex: none` its
  automatic minimum size collapses and it shrinks to clip its content instead of
  making the list scroll. `tests/test_browser_e2e.py` asserts the list really
  overflows and the last task is reachable.
- **Copy reuses one mechanism.** `copyText`/`copied` serve a user message, an
  assistant reply (`copyButton`) and the run report (`copy-report`); the full
  stored text is copied, never the rendered DOM.
- **HTTP/CLI/MCP.** `POST .../runs/{id}/{cancel|hide}`, `GET .../executors`,
  `POST .../executors` and `.../executors/{id}/{enable|disable|remove}`,
  `POST .../repositories/{owner}/{name}/{primary|account}`; the run detail now
  carries `report`. CLI: `run show|cancel|hide`, `project executors show|set`,
  `project repo primary`. MCP adds `list_runs`/`get_run`/`cancel_run`/
  `list_project_executors`/`assign_project_executor`.
- Tests: `tests/test_durable_autonomy.py` (retry cycles, reports, cancel/hide,
  executor assignment, multi-repo, idempotent recovery), the new cases in
  `tests/test_lab_http.py` (routes) and `tests/test_browser_e2e.py` (copy, cancel
  disappearance, report in chat, phone scroll). `tests/test_route_auth.py`
  probes every new endpoint. Full suite: 993 passed / 8 skipped.

## Independent audit of PR #29 (durable autonomy, multi-connection)

This records what an independent audit found and fixed. Read it before changing
the chat turn, the run report delivery, or the planner's empty-plan path.

- **Chat-first intent routing was missing entirely.** With a conversation's mode
  set to `autonomous`, *any* message started a run - a question ("почему прогон
  ломается?") launched agents. `orchestration/nexus_intent.py` is now the one
  router: it classifies a message as `chat` / `analysis` / `autonomous` from its
  *shape* (question vs order), not from a keyword, and `effective_mode` turns
  that into the mode the turn runs in. Migration `0027` adds the `analysis` and
  `auto` modes and makes `auto` the default; the studio has an "Авто" (default)
  and a "Разбор" mode. Invariants: a question never starts a run; an ambiguous
  message is answered with one clarifying question (and does not even call the
  model); `analysis` mode never starts a run; "продолжи" continues the run the
  conversation already started (resume, never a second run); a question asked
  during a run does not change the run's state. `tests/test_intent_router.py`.
- **A statement is not ambiguous.** Only a message that carries a work signal
  but whose shape is unclear is flagged ambiguous. A plain "Вопрос без
  провайдера" or a hostile HTML blob is a normal chat turn (answered, never
  intercepted) - the browser tests depend on that.
- **The planner recovered nothing from an empty plan.** "the planner found
  nothing to do in the specification: the plan has no tasks" was terminal.
  `SpecPlanner._fallback_plan` now decomposes by the specification's own index -
  one task per section that holds at least one *normative* piece, chained in
  document order - each task carrying the `spec_ref` the delivery path needs.
  It never fabricates work: a text with no obligation still fails honestly, with
  the reason. `coverage["fallback"]` marks it. `tests/test_long_task_cases.py`.
- **Report delivery was not idempotent under two workers.** `deliver_pending`
  read the pending rows and marked each `sent` only *after* posting, so the
  inline lab loop and a standalone `lab_worker` (which the docs say may run side
  by side) both posted the same report. Delivery now *claims* each report
  atomically (`claim_pending_reports`, `FOR UPDATE SKIP LOCKED`, status
  `sending`; migration `0028` widens the CHECK) and `reclaim_stale_report_sends`
  returns a dead worker's claim. `tests/test_durable_autonomy.py`.
- **`is_primary` was a label with no effect.** The docstring promised a
  repository-less task runs against the project's primary repository, but
  nothing read the column. `ProjectRepositoryService.primary_repository` now
  resolves it (and treats a project's *single* linked repository as primary in
  effect), and both run-start paths use it.
- **Test-suite flakiness (not a product defect).** Running the whole suite can
  fail with `psycopg.errors.UndefinedTable` when one module's server (with
  `lab_inline=True`) still touches the database while the next module resets the
  schema. Each module passes alone; a clean serial run passes. Do not "fix" this
  by weakening a test.

## Tasks across repositories and the run card (this increment)

- **A plan task may name its own repository and branch.** `validate_tasks` keeps
  them (`keep=("requirements", "earlier", "repository", "branch")`), `merge`
  carries them onto the stored task, and `start_run` uses the task's when it has
  one and the run's otherwise. So one run can span several repositories of a
  project; a task with no repository uses the run's, the common case.
- **The run card names the executor and the retry cycle.** `list_tasks` joins
  `nexus_openhands_accounts.label` as `account_label` (the key is never
  selected), and the studio shows `исполнитель: <label>` and `цикл N` per task.
## Safe restore points (the first safety rule)

Before touching anything, a run takes a restore point. `orchestration/restore_point.py`
writes a **git bundle** (`git bundle create --all`, every ref) plus a JSON record
under `.restore-points/` (gitignored). The record names the exact `head`, `branch`,
whether the tree was `dirty`, the `untracked` paths, the bundle's SHA-256 and size.
Invariants to keep:

- **Non-destructive.** Creating one only reads the repository and writes new files
  into the output directory. It never resets, checks out, stashes or deletes. A test
  asserts HEAD, branches and tracked status are unchanged.
- **Self-contained and offline.** `--all` packs every ref, so the whole history
  restores from the one file with no network. `verify` re-checks the SHA-256, runs
  `git bundle verify`, and confirms the recorded HEAD is among the bundle's refs;
  `restore` clones it into a scratch directory to prove it comes back.
- **Honest about the working tree.** A bundle cannot carry uncommitted changes, so a
  dirty tree also records the patch of tracked changes (`git diff --binary HEAD`) and
  the *names* of untracked paths. Untracked contents are deliberately not copied:
  they may hold secrets and can be large. The `dirty`/`untracked`/`notes` fields say
  what was and was not captured.
- **CLI:** `nexus restore-point create|list|verify` (`orchestration/cli.py`). It is
  local, needs no `NEXUS_URL`/token, and never prints a secret.
- Tests: `tests/test_restore_point.py` (13 cases; a real git repository, a real
  bundle, a real restore, tamper detection, and the CLI).

## OpenHands inventory and current versions (2026-11-12)

- **`docs/OPENHANDS_INVENTORY.md` is the dated inventory** of the OpenHands
  surface NEXUS depends on (Cloud V1 + the sandbox agent-server): API paths,
  authorization, conversation lifecycle, events, status, runtime, workspace,
  tools, limits, errors, timeouts, availability, and the NEXUS↔OpenHands
  compatibility matrix. Read it before changing an adapter.
- **Live versions drift from the two evidence maps.** On 2026-11-12 the live
  Cloud was **1.87.1** (OpenAPI 3.1.0, sdk 1.60.1) and the sandbox agent-server
  **1.60.1** (`refs/tags/v1.60.1`); `INTEGRATION-MAP.md` still names `v1.50.1`
  and `CLOUD-INTEGRATION-MAP.md` implies older. Both maps now carry a pointer to
  the inventory; do not treat their version lines as current.
- **The live probe is green at those versions.** `OpenHandsDiagnostics.run(live=True)`
  with a working `default_model` gave **10 VERIFIED / 0 FAILED / 0 BLOCKED**
  (`conversation_create`, `message_and_events`, `run_command`, `workspace_rw`).
  Without `OPENHANDS_DEFAULT_MODEL` the same probe ends `error` (LLM auth) —
  the documented trap, unchanged.
- **The app-transport 500 was not reproduced.** A `POST /api/v1/app-conversations`
  start with an initial message reached `READY` (~45 s) and `finished`. This is a
  dated observation, not proof the account-side failure is gone; `direct`
  remains the default and never depended on it.
- **`events/search` still caps `limit` at 100** (`limit=200` → 422), and a
  missing/invalid key is `401` on both Cloud (Bearer) and the agent-server
  (`X-Session-API-Key`). Confirmed live.
- **A two-live-account failover stays BLOCKED** here: only one OpenHands key is
  available, so the switch is proven only against the local fakes +
  PostgreSQL (`tests/test_openhands_failover.py`).
- **Do not read a probe conversation back as a defect.** The live probe creates
  and deletes exactly one conversation; all were deleted (0 `nexus-diagnostics`
  remain). A pool that reports "no usable account" while a live `pytest` shares
  the same database is the test suite truncating tables, not a pool bug —
  reproduce on a private database.

## Account pool: edit, connection, limits and masked values (this session)

The pool already routed across accounts. This adds the four account fields a
complete pool still lacked, each enforced in code, not in the UI:

- **`max_concurrent_tasks`** (migration `0026_openhands_account_limits.sql`) is
  the account's concurrency ceiling; `NULL` means no limit. `AccountPool._at_limit`
  compares it with the *same observed load* `load_balancing` uses (in-flight
  `nexus_run_tasks` bound to the account plus the reserved counter), and the
  automatic modes (`auto`, `failover`, `load_balancing`) skip a full account. A
  pool where every usable account is full raises a reason that names the cause
  ("every usable OpenHands account is at its concurrency limit"), never a generic
  "no account". **`manual` is the exception**: a limit steers automatic
  selection, it does not veto an operator's explicit choice.
- **`daily_conversation_limit`** records the account's daily ceiling for the
  operator and the diagnostics. It is informational; the pool never guesses or
  enforces a number the service did not report.
- **`api_key_hint`** is the masked key fragment (`sk-o…onse`: first and last four
  characters; a key under 12 characters is masked whole). It is what the
  interface, the CLI and the diagnostics show, so they can say *which* key is
  stored without holding it. The full key stays in `api_key_encrypted` and
  `OpenHandsAccountService._view` still never returns it.
- **`connection_ok` / `last_connection_at`** are the last reachability probe.
  `CloudClient.ping()` (and `CloudDirectClient.ping`) answers *reachability*
  separately from `health()`: any HTTP answer — including 401/403/429 — means the
  endpoint is reachable, only a transport failure means it is not. An endpoint
  can be reachable while the key is rejected, and the operator needs to tell
  those apart (`check_connection` vs `verify`).

Routes and CLI: `POST /api/openhands/accounts/{id}/edit` (a new `base_url`
re-verifies the stored key against it before committing) and
`.../connection`; `nexus openhands add|edit|connection|account
verify|default|enable|disable|remove|priority`. `_VIEW_COLUMNS` in
`nexus_openhands_accounts.py` is the single key-free shape every read returns —
add a new column there and to `REQUIRED_COLUMNS` in `cloud_store.py`, or a read
will silently omit it. Tests: `tests/test_openhands_account_admin.py` (16 cases)
and the new cases in `tests/test_openhands_pool_http.py`.

## Account error history: "ошибки" as a list, not a line (this session)

The account view kept only `last_error`, the *latest* failure. The task's
per-account field "ошибки" (the account's errors) needs the history, so
migration `0027_openhands_account_error_history.sql` (additive) adds
`error_count` and `nexus_openhands_account_events`.

- **`error_count`** is the running total of observed failures, so a flapping
  account can be ranked without reading the history.
- **`nexus_openhands_account_events`** is one row per observed failure or
  recovery: `kind` is `auth` (a rejected key), `transport` (unreachable),
  `error`, or `recovered`; `detail` is the message and is never a secret.
  `AccountPool.record_failure`/`record_success` append to it, so
  `OpenHandsAccountService.error_history` returns the account's failures
  newest-first. `_append_event` swallows its own errors on purpose: the history
  is a side record and must never fail the dispatch it observes.
- **HTTP `GET /api/openhands/accounts/{id}/errors`** and CLI
  `openhands errors --account <id>`; `error_count` is on the account view and in
  the pool listing.
- `error_count` is in `_VIEW_COLUMNS` and both it and the new table are in
  `REQUIRED_COLUMNS` in `cloud_store.py`.
- Routes `edit`, `connection` and `errors` are now in `tests/test_route_auth.py`
  (the `edit`/`connection` routes were added without that coverage). Tests:
  the error-history cases in `tests/test_openhands_account_admin.py` and
  `tests/test_openhands_pool_http.py`.

## Race-condition protection: fencing tokens and task ownership (this session)

`claim_task` was already atomic (`FOR UPDATE SKIP LOCKED` plus a lease) and
refused a second claim of one task. What was missing was any way to stop a
worker that had **lost** its task from still writing to it: the claim took a
`worker_id` and never stored it, and no mutation was fenced. Migration
`0028_task_lease_fencing.sql` (additive) adds two columns to `nexus_run_tasks`:

- **`lease_owner`** — the worker that holds the claim, `NULL` when unclaimed.
  Informational (diagnostics, the operator surface); never a secret.
- **`lease_token`** — a per-task counter that only moves forward. `claim_task`
  bumps it and records the owner; **every transition that takes a task away from
  its holder bumps it too and clears the owner**: `reclaim_expired`,
  `switch_task_executor`, `_requeue_retryable` (a resumed run), the cancel/fail
  block in `_apply_run_state`, and the release/postpone/requeue mutators.

The fence is the **token, not the clock**. `mark_running`, `record_verification`,
`release_task`, `postpone_task` and `requeue_for_failover` take an optional
`lease_token`; when given, the `UPDATE` carries `AND lease_token=%s`, so a stale
holder's write matches no row and is a no-op (the row is returned as it stands,
not an error). The compare and the write are the same statement, in the
database. `LabStore.holds_lease(task_id, token)` is checked by the scheduler
immediately before it starts a conversation; it does **not** consult
`lease_expires`, because a lease that expired but that nobody reclaimed still
belongs to its worker — it is the reclaim (or switch, or resume) that takes the
task away, and that is what bumps the token.

Do not "simplify" this back to a time-based check, and do not drop the
`lease_token` argument from a mutation: without it the fence is open. A caller
that read the task carries `task["lease_token"]` back; `_task_row` always
exposes it (default 0 for a row written by an older writer). `lease_owner` and
`lease_token` are in `REQUIRED_COLUMNS` in `cloud_store.py`.

Tests: `tests/test_race_protection.py` (13 cases; stale bind, stale verdict,
stale release/postpone/requeue, switch, resume, the pre-dispatch lease check,
the expired-but-unreclaimed lease, the column default, the schema guard, the
abandoned-conversation record, and the transport stop over real HTTP).
Removing the `mark_running` fence makes two of them fail, so they are
load-bearing.

**Stopping the abandoned executor.** A switch clears the task's binding, which
alone does **not** stop an agent already running in the Cloud. So the switch is
a *hand-off plus a stop*: `switch_task_executor` records the abandoned
conversation in the task's evidence (it is never lost) and
`App.switch_task_executor` stops it before returning
(`App._stop_abandoned_conversation`). `stop_conversation` is best effort and
never deletes the transcript. It is **transport-honest**: the direct and local
transports pause the conversation
(`POST /api/conversations/{id}/pause`, keeps the transcript) and return `True`;
`CloudClient.stop_conversation` (the `app` transport) returns `False`, because
the Cloud V1 surface only pauses at the *sandbox* level. A caller must not treat
a `False` as "stopped" — the branch lock remains the guarantee that two
executors do not push to one branch at once.

## The lab worker is a separate process and must outlive the database (this session)

The autonomous lab is advanced by a loop that runs in two places: the web
server's own thread (unless `NEXUS_LAB_INLINE` says no) and a separate
`python -m orchestration.lab_worker` process. Three launch paths start the
separate process: `docker-compose.yml` (the `worker` service),
`render.yaml` (the `python-dispetcher-lab-worker` worker service), and the
systemd unit now shipped at `packaging/systemd/nexus-lab-worker.service`.
All three run the same module with the same `SUPABASE_DB_URL`, so the API and
the worker agree on configuration and share one PostgreSQL.

- **The worker loop must survive a transient database error.** `PostgresStore`
  raises `StoreUnavailable` while it reconnects, and the driver never
  reconnects by itself. `LabWorker.run_forever` used to let that propagate out
  of the loop, so one blip (a database restart, a pooler recycling its clients,
  a network reset) killed the only thing advancing the runs; the web server's
  inline loop already caught it, so the two paths disagreed. The loop now
  catches the error, logs it, and retries on the next pass; registration is
  retried until it sticks and the final deregistration is best effort. Do not
  "simplify" this back to a bare `tick_once()` in the loop.
- **The worker is judged by what it changes, not by its log.** `tests/test_lab_worker_process.py`
  starts the real child process against real PostgreSQL and the fake OpenHands
  Cloud/GitHub (the web server runs with `NEXUS_LAB_INLINE=0`, so the worker is
  the only actor): it must register as kind `worker` (a fresh heartbeat in
  `nexus_lab_workers`), pass the `lab_worker` diagnostic as `VERIFIED`, claim and
  dispatch, reach `completed`, survive a `SIGKILL` (a second process continues
  the run without starting a second conversation), and keep working with no
  client connected. `tests/test_planner_worker.py::test_the_worker_loop_outlives_a_transient_database_error`
  is the load-bearing resilience case (removing the fix makes it fail).
- **`tests/test_worker_setup.py`** checks the launch artifacts without starting
  anything: the systemd unit parses and runs the worker module, Compose and
  Render define a worker service on the same database, and `docs/WORKER_SETUP.md`
  documents every mechanism. Keep the three launch paths pointing at the same
  module.
- **`--once` registers too (this session).** The cron-style single pass
  (`main()` with `--once`) called `tick_once()` directly and left no worker row,
  so a deployment that advances runs by cron read as "active run(s) exist but no
  worker heartbeat is fresh" to `GET /api/openhands/diagnostics`. It now goes
  through `LabWorker.run_once()`: register (`kind=worker`), one pass (which
  writes the heartbeat), then mark the row `stopped` on the way out - the same
  trace a long-lived worker leaves, minus liveness a single pass cannot have. A
  pass whose registration fails still ticks. `run_forever` marks the row stopped
  through the same `_stop_worker_best_effort`. Tests:
  `tests/test_planner_worker.py::test_run_once_registers_and_heartbeats_like_a_long_lived_worker`
  and `::test_run_once_still_ticks_when_it_cannot_register`.

## The EVIDENCE stage of the autonomous pipeline (this session)

The audit diagram has always ended `task/workflow result ──▶ Evidence Trust
Engine`, but the lab loop only wrote the verdict onto the task row
(`record_verification`); the signed, re-verifiable record the web API and MCP
produce was never written for an autonomous run. The stage is now wired.

- **`LabScheduler(evidence=…)` is the EVIDENCE stage.** When it is given an
  `EvidenceService` (`nexus_evidence.py`), every settled task result — a
  verified/unverified/failed verdict from the poll path, and the `blocked`
  verdicts from the dispatch gate and the "no usable account" path — is also
  recorded through the Evidence Trust Engine via
  `EvidenceService.record_task_result`. `None` (no signer configured) keeps the
  loop exactly as it was: the record is skipped, never faked.
- **Best effort, never fatal.** `LabScheduler._record_evidence` logs and returns
  on any error: a broken key must not stall a run, and the task's own verdict is
  already written before evidence runs. Removing the `try/except` makes a run
  die on a bad signer — the wrong failure.
- **The record binds the observed commit.** `record_task_result` signs
  `remote_sha` (what the Git verifier read on the branch), not a commit the agent
  claimed, so `reverify(expected_commit=…)` is meaningful. It also carries the
  task verdict in `detail.verdict` — a task can be `completed_unverified` in a
  `VERIFIED` (properly signed) record, and that is honest, not a contradiction:
  the signature is about the record's integrity, the verdict is about the work.
- **Wired from the environment in `lab_worker.main`** (`build_evidence_service`,
  a signer only when `NEXUS_EVIDENCE_PRIVATE_KEY` is set) and from `App`'s own
  loop (`App.evidence`). A deployment with no key is unchanged.
- Tests: `tests/test_lab_loop.py` (the EVIDENCE-stage block: signed record +
  re-verify, a broken signer does not stop the loop, no service writes nothing)
  and `tests/test_pipeline_e2e.py` (the whole pipeline over HTTP, with the
  record re-verified through the public route).

## The full autonomous pipeline, verified end to end

`tests/test_pipeline_e2e.py` drives the whole chain over real HTTP and real
PostgreSQL — `USER PROMPT → PLANNER → DAG → SCHEDULER → OPENHANDS → EXECUTION →
QA → EVIDENCE → ACCEPTANCE → NOTIFICATION` — against the same local socket
servers the studio tests use (fake Cloud, fake GitHub, fake LLM). It asserts each
stage's *effect*, not its claim: the dependency gates the DAG, a text answer
naming no commit is `completed_unverified` and blocks the run ("не считай
текстовый ответ доказательством"), a signed evidence record exists and
re-verifies, and the completion notification appears only after the run ends. It
also runs the same loop through the real `LabWorker` class with
`NEXUS_LAB_INLINE=0`, proving the server accepts/plans while the separate worker
advances. `OpenHandsExecutor.instructions` is asserted to ask the agent for every
capability the request lists (inspect, read/change files, Git, install allowed
deps, run tests, analyze and fix errors, push, report).

## The pipeline's ACCEPTANCE stage on the autonomous loop (this session)

The EVIDENCE stage above signs every settled *task* result. What was still missing
is the stage the pipeline names after it: **ACCEPTANCE**. A run that reached
`completed` left no signed record of *why* it was accepted - only the scheduler's
word on the run row - so the two stages `... → EVIDENCE → ACCEPTANCE →
NOTIFICATION` ended at EVIDENCE.

- **`nexus_evidence.LabEvidence`** records the run's terminal decision over the
  tasks' own verdicts. `LabScheduler(acceptance=...)` calls it where it decides
  the run's fate, and again for a terminal run it finds (an operator-cancelled run
  never went through the decide step, so this is the pass that sees it).
- **The decision is earned, not asserted.** `ACCEPTED` only when every task is
  `completed_verified`; `NOT_READY` for a cancelled run; `REJECTED` otherwise.
  `READY_WITH_CONDITIONS` is not used - a run has no partial-acceptance contract.
  A blocked run is not terminal, so it is not decided yet; the task that stopped
  it already has its own EVIDENCE record.
- **`EvidenceService.record_once` is the exactly-once gate.** The lab names each
  record with a deterministic uuid (`lab_evidence_id`), so the `nexus_evidence`
  primary key absorbs a re-run of the step: a worker that crashes between the
  decision and the record collides with its own row instead of appending a
  second. `record()` gained `record_id` for this; the id is the row key, not a
  signed field, so it cannot replace a signed record with a different one.
- **No signer, no record - never a fake.** Without `NEXUS_EVIDENCE_PRIVATE_KEY`
  `LabEvidence` records nothing and warns once (per process); the run is
  unaffected. `/api/system` already reports signer presence.
- `LabStore` gained `get_task_internal`/`list_tasks_internal` for the loop (it is
  not a project member), mirroring the `*_internal` methods that already existed.
- Tests: `tests/test_lab_evidence.py` (the ACCEPTED decision, a cancelled run as
  NOT_READY, a blocked run leaving no decision, the id derivation, and a
  whole-pipeline walk asserting a durable artifact per stage). Removing the
  acceptance recording makes them fail.
