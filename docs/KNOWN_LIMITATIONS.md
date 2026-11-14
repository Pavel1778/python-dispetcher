# Known limitations

Everything here is a real, current limitation. Nothing is described as working
that has not been exercised; where a claim is only proven against the local
stand-in servers (not the live service), it says so. `docs/PRODUCTION_READINESS.md`
is the per-subsystem matrix with the evidence.

## Not implemented (design or route only)

- **The MCP *client*** (NEXUS connecting *to* external MCP servers) is a design
  only (`docs/MCP_ARCHITECTURE.md`). The MCP *server* (stdio + Streamable HTTP)
  is implemented.
- **The Self-QA engine** (`docs/SELF_QA_ARCHITECTURE.md`) is design only.
- **Preview of PDF / DOCX / XLSX / PPTX**: the File Lab renders text and images
  inline; any other type is offered as a download. The UI does not claim to
  render a format the code cannot (`orchestration/file_lab.py`, `TEXT_EXTS` /
  `IMAGE_EXTS`).
- **Features that have routes but no screen** in the studio: lab file uploads,
  workspaces and voice notes; evidence; plan approval; GitHub create-repository
  and visibility confirmations; the legacy task form and queue; the system
  diagnostics page; the allowlist editor; OpenHands account management. They are
  reachable through the API, the CLI and MCP.

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
