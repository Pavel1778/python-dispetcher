# OpenHands setup

How NEXUS talks to OpenHands, and how to configure it so a conversation really
runs. This is the operator-facing companion to `INTEGRATION-MAP.md` (the API
facts) and `AGENTS.md` (the runtime facts that cost debugging to learn).

## What NEXUS uses OpenHands for

NEXUS is the **control plane**: it owns tasks, state, leases and verification.
OpenHands is the **executor**: it runs a conversation that edits a repository,
runs commands and returns a report. NEXUS never treats an agent's text as proof
of work — a result is `VERIFIED` only when the Evidence Trust Engine (or the Git
verifier) confirms it independently.

## The two transports

`OPENHANDS_TRANSPORT` picks how a conversation is created:

| value | how | repository selection | file delivery |
| --- | --- | --- | --- |
| `direct` (default) | NEXUS starts a sandbox itself and drives that sandbox's agent-server | by instruction (`OPENHANDS_DIRECT_REPO_MODE`) | yes (upload + read back) |
| `app` | the Cloud `/api/v1/app-conversations` endpoint | native field | no |

`direct` is the default because it is the path verified to create a *running*
conversation. `app` is kept for environments where the app-conversation
endpoint's sandbox provisioning works.

## Required environment

| variable | required | meaning |
| --- | --- | --- |
| `OPENHANDS_API_KEY` | yes (Cloud) | Bearer key. Create it in the Cloud UI under **Settings → API Keys**. Never logged, never returned. |
| `OPENHANDS_CLOUD_URL` | no | Override for a self-hosted instance. Must be `http(s)`. Default `https://app.all-hands.dev`. |
| `OPENHANDS_TRANSPORT` | no | `direct` (default) or `app`. |
| `OPENHANDS_DEFAULT_MODEL` | **recommended** | A model the sandbox can actually reach. See below. |
| `OPENHANDS_DIRECT_REPO_MODE` | no | `prompt` (default), `refuse` or `allow`. See below. |
| `OPENHANDS_AGENT_PROFILE_ID` | no | Select an account agent profile by id. Unset = inherit the account's active profile and its MCP servers/skills. |
| `NEXUS_SECRET_KEY` | to store accounts | Fernet key that encrypts a stored API key. Without it, adding an account is refused rather than writing plaintext. |
| `NEXUS_OPENHANDS_ALLOWED_HOSTS` | no | Comma-separated hosts permitted as a private self-hosted endpoint (narrower than `NEXUS_ALLOW_PRIVATE_TARGETS`). |

Every operator-supplied base URL is checked by the SSRF guard (`net_guard`): a
non-`http(s)` scheme, embedded credentials, a loopback/private/link-local or
cloud-metadata target is refused before any socket is opened. See
`docs/ENVIRONMENT.md` §3.3.

### `OPENHANDS_DEFAULT_MODEL` is not optional in practice

A freshly provisioned sandbox starts a conversation on a **default model whose
key the sandbox may not have**. The conversation then fails almost immediately
with `LLMAuthenticationError` (`classification.kind = "auth"`, `retryable =
false`). Set a model the sandbox can reach:

```bash
export OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash
```

Verified live (2026-11-11): without it, `error` with an LLM auth failure; with
it, the same task reaches `finished`.

## Repository selection on the direct transport

The sandbox agent-server create call has **no** `selected_repository` field; a
conversation starts in an empty workspace. `OPENHANDS_DIRECT_REPO_MODE` decides
what happens to a repository-scoped task:

- `prompt` (default): a deterministic "clone `owner/name` at `branch`" step is
  prepended to the agent's instructions. Correct, but by instruction, not a
  native field.
- `refuse`: raise, so the task dead-letters with a clear reason. Use when a
  repository task must never run without native selection.
- `allow`: ignore the field (repository-less work only).

`CloudClient.supports_repository_selection` is `True`; the direct transport's is
`False`. `/api/config` and the System screen report which applies.

## Verify the setup

```bash
# The whole diagnostic report (structural checks; the live probe is opt-in):
NEXUS_URL=https://nexus.example NEXUS_TOKEN="$WEB_AUTH_TOKEN" \
  python -m orchestration.cli openhands diagnostics --json

# The live end-to-end probe (creates and deletes one conversation):
NEXUS_URL=... NEXUS_TOKEN=... \
  python -m orchestration.cli openhands diagnostics --live
```

