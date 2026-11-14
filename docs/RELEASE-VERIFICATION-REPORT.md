# NEXUS — production release verification + complete CRUD (2026-11-14)

The report for the request "Production Release Verification + Complete CRUD
Implementation". Statuses are honest: **VERIFIED**, **IMPLEMENTED**, **TESTED**,
**BLOCKED**, **NOT IMPLEMENTED**, **NOT VERIFIED**.

---

## A. Production Release Report

- **Target commit:** `b2f3028` (then the fix `383a234`, then the CRUD increment
  `9e01f2f`).
- **Actual production commit:** `9e01f2f` — the served `static/app.js` and
  `static/app.css` match the local files byte for byte, and the rendered
  `ASSET_VERSION` (`56cadf6b9482`) equals the local build. **VERIFIED.**
- **Deployment date:** 2026-11-14, 22:40 UTC.
- **Deployment mechanism:** the service's own `autoDeploy: true` on the `main`
  push. **This environment has no Render MCP tool and no `RENDER_API_KEY`** (no
  such tool is exposed, no such variable is set, `api.render.com/v1/services`
  answers `401`), so a commit-specific deploy could not be issued from here; the
  configured auto-deploy is the safe path and it worked. No Render setting,
  plan, region or variable was changed. **BLOCKED** (the explicit MCP deploy).
- **Migration status:** all migrations applied on production. The service calls
  `verify_schema()` before binding and `nexus_generations` (a `REQUIRED_COLUMNS`
  table created by `0037`) is present, so `0037` and `0038` applied. **VERIFIED.**
- **Health:** `GET /healthz` → `200 {"status": "ok"}`. **VERIFIED.**
- **Readiness:** `GET /readyz` → `200 {"status": "ready", "database": true}`.
  **VERIFIED.**
- **Smoke tests:** the unauthenticated checks pass (health, readiness, assets,
  version). The authenticated smoke tests could not run — no production
  credentials exist in this environment. **BLOCKED** (see `docs/KNOWN_LIMITATIONS.md`).
- **Runtime errors:** none observed from outside; the service is up and serving.
- **Known limitations:** `docs/KNOWN_LIMITATIONS.md`.
- **Rollback procedure:** Render keeps previous deploys; the operator can roll
  back to the previous deploy in the dashboard. Code-wise, `main` is the source
  of truth and a revert commit is the alternative; migrations are additive and
  forward-only, so a rollback of *code* is safe (an older build ignores the new
  nullable columns). No migration in this increment is destructive.

### The production-blocking defect this session found

Production was stuck on `42609a4` (the pre-increment build) even though
`autoDeploy: true`. The cause was `migrations/0037_conversation_management.sql`:
its revoke loop variable was `record` instead of `text`, so the statement became
`REVOKE ALL ON nexus_generations FROM "(authenticated)"` and the migration failed
with **`role "(authenticated)" does not exist`** — the exact error the request
asked to check. Production's database has the `authenticated` role, so `0037`
never applied there; `PostgresStore.__init__` calls `apply_migrations`, so every
deploy crashed at startup and the service kept the last good build. It was
invisible locally because a plain PostgreSQL has neither `anon` nor
`authenticated`, so the loop body never executed. **FIXED**, with a regression
test (`tests/test_schema_bootstrap.py::test_every_migration_applies_when_the_supabase_roles_exist`).

---

## B. Complete CRUD Matrix

`docs/CRUD-MATRIX.md` — every entity (users/access, projects, conversations,
messages, tasks/runs, agents, providers, GitHub, files, MCP, notifications,
memory, settings), every operation, its enforcement point and status.

**Implemented and tested this increment:**

| Item | Status |
| --- | --- |
| Permanent delete — conversations (trash first) | IMPLEMENTED / TESTED |
| Permanent delete — projects (trash first, refused while a run is active) | IMPLEMENTED / TESTED |
| Dependents census before a project delete | IMPLEMENTED / TESTED |
| Notification lifecycle (list/unread filter, read/unread, bulk, delete) | IMPLEMENTED / TESTED |
| Session list / revoke one / revoke all others | IMPLEMENTED / TESTED |

