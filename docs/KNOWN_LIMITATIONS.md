# Known limitations

Everything here is a real, current limitation. Nothing is described as working
that has not been exercised; where a claim is only proven against the local
stand-in servers (not the live service), it says so. `docs/PRODUCTION_READINESS.md`
is the per-subsystem matrix with the evidence.

## Not implemented (design or route only)

- **The MCP *client*** (NEXUS connecting *to* external MCP servers) is a design
  only (`docs/MCP_ARCHITECTURE.md`). The MCP *server* (stdio + Streamable HTTP)
  is implemented. **No current NEXUS user flow needs an MCP client**: NEXUS is
  the MCP server the operator/agent connects *to*; a conversation's own MCP
  servers are configured on the OpenHands account and the agent-server connects
  to them, not NEXUS. A client would be a new capability, not a completion of an
  existing one, so it is left as design until a scenario needs it.
- **Self-QA** is implemented as a **deterministic** engine
  (`orchestration/nexus_selfqa.py`, `nexus qa run`): it runs a project's own
  checks (`.nexus-qa.json`, or lint + tests) in an isolated checkout and returns
  passed/failed/skipped/blocked. What it does **not** do: judge whether the
  result matches the task's *intent* (that needs a model and is a separate,
  optional layer, deliberately not part of the verdict), and it is not yet
  wired into the run loop as an automatic gate - it is an operator/CLI check.
- **Preview of PDF / DOCX / XLSX / PPTX**: the File Lab renders text and images
  inline; any other type is offered as a download. The UI does not claim to
  render a format the code cannot (`orchestration/file_lab.py`, `TEXT_EXTS` /
  `IMAGE_EXTS`). An office-document text extractor is P2 and not in the MVP.
- **Features that have routes but no screen** in the studio: lab file uploads,
  workspaces and voice notes; evidence records; GitHub create-repository and
  visibility confirmations; the legacy task form and queue; the system
  diagnostics page; the allowlist editor. They are reachable through the API,
  the CLI and MCP. Plan approval and OpenHands account management **are** on
  screen (the chat-first «Вручную» mode and Settings → Подключения).

## Live verification achieved (2026-11-16)

Contrary to the earlier note that live execution was unproven, a **full
autonomous run was driven through NEXUS against the live OpenHands Cloud** in
this environment:

- project bound to a real GitHub branch, a goal submitted, the run **planned**
  into a two-task DAG, dispatched to live OpenHands conversations, the agent
  **cloned, committed and pushed** to the bound branch, and NEXUS **verified**
  both tasks (`completed_verified`); the run reached `completed` with a report.
- The account still has **no GitHub provider token**, but NEXUS now hands the
  sandbox a Git credential itself (from `GITHUB_TOKEN` or the bound account), so
  a push succeeds. The `secrets` channel is verified: the agent-server injects
  the value and masks it in the agent's output.


## Testing caveats

- **One load-sensitive test can flake on a saturated 2-CPU box.**
  `tests/test_long_task_cases.py::test_a_restart_while_the_plan_is_being_built_goes_on_from_the_part_it_was_on`
  kills the simulated crashing process at a point that races the spec
  planner's attempt bookkeeping: under heavy load the third stage can read
  `attempts=1` instead of `2` (the rest of the assertions still hold; the
  suite is green on CI and in isolated runs). Not a correctness defect — the
  attempt counter is a diagnostic, the plan and the run complete correctly
  either way. Documented so nobody "fixes" it by loosening the assertion;
  the real fix is a deterministic seam in the test's crash timing.

## Verified only against the local stand-in servers

Every test uses `tests/fake_cloud.py`, `tests/fake_runtime.py`,
`tests/fake_github.py` and a local OpenAI-compatible LLM endpoint. What that
does **not** prove:

- that a live OpenHands account accepts a task and completes it (needs a live
  key and a live model);
- that a live GitHub write lands (needs a live token with write scope);
- that a real model answers correctly (needs a live provider key).

`docs/INTEGRATION_TESTING.md` is how to run the live checks.

## OpenHands / runtime

