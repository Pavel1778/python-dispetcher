# OpenHands accounts and the account pool

NEXUS can hold any number of OpenHands accounts and route work across them. An
account is one row: a label, a base URL, a transport, an optional default model
and an **encrypted** API key. The pool decides which account runs each task.

## Security rules (enforced in code)

- The API key is Fernet-encrypted with `NEXUS_SECRET_KEY` before it is written.
  Without that key, adding an account is **refused** (`SecretsError`); the server
  never writes a plaintext credential.
- The key is never returned by any method, never logged, never in an exception
  message. `account_view` is the only shape that leaves the service, and it has
  no key field. It carries a **masked hint** (`api_key_hint`) instead — the first
  and last four characters, e.g. `sk-o…onse` — so the interface and the logs can
  show *which* key is stored without holding it. A key shorter than 12
  characters is masked whole.
- An account is verified against Cloud (`GET /api/settings`) before it is
  trusted. A rejected key marks the account `invalid`, not silently kept.
- Accounts are scoped to a principal. A project may bind exactly one; the
  worker resolves the project's owner as the principal whose pool applies.

## The account fields

| field | meaning |
| --- | --- |
| `id` | unique id (uuid) |
| `label` | a human name, unique per principal |
| `base_url` | the Cloud (or self-hosted) API endpoint |
| `transport` | `direct` (sandbox agent-server) or `app` (app-conversation endpoint) |
| `default_model` | the model a bare sandbox is told to use; optional |
| `state` | `active` / `invalid` / `revoked` — the **authorization** status |
| `identity` | the verified agent-profile id, when known |
| `is_default` | the principal's primary account (at most one) |
| `enabled` | the **on/off** switch; disabling is reversible and keeps the key |
| `priority` | the failover order (lower is tried first) |
| `health` | the last observed health: `unknown` / `healthy` / `degraded` / `unavailable` |
| `active_tasks` | the reserved-load counter (observed load is read from the task rows) |
| `last_error` / `last_error_at` | the last failure and when it happened |
| `last_checked_at` | when the account was last verified |
| `max_concurrent_tasks` | the concurrency ceiling; `null` means no limit |
| `daily_conversation_limit` | the account's daily ceiling, recorded for the operator; `null` means unknown |
| `connection_ok` / `last_connection_at` | the last reachability probe result and when |
| `error_count` | how many real calls have failed on this account |
| `api_key_hint` | the masked key fragment; never the key |

## Managing accounts (HTTP / CLI)

```bash
# List (key-free views only):
python -m orchestration.cli openhands accounts --json

# Add (verifies the key first):
curl -sS -X POST "$NEXUS_URL/api/openhands/accounts" \
  -H "Authorization: Bearer $NEXUS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"label":"personal","api_key":"sk-...","base_url":"https://app.all-hands.dev",
       "transport":"direct","make_default":true}'

# Edit the non-secret fields (the key is not changed here):
POST /api/openhands/accounts/{id}/edit
  {"label":"...","base_url":"...","transport":"direct|app",
   "default_model":"...|null","max_concurrent_tasks":N|null,
   "daily_conversation_limit":N|null}

# Verify authorization / rotate the key / revoke / set default:
POST /api/openhands/accounts/{id}/verify
POST /api/openhands/accounts/{id}/rotate   {"api_key": "sk-..."}
POST /api/openhands/accounts/{id}/revoke
POST /api/openhands/accounts/{id}/default

# Probe endpoint reachability (distinct from authorization):
POST /api/openhands/accounts/{id}/connection
```

```bash
# The same through the CLI:
python -m orchestration.cli openhands add --label personal --api-key sk-... \
  --base-url https://app.all-hands.dev --transport direct --default \
  --max-concurrent 4 --daily-limit 20
python -m orchestration.cli openhands edit --account <id> --label renamed \
  --max-concurrent 0            # 0 clears the limit
python -m orchestration.cli openhands connection --account <id>
python -m orchestration.cli openhands account verify --account <id>
python -m orchestration.cli openhands account disable --account <id>
python -m orchestration.cli openhands account remove --account <id> --force
python -m orchestration.cli openhands account priority --account <id> --priority 1
```

## Per-account controls

