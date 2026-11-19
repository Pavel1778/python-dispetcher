# Architecture (current, 2026-11-19)

## Dependency direction (intended, enforced by review + tests)

```
HTTP (web_server Handler)  →  services (nexus_*, openhands_*, github_*)  →  store (cloud_store)  →  PostgreSQL
                               ↑                      ↑
                            CLI (cli.py)          lab_worker.py
```

Invariants:

- Handlers never open SQL connections; services never parse HTTP.
- The CLI and MCP are thin clients over the same HTTP API (`cli.py` speaks HTTP
  only; no direct store imports except where documented for diagnostics).
- The web tier authenticates one configured login; project authorization lives
  in the service layer (`ProjectService._project` / `nexus_policy.require_scope`),
  which every project-scoped route funnels through. Non-members receive
  `NotFound`, never existence.
- Writes flow through the write gate: bearer OR (session + `X-CSRF-Token`);
  an AST test pins the ordering (`tests/test_write_gate.py`).
- Migrations are the only schema authority; applied bodies are checksummed and
  immutable (`MigrationError` on mismatch).

## Layer map and module anchors

| Layer | Modules |
|---|---|
| HTTP / interface | `orchestration/web_server.py` (Handler, App, WebServer), `web_helpers.py` (extracted: IP/proxy parsing, login throttle, gzip negotiation, body caps, title/task views), `web_ui.py`, `multipart.py`, `web_auth.py` |
| API-adjacent CLI/MCP | `cli.py`, `mcp_server.py` (stdio + `POST /mcp`), `nexus_selfqa.py` (bounded QA), `tracing.py` |
| Application services | `nexus_*.py` (projects, lab, conversations, evidence, generation, planner, reports, retry, specs, workflow, policy, binding, executors, context/memory…) |
| Dispatch / orchestration | `control_plane.py`, `dispatcher.py`, `cloud_dispatcher.py`, `cloud_direct.py`, `sr_runtime.py`, `providers.py` |
| Integrations | `openhands_*.py`, `github_*.py`, `cloud_client.py` |
| Persistence | `cloud_store.py` (pool, `_tx`, migrations), `nexus_lab_store.py` (claim engine), `restore_point.py`, `migrations/` (42) |
| Frontend | `orchestration/studio/` (+ `pwa/`), no build step; `tests/studio_render_test.js` |

## Refactoring performed vs. deferred (honest)

Done this pass: behavior-preserving extraction of pure helpers from
`web_server.py` into `orchestration/web_helpers.py` (5,376 → 5,172 lines),
verified by the auth/route/web/write-gate suites (164 tests).

Deferred deliberately: splitting `Handler` (~3.3k lines) into route modules.
The blockers are mechanical, not architectural: routes share `self`-scoped
helpers (`_session`, `_require_write`, `_project_path`, `_json`) and the AST
write-gate test guards ordering; a route-level split must move or duplicate
those contracts atomically. Doing it half-way would risk the single most
security-sensitive ordering in the codebase. It is planned as its own increment
(extraction order: `/api/v1/projects/{id}/github/*` → `openhands/*` → `lab/*`),
not mixed into this change.

## Bounded contexts that are not to be merged

- `nexus_evidence` vs `workflow_verifier` — record vs. verdict; different audit needs.
- `opaque_files` vs `file_lab` — legacy raw-blob uploads vs. managed lab files (same table, provenance-tagged).
- `web_server` vs `lab_worker` — inline loop vs. process; both claim via the same lease protocol.
