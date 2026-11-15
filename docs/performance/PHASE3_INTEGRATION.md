# NEXUS — Phase 3: Integration & Release Gate

> **STATUS: MEASURED.** Independent reconciliation of the audited
> `engineering/performance-evolution` branch with the current `main`, and the
> release-gate verdict.
>
> Evidence classes: **VERIFIED / MEASURED / INFERRED / UNKNOWN / BLOCKED**.

- **Audited commit:** `100678b85323fca270b199648790d7cb0524614e`
- **Audited base:** `a7442632bb05492f65f72b1bf2e3ec94436625d5`
- **Current main at integration:** `5ebbcec9b69a245c2458fbe131fd75039d103da2`
- **Integration merge commit:** `96e0976f3bd3c48c9ab4655c8eefe872375d1e7a`
  (merge of `origin/main` into `engineering/performance-evolution`)

---

## 1. Reconciliation (VERIFIED)

- `git merge-base engineering/performance-evolution origin/main` = `a744263`
  (the audited base is a common ancestor).
- The merge of `origin/main` into the branch completed **without conflict**
  (`git merge --no-commit` reported "Automatic merge went well").
- No force-push, no history rewrite, no branch deletion; `main` is untouched.

## 2. Conflicts (VERIFIED — none, but three files were touched by both sides)

Both sides edited three files since the base; git merged them cleanly because
the changes are in different regions:

| File | Branch change | Main change (PR #39) | Result |
|---|---|---|---|
| `orchestration/cloud_store.py` | connection pool (`_open_pool`/`_ensure_pool`, `_tx`) | `REQUIRED_COLUMNS`: `nexus_projects` + `nexus_run_tasks.kind` | both kept |
| `orchestration/web_server.py` | accept backlog + gzip | `_project_lifecycle*`, `observability()` | both kept |
| `tests/test_long_task_cases.py` | `_gone()` pool detachment | per-task commit/push | both kept |

Verified after the merge: the pool helpers are present, `_project_lifecycle`
and `observability` are present, the gzip/backlog changes are present, and no
conflict markers remain.

## 3. Migration numbering (VERIFIED — not a collision)

`origin/main` alone already carries two files numbered `0037`
(`0037_conversation_management.sql`, `0037_task_kind.sql`) and two numbered
`0038` (`0038_lifecycle.sql`, `0038_project_lifecycle.sql`). This is
**pre-existing on main**, not introduced by the merge. The migration runner keys
on the **filename** (`schema_migrations.filename PRIMARY KEY`) and applies
`*.sql` in lexical order, so distinct names coexist and an already-migrated
database re-runs nothing. `tests/test_lifecycle_migrations.py` proves this on a
real PostgreSQL 17. No action taken.

## 4. Independent regression (VERIFIED, isolated worktree)

To avoid a shared-resource artefact (see §7), the full suite was run in a
**clean git worktree** at `96e0976` against a **dedicated PostgreSQL 17
cluster** on port 5433:

| Run | Result |
|---|---|
| `pytest tests/ -q` (worktree `96e0976`, isolated cluster) | **1907 passed, 9 skipped, 0 failed** |
| `ruff check .` | All checks passed |
| schema + archive safety + RBAC + lifecycle group | 169 passed, 1 skipped |

## 5. Performance (MEASURED, isolated cluster)

| Metric | Result |
|---|---|
| DB p99 @100 concurrent | 641 ms (single conn) → **38 ms** (pool) |
| API p99 @50 concurrent | ~115 ms (noisy under shared host load; the old backlog gave 2725 ms) |
| Archive extract 1000 members | ~72 ms (unchanged by the merge) |

## 6. Run Management (VERIFIED)

| Operation | Backend | Status |
|---|---|---|
| View run | `GET …/runs/{id}` (run, tasks, report, executions, switches, events, stops, cleanup) | implemented |
| Pause / Resume | `POST …/runs/{id}/{pause,resume}` → `LabStore.set_run_state` | implemented; stops **scheduling** (claim refuses a paused run), does **not** suspend a conversation already running in OpenHands |
| Cancel | `POST …/runs/{id}/cancel` | implemented; settles the run, signals stops |
| Retry failed | `POST …/runs/{id}/retry-failed` | implemented; requeues only unsuccessful tasks |
| Hide / Show | `POST …/runs/{id}/{hide,show}` | implemented |
| Delete (from history) | `DELETE …/runs/{id}` | implemented; terminal runs only |
| Logs / events | `GET …/runs/{id}` → `events`, `executions`, `switches` | implemented |
| Metrics | `GET /api/system` → `db_pool`; `GET /api/dashboard` counts | partial (pool + counts; no per-run latency metrics) |
| Restart | — | **NOT IMPLEMENTED** as a distinct operation |
| Duplicate | — | **NOT IMPLEMENTED** as a distinct operation |

## 7. Environment hazard found during the audit (VERIFIED)

A **second, concurrent OpenHands session** was editing files in the same working
directory and running its own `pytest` against the **same PostgreSQL cluster**
(database `nexus_fresh`). That produced two failures in the first full run here:

- `test_every_migration_applies_when_the_supabase_roles_exist` — the other
  session's migrations attach policies to the **cluster-wide** role
  `authenticated` in its own database, so this test could not drop the role
  (`DependentObjectsStillExist`). It passes on a dedicated cluster.
- `test_a_repository_that_cannot_be_read_is_said_so_under_the_answer` — an
  order-dependent browser race; it reproduces **identically on a clean
  `origin/main` worktree** (same assertion, `tests/test_browser_e2e.py:1051`),
  so it is pre-existing, not a regression.

Both disappear on the isolated cluster. Neither is caused by this branch.

## 8. Verdict

**READY FOR PR**, with the caveats in §6 (restart/duplicate not implemented) and
§7 (a pre-existing browser flake in `main`'s own test).

The integration commit `96e0976` merges `origin/main` into the branch with no
conflict and no lost work; the merged tree passes the full suite (1907/0) and
Ruff on an isolated cluster. No production, Render, or `main` change was made;
no push, merge to `main`, or PR was created.