| action | endpoint | effect |
| --- | --- | --- |
| edit | `POST /api/openhands/accounts/{id}/edit {...}` | change label/base URL/transport/model/limits; the key is untouched. A new base URL re-verifies the stored key against it before the change is committed |
| enable / disable | `POST /api/openhands/accounts/{id}/enable {"enabled": false}` | a disabled account is never selected; the key is kept |
| priority | `POST /api/openhands/accounts/{id}/priority {"priority": 1}` | the failover order (lower is tried first) |
| verify | `POST /api/openhands/accounts/{id}/verify` | re-check **authorization**; a rejected key marks the account `invalid` |
| connection | `POST /api/openhands/accounts/{id}/connection` | probe **reachability**; an endpoint that answers (even `401`) is reachable, a transport failure is not. The two are different questions |
| error history | `GET /api/openhands/accounts/{id}/errors` | the account's observed failures and recoveries, newest first (`python -m orchestration.cli openhands errors --account <id>`) |
| remove | `POST /api/openhands/accounts/{id}/remove {"force": true}` | delete; refused while the account has active tasks unless forced |

Disabling is reversible and keeps the key. Revoking (`state='revoked'`) also
clears the default and requires re-adding. Removing deletes the row.

### Limits

`max_concurrent_tasks` is the account's own concurrency ceiling. The pool counts
the tasks actually in flight for the account (the same observed load
`load_balancing` uses) and **skips an account already at its limit** in the
automatic modes (`auto`, `failover`, `load_balancing`). If every usable account
is full, the pool raises a reason that says so ("every usable OpenHands account
is at its concurrency limit") rather than a generic "no account". `MANUAL` is the
exception: a limit steers automatic selection, it does not veto an operator's
explicit choice. `null` means no limit.

`daily_conversation_limit` records the account's daily ceiling for the operator
and the diagnostics. It is informational — the pool never guesses a value and
never enforces a number the service did not report.

## Selection modes

`POST /api/openhands/pool {"selection_mode": "..."}`, or
`python -m orchestration.cli openhands pool set --mode ...`.

| mode | policy |
| --- | --- |
| `manual` | the bound or default account, and **only** that one. A failure is reported; the pool never switches. |
| `auto` | the bound/default account while usable, else the highest-priority usable account. |
| `failover` | `auto` plus an explicit *exclude* set: the caller names accounts it already tried, so a retry lands elsewhere. |
| `load_balancing` | the usable account running the fewest active tasks (priority breaks ties). |

"Usable" means `state='active'`, `enabled`, and health not `unavailable`. An
account marked `degraded` stays selectable: one transient error is not a reason
to stop using it.

**Load is observed, not guessed.** `load_balancing` ranks accounts by the number
of tasks actually in flight for them — read from `nexus_run_tasks` where the
task is `dispatched`/`running`/`verifying` and bound to the account — plus any
capacity a caller reserved with `begin_task`. A task finishing on an account
also marks that account `healthy` again (`AccountRouter.note_success`), so a
single transient failure does not remove an account from the pool for good.

`fallback_order` pins an explicit order (a list of account ids) that overrides
priority for the failover ordering.

```bash
python -m orchestration.cli openhands pool show
python -m orchestration.cli openhands pool set --mode failover \
  --order "id-2,id-1,id-3"
```

## How the worker uses the pool

`build_account_router` returns a router when the **deployment** has any stored
account (any principal), and `None` otherwise (the pre-existing single-key
behaviour). The check is `OpenHandsAccountService.has_any_accounts`, not
`list_accounts(principal)`: the worker runs as `machine:default` and owns no
accounts, while an account added in the studio belongs to `user:<login>`, so a
per-principal check left the pool — and the automatic selection modes — switched
off on every deployment with a login. The router still *selects* per project:
`AccountRouter._principal_for_project` resolves the project's owner as the
principal whose pool applies.

When a router is present, `OpenHandsExecutor` asks it which account runs each
task and dispatches through a client built for that account's base URL,
transport and model. A poll reattaches to the **same** account — a different
account is a different sandbox and cannot read that conversation.

A worker that started when the deployment had no stored account asks again on
every pass (`LabWorker._find_router`), so a connection added later is used
without a restart.

See `FAILOVER.md` for what happens when an account fails.

## Connections a person adds: the address check

The API URL of a connection added through the API is checked before anything
is stored and again whenever a socket is opened to it (`net_guard`): `http` or
`https` only, no credentials in the URL, and every address the host resolves to
must be a public one — not loopback, private, link-local, carrier-grade NAT or
the like, for IPv4 and IPv6. Redirects are never followed. A self-hosted
OpenHands on a private network is allowed by the operator, not the caller:
`NEXUS_OPENHANDS_ALLOWED_HOSTS=host[,host:port…]`, or
`NEXUS_OPENHANDS_ALLOW_PRIVATE=1` for a deployment on a trusted network.

## Executors of a project

A project may be given its own ordered list of connections — a primary and
backups — with `failover` or `distribute` as the strategy
(`/api/v1/projects/<id>/executors`). It then runs on those connections only.
A project without a list keeps the pool rules above. See `DURABLE-RUNS.md`.
