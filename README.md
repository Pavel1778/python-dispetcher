# Multi-agent orchestration platform

Durable control-plane state for coordinating multiple OpenHands agents.

A **workflow** holds a goal. A **Sr** (senior) OpenHands conversation plans it and
delegates each unit of work to **Jr** tasks through client tools. A dispatcher
runs each Jr to completion, records the result, and feeds it back to the Sr.

```
workflow ──▶ Sr conversation ──sr_create_task──▶ task ──▶ Jr conversation
                     ▲                                        │
                     └────────────── result ──────────────────┘
```

State lives in PostgreSQL; conversations run on OpenHands Cloud. The default
Cloud transport provisions a sandbox and drives its agent-server directly
(the `direct` transport); the legacy app-conversation endpoint is kept behind
`OPENHANDS_TRANSPORT=app`. See `DEPLOY.md` for the deployment guide,
`AGENTS.md` for the repository layout, and `CLOUD-INTEGRATION-MAP.md` for the
Cloud API surface.

## Local development

```bash
export SUPABASE_DB_URL="postgresql://postgres@127.0.0.1:5432/ohp_test"
python3 -m pytest -q                     # unit + integration (needs PostgreSQL)
python3 tests/e2e_local_sr_jr.py         # real Sr→Jr on a local agent-server
python3 tests/e2e_cloud_sr_jr.py         # real Sr→Jr on OpenHands Cloud
```

The end-to-end scripts are not collected by pytest; they need a live LLM (and,
for the Cloud one, `OPENHANDS_API_KEY` and `OPENHANDS_DEFAULT_MODEL`).

## Cross-platform web client (preview)

The dashboard can be installed as a PWA on supported Android, Windows and
Linux browsers after this version is deployed over HTTPS. It is an online web
client, not an APK or native desktop package. Installation, security and known
release blockers are documented in [docs/CLIENTS.md](docs/CLIENTS.md).

## NEXUS roadmap

The proposed NEXUS architecture, current-code audit and migration/acceptance
plan are in [docs/NEXUS-SPEC.md](docs/NEXUS-SPEC.md). The specification is a
**draft** pending the second audit; the existing dispatcher is not yet NEXUS
and its completed tasks are not independently verified QA evidence.

## NEXUS project foundation (first increment)

The existing dashboard now has a **Проекты NEXUS** tab for creating projects
and saving goal drafts. Users can also create and approve versioned plans manually.
Drafts and approvals **do not** start OpenHands, workflows, or QA. Legacy Dispatcher workflows/tasks are deliberately
not attached to projects and remain accessible through the existing owner-level
API. Do not treat those legacy endpoints as project-isolated.

The new API supports `GET/POST /api/v1/projects`,
`GET/PATCH /api/v1/projects/{id}`, and `GET/POST
/api/v1/projects/{id}/goals` plus `GET /api/v1/projects/{id}/events`. All use
existing session/CSRF or bearer authentication. Project and goal reads/writes
are filtered by membership; another principal gets 404. Goals optionally accept
an `Idempotency-Key`; reusing it with different content returns 409.

This is still the existing single-owner authentication system, not a complete
multi-user IAM system. The legacy machine bearer acts as the configured
`AUTH_USERNAME` owner (or `machine:default` when no username is configured),
so it has that owner's project access. Keep this token secret. Membership is
created for the owner and there is no membership-management UI yet. Switching
`AUTH_USERNAME` changes the principal; there is no automatic data transfer.
New NEXUS tables have RLS enabled without client policies, and browser clients
must use the backend, not Supabase direct access. The legacy Supabase RLS
policies are not changed by this increment.

**Deployment:** `migrations/0009_nexus_projects.sql` is additive and leaves
legacy data untouched. This repository auto-applies migrations on startup by
default; review the target's existing migration history and back up before
running the new version against production. Disable `AUTO_MIGRATE` if schema
changes are managed separately. No production migration has been applied as
part of this change. Tests that use `TEST_SUPABASE_DB_URL` delete/truncate data;
use a disposable PostgreSQL database only.

## NEXUS planning and operations

The current NEXUS increment stores versioned, manually authored DAG plans with
validated dependencies, criteria and owner approval. Plan approval does not
dispatch work. See [docs/NEXUS-OPERATIONS.md](docs/NEXUS-OPERATIONS.md) for
API contracts, UI usage, security limits, testing, and deployment precautions.
Migration `0010_nexus_plans.sql` is automatically applied on startup unless
`AUTO_MIGRATE=false`; review and back up the target database before deployment.
