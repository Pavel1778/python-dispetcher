# NEXUS CLI — Reference

Status: **implemented** (`orchestration/cli.py`). This document matches the
shipped `nexus` command. When the CLI changes, update this file in the same
commit.

---

## 1. Principles

- The CLI is a **thin client** over the HTTP API. It duplicates no business
  logic; every authorization check the server performs applies to the CLI too.
- Secrets are read from the environment and never printed, logged, or embedded
  in `--json` output. `nexus doctor` reports presence only.
- Every command supports `--json` and returns a machine-readable exit code.
- Works without a TTY; no interactive prompt.
- `--url`, `--json`, `--no-color`, `--timeout` and `--yes` are accepted before
  or after the subcommand.

## 2. Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | generic failure |
| 2 | usage / bad arguments |
| 3 | configuration error (missing `NEXUS_URL` / `NEXUS_TOKEN`) |
| 4 | not found |
| 5 | conflict (state refused the action) |
| 6 | dependency unavailable (DB, Cloud, GitHub) |
| 7 | timeout |
| 8 | refused (reserved for destructive actions without confirmation) |

## 3. Configuration

| Variable | Meaning |
|---|---|
| `NEXUS_URL` | base URL of the NEXUS server |
| `NEXUS_TOKEN` | bearer token (falls back to `WEB_AUTH_TOKEN`) |

The token is attached as `Authorization: Bearer ...` and never printed.

## 4. Commands (implemented)

### Meta

| Command | Backed by |
|---|---|
| `nexus version` | package version |
| `nexus doctor` | `GET /healthz`, `GET /api/system` (component status) |
| `nexus status` | `GET /api/dashboard`, `GET /api/health` |

### Projects

| Command | Backed by |
|---|---|
| `nexus project list` | `GET /api/v1/projects` |
| `nexus project create <name> [--description] [--idempotency-key]` | `POST /api/v1/projects` |
| `nexus project show <project_id>` | project + repositories + tasks |
| `nexus project repo add <project_id> <owner/name>` | `POST /api/v1/projects/{id}/repositories` |
| `nexus project repo remove <project_id> <owner/name>` | `POST /api/v1/projects/{id}/repositories/remove` |

### Tasks

| Command | Backed by |
|---|---|
| `nexus task list [--project <id>]` | `GET /api/tasks` or the project scope |
| `nexus task create <goal> --project <id> [--repository] [--branch]` | `POST /api/v1/projects/{id}/tasks` |
| `nexus task show <task_id> --project <id>` | `GET /api/v1/projects/{id}/tasks/{task_id}` |
| `nexus task dispatch <task_id>` | `POST /api/tasks/{id}/retry` |

### Workflows

| Command | Backed by |
|---|---|
| `nexus workflow list [--project <id>]` | `GET /api/workflows` or the project scope |
| `nexus workflow show <workflow_id>` | `GET /api/workflows/{id}` |

### Evidence

| Command | Backed by |
|---|---|
| `nexus evidence list --project <id>` | `GET /api/v1/projects/{id}/evidence` |
| `nexus evidence verify <evidence_id> --project <id> [--expected-commit]` | `POST /api/v1/projects/{id}/evidence/{evidence_id}/verify` |

### Agent / QA

| Command | Backed by |
|---|---|
| `nexus agent status` | `GET /api/capabilities`, `GET /api/health` |
| `nexus qa report` | `GET /api/dashboard`, `GET /api/system` |
| `nexus qa verify --project <id>` | evidence verdicts (exit 5 if any FAILED) |

## 5. OpenHands engineering session flow

```
nexus doctor --json                       # 1. environment
nexus project list --json                 # 2. identify project
nexus project show <id> --json            # 3. study state
nexus task create "<goal>" --project <id> --repository owner/name --branch main
nexus workflow show <workflow_id> --json  # 4. watch the execution state
nexus evidence list --project <id> --json # 5. inspect evidence
nexus qa verify --project <id>            # 6. verify the evidence chain
```

## 6. Testing

`tests/test_cli_mcp.py` (integration, real server on a disposable DB):

- the documented exit codes are asserted;
- a missing `NEXUS_URL`/`NEXUS_TOKEN` returns exit 3;
- an unreachable server returns exit 6;
- `--json` output parses and contains no token value;
- project lifecycle create → list round-trips.

## 7. Non-goals

- No command that exists only to print a canned string.
- No business logic in the CLI that is not also reachable through the API.
- No arbitrary shell, SQL or eval.