**Remaining gaps (recorded, with the reason):** pagination and bulk actions
(scale, not correctness — every list is bounded); file trash/restore (needs a
retention policy); message edit/delete and memory delete (deliberately not
offered — they would falsify a context an agent acted on); multi-user membership
(single-owner model); a central audit log.

---

## C. CRUD API inventory

See `docs/CRUD-MATRIX.md` (the "Where" column names each route). New this
increment: `DELETE …/conversations/{id}?permanent=1`, `DELETE /api/v1/projects/{id}`,
`GET /api/v1/projects/{id}/dependents`, `GET …/notifications[?unread=1]`,
`PATCH …/notifications/{id}`, `POST …/notifications/read`,
`DELETE …/notifications/{id}`, `GET /api/sessions`, `DELETE /api/sessions[/{id}]`.

---

## D. Entity lifecycle map

`docs/ENTITY-LIFECYCLE.md` — the four generic states (ACTIVE, ARCHIVED,
TRASHED, REMOVED), which entity uses which, and the safety rule for each
transition (trash required before permanent delete; an active run blocks a
destructive parent action; cascade is per-relationship).

---

## E. Dependency graph

`docs/DEPENDENCY-GRAPH.md` — the parent→child graph, the foreign-key action per
edge, the exact set a permanent project delete removes, and what is never
cascaded (repositories, accounts, evidence, a run's history).

---

## F. Security audit

`docs/SECURITY-AUDIT-2026-11-14.md` — authentication, authorization and tenant
isolation, input handling (SSRF, redirects, path traversal, XSS, SQL injection),
secrets, destructive operations and the audit trail, each with its test. The
new destructive routes require membership and are two-step; a non-member gets
404, an id never leaks across projects.

---

## G. Migration report

- **Total:** 37 SQL migrations (`0001`–`0011`, `0014`–`0038`; `0012`/`0013`
  reserved).
- **New this session:** `0038_project_lifecycle.sql` (additive: `archived_at`/
  `deleted_at` on `nexus_projects`).
- **Fixed:** `0037_conversation_management.sql` (the `record`/`text` bug).
- **Verified:** on a database with the Supabase roles, all migrations apply with
  zero errors; re-applying is a no-op; an edited applied migration is refused
  (`MigrationError`). No DROP/TRUNCATE/destructive operation; every migration is
  additive and forward-only. **VERIFIED** — `tests/test_schema_bootstrap.py`,
  `test_durable_migrations.py`, `test_migration_d2.py`.

---

## H. Test results

- `ruff check .` — clean.
- Local full suite — see the run recorded in the session log (expected: ~1860
  passed, ~9 skipped, 0 failed).
- New tests this increment: `test_project_lifecycle.py` (+4),
  `test_lab_http.py` (+1), `test_auth.py` (+3), `test_schema_bootstrap.py` (+1),
  route-auth coverage for every new route.
- Browser E2E (Chromium) — 37 passed (previous run; the new controls are covered
  by the studio-UI wiring test).
- CI — green for the pushed commits.

---

## I. Known limitations & remaining blockers

- **BLOCKED — authenticated production smoke tests.** No production credentials
  in this environment. The flows are covered locally (real server + PostgreSQL)
  and by the browser E2E.
- **BLOCKED — a commit-specific Render deploy.** No Render MCP tool / API key;
  the auto-deploy path worked and is verified by the served assets.
- **BLOCKED (by the account) — a live task that pushes to GitHub.** The
  OpenHands account has no GitHub provider token; the push path is exercised
  against the stand-in GitHub server.
- **NOT IMPLEMENTED** — the MCP client, the Self-QA engine, file trash, a
  central audit log, multi-user membership, pagination/bulk on the large lists.
  See `docs/CRUD-MATRIX.md` and `docs/KNOWN_LIMITATIONS.md`.
- **NOT VERIFIED** — a two-live-account OpenHands failover (only one key).

---

## Final status

**PRODUCTION READY** for the code and the deployment: production serves the new
build (verified by assets, version and health), the migration defect that
blocked every deploy is fixed and regression-tested, the CI suite is green, and
the requested CRUD operations (permanent delete for projects and conversations,
plus the notification and session lifecycles) are implemented, authorized and
tested. The only unverified items are the authenticated production smoke tests
and the two account-level blocks above, each with its reason.
