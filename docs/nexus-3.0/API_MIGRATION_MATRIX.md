# API MIGRATION MATRIX — Python surface vs Rust

**Date:** 2026-11-20 · **Status: BLOCKED** — there is no Rust API to migrate to.

## 1. The Python surface that must be preserved

Measured from the code, not from a doc:

| Surface | Size |
| --- | ---: |
| HTTP routes (`GET`/`POST`/`PATCH`/`DELETE` dispatch points) | **65** (29 GET, 25 POST, 2 PATCH, 9 DELETE) |
| MCP tools (Streamable HTTP `POST /mcp` + stdio) | **19** |
| Sr client tools delivered to an agent | **7** |
| CLI commands | **15** (`version … qa`) |

## 2. The Rust surface that exists

**None.** There is no `nexus-api` crate (see `FORENSIC_BASELINE.md`). No Rust
route serves anything, so there is no compatibility question to answer and no
route can be marked "migrated".

## 3. Migration matrix

The brief asks for a route-by-route matrix. A row per route is only meaningful
when there is something to compare against; with no Rust API, every row would be
the same value. The matrix is therefore recorded at the level of the capability
groups, with the exact count that must be reproduced:

| Group | Python routes | Rust routes | Verdict |
| --- | ---: | ---: | --- |
| Authentication / session | `/login`, `/login/state`, `/logout`, `/api/session` | 0 | **BLOCKED** |
| Health / readiness | `/healthz`, `/readyz` | 0 | **BLOCKED** |
| Studio status / config | `/api/studio`, `/api/system`, `/api/config`, `/api/capabilities` | 0 | **BLOCKED** |
| Projects | `/api/v1/projects…` (create, list, get, patch, archive, delete, purge, stats) | 0 | **BLOCKED** |
| Runs | `/api/v1/projects/{id}/runs…` (list, detail, pause, resume, cancel, report, retry-failed, delete) | 0 | **BLOCKED** |
| Tasks | `/api/v1/projects/{id}/tasks…`, runs/{run}/tasks/{task}/switch | 0 | **BLOCKED** |
| Conversations | `/api/v1/projects/{id}/conversations…` (CRUD, messages, export, pin, archive, delete) | 0 | **BLOCKED** |
| Files / workspaces / voice | `/api/v1/projects/{id}/files…`, `/workspaces…`, `/voice…` | 0 | **BLOCKED** |
| GitHub integration | `/api/github…`, `/api/v1/projects/{id}/github…`, `/repositories…` | 0 | **BLOCKED** |
| Evidence | `/api/v1/projects/{id}/evidence…`, `…/verify` | 0 | **BLOCKED** |
| Notifications | `/api/v1/projects/{id}/notifications…` | 0 | **BLOCKED** |
| OpenHands accounts / pool / diagnostics / tools | `/api/openhands/…` | 0 | **BLOCKED** |
| Commands registry | `/api/commands` | 0 | **BLOCKED** |
| MCP (Streamable HTTP) | `POST /mcp` | 0 | **BLOCKED** |
| Observability | `/api/observability`, tracing | 0 | **BLOCKED** |

## 4. What the Studio needs, and what must not change

The Studio is 5 670 lines of framework-free front end whose every control calls
a real endpoint. It is **not** being changed by this audit (mission rule). Before
any API migration:

- a visual regression baseline must exist (the earlier Phase 0 work claimed
  9/9 pixel-identical screens; that baseline is not in this repository — see
  `FORENSIC_BASELINE.md` §3);
- `tests/test_route_auth.py` must gain a case for every migrated route, so an
  unauthenticated Rust route cannot ship;
- `tests/test_studio_ui.py` (control ↔ handler ↔ endpoint) must keep passing.

## 5. Consequence

Migration readiness is **REJECTED**, not "in progress": a migration matrix with
zero target routes is not a migration plan. The correct next step is to build
the Rust API, then compare it route by route against this inventory.
