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
  no key field.
- An account is verified against Cloud (`GET /api/settings`) before it is
  trusted. A rejected key marks the account `invalid`, not silently kept.
- Accounts are scoped to a principal. A project may bind exactly one; the
  worker resolves the project's owner as the principal whose pool applies.

## Managing accounts (HTTP / CLI)

```bash
# List (key-free views only):
python -m orchestration.cli openhands accounts --json

# Add (verifies the key first):
curl -sS -X POST "$NEXUS_URL/api/openhands/accounts" \
  -H "Authorization: Bearer $NEXUS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"label":"personal","api_key":"sk-...","base_url":"https://app.all-hands.dev",
       "transport":"direct","make_default":true}'

# Verify / rotate / revoke / set default:
POST /api/openhands/accounts/{id}/verify
POST /api/openhands/accounts/{id}/rotate   {"api_key": "sk-..."}
POST /api/openhands/accounts/{id}/revoke
POST /api/openhands/accounts/{id}/default
```

## Per-account controls

| action | endpoint | effect |
| --- | --- | --- |
| enable / disable | `POST /api/openhands/accounts/{id}/enable {"enabled": false}` | a disabled account is never selected; the key is kept |
| priority | `POST /api/openhands/accounts/{id}/priority {"priority": 1}` | the failover order (lower is tried first) |
| remove | `POST /api/openhands/accounts/{id}/remove {"force": true}` | delete; refused while the account has active tasks unless forced |

Disabling is reversible and keeps the key. Revoking (`state='revoked'`) also
clears the default and requires re-adding. Removing deletes the row.

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

`build_account_router` returns a router when the principal has any stored
account, and `None` otherwise (the pre-existing single-key behaviour). When a
router is present, `OpenHandsExecutor` asks it which account runs each task and
dispatches through a client built for that account's base URL, transport and
model. A poll reattaches to the **same** account — a different account is a
different sandbox and cannot read that conversation.

See `FAILOVER.md` for what happens when an account fails.