Each check reports `VERIFIED`, `FAILED`, `BLOCKED` or `UNVERIFIED` (see
`FAILOVER.md`). The live probe is the only check that proves a conversation
really runs; without it, conversation checks are `UNVERIFIED`, never assumed.

**The live probe runs on the account it selected.** It builds the client from
that account's stored `base_url`, key, `transport` and `default_model`, not from
the process-wide `OPENHANDS_*` variables. So a probe that says `VERIFIED` is
evidence about *that account*; a probe that could pass on a working process
default while the named account is misconfigured would be worse than no probe.
When no account is configured the check is `BLOCKED`, not `FAILED`.

### The checks the report runs

| check | proves | needs `live` |
| --- | --- | --- |
| `database`, `schema` | a PostgreSQL round-trip; every required table/column present | no |
| `accounts` | each configured account's key is accepted | no |
| `account_pool` | the selection mode picks a usable account | no |
| `cloud_api` | the OpenHands endpoint answers | no |
| `queues_leases` | claim → release → re-claim on a throwaway run | no |
| `recovery` | an expired lease is reclaimed and re-claimed with a higher fencing token; a bound conversation is not re-claimed | no |
| `lab_worker` | a fresh worker heartbeat (FAILED when work is waiting and none is fresh) | no |
| `providers`, `provider_probe` | the LLM orchestrator cascade (configuration; then a real read-only probe) | probe only |
| `openhands_tools`, `mcp_configuration`, `nexus_tools` | the agent's tool catalog, the account's MCP names, the NEXUS surfaces | catalog only |
| `conversation_create`, `message_and_events`, `run_command`, `workspace_rw`, `task_completion` | create a conversation, deliver a message, read events, run a shell command, read/write the sandbox, reach the terminal state `finished` | yes |

`task_completion` follows the conversation's real terminal state: `finished` is
`VERIFIED`; any other terminal state (`error`, `stuck`) is `FAILED`; without
`live` it is `UNVERIFIED`.

To see the tool surface — which tools NEXUS exposes and which the OpenHands
agent has:

```bash
NEXUS_URL=... NEXUS_TOKEN=... \
  python -m orchestration.cli openhands tools --live
```

`nexus_tools` (the MCP and `sr_` client tools) and `mcp_configuration` (the
account's configured MCP server names) run without `--live`; the agent catalog
is `UNVERIFIED` without it, because reading it on the direct transport may
provision a sandbox. A listed tool is not proof it ran — only an observed
`ActionEvent` is.

## Verified live (2026-11-12)

Against the real Cloud service with a valid `OPENHANDS_API_KEY`:

- `cloud_api`: `VERIFIED` (`GET /api/v1/app-conversations/count` -> 200).
- `accounts`: `VERIFIED` (key accepted; `llm_api_key_set` true).
- `queues_leases`: `VERIFIED` (claim → release → re-claim round trip).
- `recovery`: `VERIFIED` (an expired lease reclaimed and re-claimed with a
  higher fencing token; a bound conversation is not re-claimed). A real database
  round trip on a throwaway project, so it passes without a Cloud key too.
- `live_conversation` / `message_and_events` / `run_command` / `workspace_rw` /
  `task_completion`:
  `VERIFIED` with `--live` on the `direct` transport and a stored
  `default_model` of `openhands/deepseek-v4.1-flash`. Without a usable model the
  conversation ends `error` (LLM auth) — which is exactly why the account's own
  `default_model` must be the one the probe uses.
- A real autonomous task driven through NEXUS (worker + account pool) created a
  Python module, ran pytest, introduced a controlled bug, saw the failure, fixed
  it, re-ran the tests and committed (`3514cec`). NEXUS recorded it
  `completed_unverified` because the task had **no repository bound**, so the
  Git verifier could not confirm a remote commit — an honest verdict, not a
  failure.

## Common failures

| symptom | cause | fix |
| --- | --- | --- |
| conversation `error`, `classification.kind=auth` | sandbox default model has no key | set `OPENHANDS_DEFAULT_MODEL` |
| `POST /api/v1/sandboxes -> 429 DAILY_SANDBOX_LIMIT_REACHED` | the account's daily sandbox quota is spent | wait, raise the quota, or reuse a running sandbox |
| `422 ... One of agent, agent_settings, or agent_profile_id must be provided` | the create call omitted `agent_settings` | the dispatcher fetches it from `GET /api/settings`; do not strip it |
| task dead-letters on a repository task | `OPENHANDS_DIRECT_REPO_MODE=refuse` | switch to `prompt`, or use a transport that selects natively |