- A conversation that is already running in the Cloud is **not stopped** by
  cancelling a run, a deadline, or a task's time limit. "Resume" after a
  time-out starts a second conversation; the first may still push.
- On the `direct` transport a conversation's id is derived from its title and
  first message, so a second attempt of the same task has the same id.
- An account with **no GitHub provider token** cannot clone a repository on its
  own; a task that must push ends `completed_unverified`.
- A bare sandbox defaults to a model whose key may be absent; pass
  `OPENHANDS_DEFAULT_MODEL` or the conversation fails with an LLM auth error.
- `delivery.mode = part` (a long text on a transport that cannot place a file)
  is weaker than a file: an agent cannot read what a cross-reference points at
  outside its part.

## Chat / repository reading

- The chat does not search file **contents**; it reads what it is shown and
  what it asks for by path. A "where is X used" question over a large repository
  is answered from the file list and a few files, and says what it read.
- A file named like a credential is never read (`.env`, keys, `.npmrc`,
  `credentials.json`…); a file over 1 MB is never fetched.
- What a model reads is sent to the model provider the cascade selects; a
  private repository's files are therefore sent to that provider.

## Data and process

- **A generation in flight is lost on a restart**: the answer was not stored.
  The conversation is left `thinking` and can be asked again.
- **Render's free plan** stops the process after ~15 minutes without a request,
  and the loop with it; conversations already started continue in the Cloud.
- A run on the branch a service deploys from redeploys that service on every
  push.
- Nothing limits how many runs work at once on **different** branches; on one
  branch tasks take turns, and anything else that pushes to the branch (a
  person, a bot) still moves the head under a task.
- **Multiple concurrent runs in one conversation are intentional** (a distinct
  message creates its own run); idempotency is per text/goal
  (`tests/test_concurrent_runs_policy.py`).

## Security posture

- The auth model is single-owner (`AUTH_USERNAME` / `AUTH_PASSWORD_HASH`); there
  is no per-user account system. Project isolation is enforced by membership
  rows, but there is one operator principal by default.
- The direct transport selects a repository by *instruction* to the agent, not
  by enforcement at the sandbox (the `app` transport supports true repository
  selection). `OPENHANDS_DIRECT_REPO_MODE` governs this and refuses to ignore
  the field silently.

## Migration framework

- The repository's test suite runs migrations by executing each `*.sql`
  directly. `PostgresStore.apply_migrations` additionally refuses to re-run an
  edited, already-applied migration, but a full manifest with per-file effect
  verification (the `arena/d7bb7e3c` design) was **not** integrated: it is a
  larger framework that the suite would not exercise. See `docs/BRANCHES.md`.

## Full CRUD (this increment)

The entity-by-entity audit is `docs/CRUD-MATRIX.md`. What is implemented and
tested this increment: permanent (hard) delete for conversations and projects
(two-step: trash first), a dependents census before a project delete, the full
notification lifecycle (read/unread, bulk, delete) and session list/revoke.

Still open, with the reason (not defects, decisions):

- **Pagination and bulk actions** on the large lists. Every list is already
  bounded (200 rows), so this is a scale gap, not a correctness gap.
- **File trash/restore.** A file delete is final; a retention policy is needed
  before a trash state means anything.
- **Message edit/delete and memory delete.** Deliberately not offered: they
  would falsify a context an agent already acted on. The safe path is a new
  message; permanent delete removes the whole conversation.
- **Multi-user project membership.** The auth model is single-owner; adding a
  member is a larger change than a route.
- **A central audit log.** Each entity has its own trail
  (`nexus_project_events`, evidence, run executions); there is no single "who
  changed what" endpoint.

## Production smoke tests

The authenticated smoke tests (sign in, create a project/conversation, send a
message, start a run) could not be run against production: this environment
holds no production credentials (`WEB_AUTH_TOKEN`/`AUTH_*` are not available),
and creating an account is out of scope. The unauthenticated checks
(`/healthz`, `/readyz`, the served assets) were run and pass; the authenticated
flows are covered against the real server locally and by the browser E2E.
