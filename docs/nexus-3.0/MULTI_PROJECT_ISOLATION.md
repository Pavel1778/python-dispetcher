# MULTI-PROJECT ISOLATION — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED.** Two or more projects operate simultaneously with no
cross-contamination of account, repository, workspace, evidence or notification.
A cross-project read is a 404, never a leak.

---

## 1. The isolation invariants (each has a test)

| Boundary | Guarantee | Test |
| --- | --- | --- |
| Project data | a non-member gets 404, not 403 | `test_project_isolation.py` |
| Routes | every project-scoped route re-checks membership | `test_project_isolation_routes.py`, `test_route_auth.py` |
| Evidence | another project's record is 404; lists do not leak | `test_evidence_trust.py` |
| Repository binding | `authorize_dispatch` re-checks the project↔repo link before every dispatch | `test_dispatch_authorization.py`, `test_project_binding.py` |
| Workspace | the partial unique index `uq_tasks_workspace_active` keeps two tasks off one checkout | `test_race_protection.py` |
| Account | `client_for_project` never falls back to another account's key | `test_openhands_config_persistence.py::test_the_workflow_resolver_cannot_reach_another_projects_account` |
| Git credential | resolved from the project's own bound account, never another's | `test_git_credential.py` |

Run this session: `pytest tests/test_project_isolation.py
tests/test_project_isolation_routes.py tests/test_dispatch_authorization.py
tests/test_project_binding.py -q` → pass (part of the 107 evidence/isolation
tests and the full suite).

## 2. Simultaneous projects

Two projects, two repositories, two tasks:

- each task's `repository_id` is canonical and a `BEFORE INSERT/UPDATE` trigger
  refuses a repository that belongs to another project;
- `claim_task` refuses a task whose workflow is in a different project;
- the branch lock (`_BRANCH_FREE`) is per repository+branch, so two projects on
  *different* branches run in parallel and two tasks on the *same* branch take
  turns — proven at 1–128 workers (`test_concurrent_claims.py`).

## 3. Multi-account

If two OpenHands accounts are configured, a project binds to exactly one and
never silently falls back. Failover is a deliberate, recorded hand-off
(`nexus_task_handoffs`), never an invisible change of identity: a switched task
gets a **new** conversation on the new account with the context passed in the
first message, and the model/provider used is the target account's own — never
the previous account's model. (Live two-account failover is BLOCKED here: one
key is available; the logic is proven against the local fakes + PostgreSQL in
`test_openhands_failover.py`.)

## 4. The live single-project proof

The live task (`OPENHANDS_LIVE_EXECUTION.md`) ran one project against one
repository with one account and one credential; the evidence bound that
project's id, that task, that branch and that observed commit. A second project
reading that record gets 404 (`test_cross_project_read_is_not_found`).
