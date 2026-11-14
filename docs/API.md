# API

Every route is under `/api` (the legacy workflow routes are under `/api` too).
Authentication is a browser session (cookie + CSRF) or a Bearer
`WEB_AUTH_TOKEN`. Everything except `/login`, `/login/state`, `/healthz`,
`/readyz`, `/static/*`, `/favicon.svg`, `/manifest.webmanifest` and
`/service-worker.js` requires one of them; a missing credential is `401`.

A read is `GET`; a write is `POST`/`PATCH`/`DELETE`. `404` for a resource that
does not exist **or** that the caller is not a member of (a non-member never
learns a project exists). `409` for a state conflict. `503` when the database
cannot be reached. A route whose body is over its cap is `413`.

## Projects

| method | path | notes |
|---|---|---|
| GET | `/api/v1/projects` | `?archived=1` and `?deleted=1` include those |
| POST | `/api/v1/projects` | `{"name", "description"}` → `201` |
| GET | `/api/v1/projects/{id}` | |
| PATCH | `/api/v1/projects/{id}` | `{"name"}` and/or `{"description"}` |
| POST | `/api/v1/projects/{id}/archive` | sets `archived_at` |
| POST | `/api/v1/projects/{id}/unarchive` | clears it |
| POST | `/api/v1/projects/{id}/delete` | trash (reversible) |
| POST | `/api/v1/projects/{id}/restore` | from the trash |
| GET/POST | `/api/v1/projects/{id}/stats` | what it holds, for the confirmation |
| POST | `/api/v1/projects/{id}/purge` | `{"confirm": true}`; irreversible |
| DELETE | `/api/v1/projects/{id}` | same as `delete` |

See [PROJECT-LIFECYCLE.md](PROJECT-LIFECYCLE.md) for the rules and the 409
guards.

## Conversations

| method | path | notes |
|---|---|---|
| GET | `/api/v1/projects/{id}/conversations` | `?q=&archived=1&deleted=1` |
| POST | `/api/v1/projects/{id}/conversations` | `{"title", "mode"}` |
| GET | `/api/v1/projects/{id}/conversations/{cid}` | |
| PATCH | `/api/v1/projects/{id}/conversations/{cid}` | `{"title"|"mode"|"archived"|"pinned"|"deleted"}` |
| DELETE | `/api/v1/projects/{id}/conversations/{cid}` | trash |
| POST | `/api/v1/projects/{id}/conversations/{cid}/delete` | trash |
| POST | `/api/v1/projects/{id}/conversations/{cid}/restore` | |
| POST | `/api/v1/projects/{id}/conversations/{cid}/purge` | `{"confirm": true}` |
| GET/POST | `/api/v1/projects/{id}/conversations/{cid}/stats` | counts |
| GET | `/api/v1/projects/{id}/conversations/{cid}/messages` | |
| POST | `/api/v1/projects/{id}/conversations/{cid}/messages` | `{"text", "mode"}` |

## Runs

| method | path | notes |
|---|---|---|
| POST | `/api/v1/projects/{id}/goals/{gid}/run` | start a run |
| GET | `/api/v1/projects/{id}/runs` | `?archived=1` for ended runs |
| GET | `/api/v1/projects/{id}/runs/{rid}` | the run, its tasks, executions, events |
| POST | `/api/v1/projects/{id}/runs/{rid}/cancel` | |
| POST | `/api/v1/projects/{id}/runs/{rid}/pause` | |
| POST | `/api/v1/projects/{id}/runs/{rid}/resume` | |
| POST | `/api/v1/projects/{id}/runs/{rid}/tasks/{tid}/switch` | `{"account_id"}` |

## Diagnostics

| method | path | notes |
|---|---|---|
| GET | `/api/observability` | queue depth, stuck tasks, worker heartbeat, per-stage latency |
| GET | `/api/system` | presence of settings, never values |
| GET | `/api/studio` | what is configured, booleans and names only |
| GET | `/api/openhands/diagnostics` | the earned diagnostics report (`?live=1`) |
| GET | `/api/openhands/pool` | the account pool |
| GET | `/api/providers` | the orchestrator cascade (`?live=1`) |

`GET /api/observability` never 500s: everything that can fail is reported as an
`error` field. See [OBSERVABILITY.md](OBSERVABILITY.md).

## MCP

`POST /mcp` speaks MCP Streamable HTTP, reusing the session/CSRF or bearer auth.
A notification returns `202`, a parse error `400`. stdio is
`python -m orchestration.mcp_server`. Every write re-checks project membership.
