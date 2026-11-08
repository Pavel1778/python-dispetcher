# NEXUS CLI — Reference (design + contract)

Status: **design only.** No `nexus` command exists yet. This file is the
contract the Phase 3 implementation must satisfy. When the CLI ships, every
line here is regenerated from `nexus --help` so the document cannot drift from
the code.

---

## 1. Principles

- The CLI is a **thin client** over existing services and the HTTP API. It must
  not duplicate business logic that already lives in `orchestration/`.
- Secrets are read from the environment and never printed, logged, or embedded
  in `--json` output. Credentials show as presence/validity only.
- Every command supports `--json` (machine-readable) and returns a
  machine-readable exit code.
- Works without a TTY; no interactive prompts unless `--yes` is absent and the
  action is destructive.
- Colour on by default when stdout is a TTY; `--no-color` and `NO_COLOR`
  disable it.

## 2. Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | generic failure |
| 2 | usage / bad arguments |
| 3 | configuration error (missing/invalid env) |
| 4 | not found |
| 5 | conflict (state refused the action) |
| 6 | dependency unavailable (DB, Cloud, GitHub) |
| 7 | timeout |
| 8 | refused: destructive action without confirmation |

## 3. Global options

```
nexus [--json] [--no-color] [--timeout SECONDS] [--config PATH] [--yes] <command> ...
```

## 4. Command contract

`P` = planned (not implemented). Nothing is marked implemented until it maps to
real functionality and has a test.

### Meta
| Command | Status | Backed by |
|---|---|---|
| `nexus --help` | P | argparse |
| `nexus version` | P | `APP_VERSION` / `RENDER_GIT_COMMIT` (same source as `/api/system`) |
| `nexus doctor` | P | `DiagnosticsEngine` (SELF_QA §3) |
| `nexus status` | P | `/api/dashboard`, `/api/health` |
| `nexus config check` | P | env validation (same rules as `web_server.main`) |
| `nexus logs` | P | server log stream / file |
| `nexus diagnose` | P | alias of `doctor --verbose` |

### Projects
| Command | Status | Backed by |
|---|---|---|
| `nexus project list` | P | `GET /api/v1/projects` |
| `nexus project create` | P | `POST /api/v1/projects` |
| `nexus project inspect <id>` | P | `GET /api/v1/projects/{id}` |
| `nexus project switch <id>` | P | local config (no server call) |

### Agents
| Command | Status | Backed by |
|---|---|---|
| `nexus agent list` | P | `/api/capabilities` + account list (Phase 6) |
| `nexus agent status` | P | per-account health (Phase 6) |
| `nexus agent inspect <id>` | P | profile/account detail |

### Tasks
| Command | Status | Backed by |
|---|---|---|
| `nexus task list` | P | `GET /api/tasks` |
| `nexus task create` | P | `POST /api/workflows` |
| `nexus task status <id>` | P | `GET /api/workflows/{id}` |
| `nexus task cancel <id>` | P | `POST /api/workflows/{id}/cancel` |
| `nexus task retry <id>` | P | `POST /api/tasks/{id}/retry` |

### GitHub
| Command | Status | Backed by |
|---|---|---|
| `nexus github status` | P | `GET /api/github/status` |
| `nexus github repositories` | P | `GET /api/github/repositories` |
| `nexus github inspect <owner/name>` | P | read routes (Phase 1) |

### MCP
| Command | Status | Backed by |
|---|---|---|
| `nexus mcp list` | P | MCP registry (Phase 5) |
| `nexus mcp status` | P | connection health |
| `nexus mcp inspect <name>` | P | tools/resources/prompts |

### Sandbox
| Command | Status | Backed by |
|---|---|---|
| `nexus sandbox status` | P | Cloud sandbox list (read-only) |
| `nexus sandbox inspect <id>` | P | `SandboxInfo` |

### Database
| Command | Status | Backed by |
|---|---|---|
| `nexus db status` | P | connection + schema guard |
| `nexus db migrations` | P | `schema_migrations` vs `migrations/` |
| `nexus db backup` | P | `pg_dump` to a target path (Phase 2) |
| `nexus db restore-check` | P | restore into a disposable DB and verify (Phase 2) |

### QA
| Command | Status | Backed by |
|---|---|---|
| `nexus qa run` | P | `QaRunner` (SELF_QA §4) |
| `nexus qa report <run-id>` | P | `EvidenceStore` |
| `nexus qa history` | P | `EvidenceStore` |
| `nexus qa repair` | P | `RepairLoop` (SELF_QA §7) |

## 5. OpenHands engineering session flow (brief §4.3)

The CLI exposes a single scripted path so an agent does not have to discover
commands:

```
nexus doctor --json                       # 1. environment
nexus project list --json                 # 2. identify project
nexus project inspect <id> --json         # 3. study state
nexus qa run --category tests --json      # 4. run the needed tests
nexus qa report <run-id> --json           # 5. structured report
# 6-8. plan + edit + re-run (agent side, using its own tools)
nexus qa run --category regression --json # 9. evidence saved by the runner
# 10. hand the report back to the orchestrator
```

## 6. Testing

`tests/test_cli.py` (integration, real server on a disposable DB):
- every command returns its documented exit code on success and on the failure
  it is designed to catch;
- `--json` output parses and contains no secret values;
- `--no-color` produces no ANSI escapes;
- running with stdin closed and no TTY does not hang.

## 7. Non-goals

- No command that exists only to print a canned string.
- No business logic in the CLI that is not also reachable through the API.
