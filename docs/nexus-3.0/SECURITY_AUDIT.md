# SECURITY AUDIT — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: the existing defences hold; the increment's new resolver adds no
cross-project hole (proven by a new negative test).**

---

## 1. What was re-verified (not re-asserted)

`tests/test_security_checks.py` (36 tests) + `tests/test_route_auth.py` pin the
11 areas the acceptance text names. They passed in this session (130 with the
evidence/SelfQA suites). Each is a *negative* test — it proves the refusal:

| Area | Defence | Test |
| --- | --- | --- |
| API key at rest | Fernet via `NEXUS_SECRET_KEY`; refuses without it | `test_a_key_is_encrypted_not_plaintext`, `test_encryption_refuses_without_a_key` |
| Key masking | first+last 4 chars only | `test_masking_never_reveals_the_middle` |
| Account separation | no fallback to another account's key | `test_a_service_never_falls_back_to_another_accounts_key` |
| Project isolation | non-member → 404, not 403 | `test_a_non_member_gets_not_found` |
| Workspace escape | `..`/absolute refused | `test_paths_cannot_escape_the_workspace` |
| RBAC | owner admin, member not | `test_owner_has_admin_member_does_not` |
| SSRF | loopback/metadata refused unless opted in | `test_metadata_and_private_targets_are_refused_by_default` |
| Command injection | argument-list only, no shell | `test_no_module_runs_a_shell` |
| Log redaction | credential shapes scrubbed | `test_credential_shapes_are_scrubbed` |
| Archive unpacking | zip-slip/absolute refused | `test_a_zip_slip_archive_is_refused` |
| MCP scopes | every tool needs a scope | `test_every_tool_declares_a_scope`, `test_a_tool_without_a_scope_is_refused` |
| Route auth | every protected endpoint 401s without a credential | `test_protected_*_require_auth` |
| CSRF | a session cookie alone cannot write | `test_session_cookie_without_csrf_cannot_write` |

## 2. New surface introduced by this increment, and its negative test

The per-workflow client resolver (`App._workflow_client`) lets a `code` turn's Sr
run on the project's bound connection. The risk: project B inheriting project A's
account. Proven refused:

- `tests/test_openhands_config_persistence.py::
  test_the_workflow_resolver_cannot_reach_another_projects_account` — two
  projects, only A bound; a workflow in B raises `NotFound` and falls back to the
  process client, **never** A's account.

The resolver reads the account by id from `nexus_projects.openhands_account_id`
(scoped to the workflow's own project) and then calls
`execution_client_for_account(principal, account_id)`, which re-checks the
principal owns the account. A cross-project id would fail that check.

## 3. The Defect-1 fix is also a security property

`add()` no longer overwrites fields a caller did not name. Before, a reconnect
could silently blank a model; now an omitted field keeps its stored value. The
API key is still re-verified against the endpoint before it is stored
(`account_capabilities()`), so a reconnect with a bad key is refused, not stored.

## 4. Secrets never leave

- `OpenHandsAccountService._view` drops `api_key_encrypted`; `_VIEW_COLUMNS` is
  the one key-free shape every read returns.
- `test_the_model_never_appears_as_a_secret_and_the_key_never_leaks` asserts the
  full key is absent from every view.
- The `api_key_hint` shows a fragment (`sk-o…P7U5`) — enough to identify which
  key is stored, never the key.
- The live diagnostics and `/api/providers` return presence only.

## 5. Remaining risks (honest)

- **A secret in the environment is reachable by anything in the process.** The
  redaction filter scrubs log records, not arbitrary reads. Standard for a
  single-process deployment.
- **The live OpenHands account has no GitHub provider token.** That is a
  capability limit, not a vulnerability; it means a push-verified task is
  impossible on this account, which is reported honestly.
- **Two-account failover is BLOCKED** here (one key), so a *live* cross-account
  isolation drill was not possible; it is proven against the local fakes +
  PostgreSQL.
