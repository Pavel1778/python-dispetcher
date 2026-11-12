# NEXUS security model

This describes what the MVP actually enforces. It is not a claim of complete
security; the open items are listed at the end.

## 0. The permission policy (`orchestration/nexus_policy.py`)

Section 10 of the acceptance text asks that *every* external and potentially
dangerous operation obey the NEXUS permission policy. That policy is one module,
with no dependency on the store, so the HTTP layer, MCP, the CLI and the worker
all consult the same rules:

- **Roles and scopes.** A project member has a role (`owner` or `member`) and a
  role carries a set of scopes (`project:read`, `project:write`,
  `project:admin`, `task:dispatch`, `run:control`, `account:admin`). A service
  asks "does this role carry this scope?", never "is this person an owner?"
  scattered through the code. `ProjectService.require_scope` is the one RBAC
  gate; a non-member is a 404 (never a disclosure), a member without the scope
  is also a 404.
- **MCP tool permissions.** Every MCP tool declares the scope it needs
  (`MCP_TOOL_SCOPES`). A tool absent from the table is refused rather than run,
  so a tool added without an authority is not silently callable. The scope is
  advertised in the tool's schema and enforced in `McpServer.handle`.
- **SSRF guard.** `check_outbound_url` refuses a non-http(s) scheme, a URL with
  embedded credentials, and a loopback, link-local, private-network or
  cloud-metadata target. Private targets are allowed only when the operator sets
  `NEXUS_ALLOW_PRIVATE_TARGETS` (a test session does, because its fakes listen on
  127.0.0.1; a production deployment does not). A metadata address is refused
  even then. The guard is enforced at every operator-supplied base URL:
  `OPENHANDS_CLOUD_URL`, the sandbox agent-server URL, `GITHUB_API_URL` and each
  provider's `<NAME>_BASE_URL`.
- **Secret redaction.** `install_log_redaction` attaches a `logging.Filter`
  that replaces credential-looking substrings (known env secrets, `ghp_…`,
  `github_pat_…`, `sk-…`, `Bearer …`, `user:password@`) with a placeholder, so a
  stray key in a log message or a traceback never reaches a log file. It is
  installed by the web server, the worker and the MCP server at startup.
- **No shell.** `safe_argv` documents and checks the rule that the one external
  program NEXUS runs (`git`, from the restore-point module) is always an
  argument list, never a shell string. A test scans the source for `shell=True`.

The policy is readable at `GET /api/security/policy` (authenticated; names and
booleans only, never a value) and in `policy_report()`.

## 1. Authentication

Two independent paths, neither a substitute for the other:

- **Browser session** (`orchestration/web_auth.py`): PBKDF2 password, a
  server-side session row (only `HMAC(AUTH_SESSION_SECRET, token)` is stored, so
  logout is immediate and a database leak yields no usable cookie), and a
  double-submit CSRF token required on every write.
- **Machine bearer** (`WEB_AUTH_TOKEN`): `Authorization: Bearer ...`, for the
  CLI and scripts.

Login is throttled per IP and endpoint-wide in memory, and locked per
(username, IP) in the database. The client IP is the rightmost *trusted*
`X-Forwarded-For` hop, so a caller cannot rotate a spoofed header to defeat the
limit. All three `AUTH_*` variables and `WEB_AUTH_TOKEN` must be set before the
server will bind `0.0.0.0`.

## 2. Authorization and project isolation

- Every project read and write goes through a service that joins
  `nexus_project_members`; a non-member gets **404**, never a disclosure that the
  resource exists.
- Project-scoped routes (`/api/v1/projects/{id}/…`) enforce membership in the
  service layer, so the check cannot be skipped by a new route that forgets it.
- The machine bearer acts as the configured `AUTH_USERNAME` owner. It is an
  owner-level credential, not a scoped token; keep it secret.
- **Dispatch authorization** (`authorize_dispatch`) is re-run immediately before
  any conversation is created and before a task is queued, for the web API, the
  CLI, MCP and the background dispatcher: project exists, task and workflow share
  it, repository linked + enabled + branch allowed, caller is a member. The
  global allowlist alone is never sufficient for a project-scoped task.
- The database enforces the same invariants: a cross-project repository
  reference and a workflow project change under its tasks are refused by
  triggers, not only by Python.

## 3. Evidence trust

- Ed25519 signatures over a canonical byte encoding; a trusted keyring by
  `key_id`; revoked keys never verify.
- `VERIFIED` requires a trusted, non-revoked key and a valid signature.
  `UNVERIFIED` for unsigned/legacy/unknown-key/expected-commit-mismatch.
  `FAILED` for a tampered field. `BLOCKED` when no crypto library is present.
- **HMAC can never produce `VERIFIED`.**
- Project-scoped records sign `project_id` (format v2); editing the column
  afterwards fails verification. Legacy records stay unscoped.
- The hash chain detects reordering by an honest reader; the signature is the
  real integrity boundary. The store is append-only in practice (no update of
  the signed payload exists); the chain is not a defence against an attacker with
  write access to the table.

## 4. Secrets

- `OPENHANDS_API_KEY`, `GITHUB_TOKEN`, `WEB_AUTH_TOKEN`, `AUTH_*`,
  `NEXUS_EVIDENCE_PRIVATE_KEY` are read from the environment and never logged,
  returned by the API, or rendered in the UI. `GitHubClient.__repr__` is
  redacted. `/api/system` and `/api/capabilities` report presence only. A
  logging filter (`nexus_policy.install_log_redaction`) additionally scrubs
  credential-shaped values from every record, so a stray key in a message is not
  written to a log.
- HTTP clients refuse redirects (`CloudClient` all; `OpenHandsClient` same-origin
  only) so an `Authorization` header is never re-sent to a `3xx` target.
- Every operator-supplied base URL (`OPENHANDS_CLOUD_URL`, the sandbox
  agent-server URL, `GITHUB_API_URL`, each provider's `<NAME>_BASE_URL`) passes
  the SSRF guard (`nexus_policy.check_outbound_url`); a loopback, private or
  metadata target is refused unless `NEXUS_ALLOW_PRIVATE_TARGETS` opts in.
- The CLI never prints the token; `doctor` reports `token_present` only.

## 5. Input handling

- File uploads are opaque bytes, bounded (5 MiB/file, 20 MiB and 100 files per
  project), stored in PostgreSQL, served with `nosniff` and a sandbox CSP. No
  filename is ever used as a filesystem path.
- Ids are validated as UUIDs before any query, so a malformed id is a 404 rather
  than a database error echoed to the client.
- The MCP server exposes no shell, SQL or eval tool.

## 6. Route protection test

`tests/test_route_auth.py` probes every protected endpoint with no credentials
and asserts 401 (the dashboard redirects to `/login`), and asserts the small
public allowlist stays reachable. A protected endpoint added without wiring
`_require_read`/`_require_write` fails this test.

## 7. The security check suite

`tests/test_security_checks.py` covers the eleven areas of section 10 of the
acceptance text, each in the layer that enforces it, against real infrastructure
where a store or a socket is involved:

| Area | What is asserted |
| --- | --- |
| API-key protection | encrypted at rest, masked in views, refuses without a key |
| Account separation | `account:admin` is owner-only; no cross-account fallback |
| Project isolation | a non-member gets 404 for a project and its goals |
| Workspace access | `safe_workspace_path` refuses traversal/absolute/UNC paths |
| RBAC | owner/member scope sets; `require`/`require_scope` refuse correctly |
| SSRF | loopback/metadata/private/scheme/credential targets refused |
| Unsafe command execution | only argument lists; no `shell=True` in any module |
| Secret leaks in logs | the redaction filter scrubs env secrets and key shapes |
| Unsafe archive unpacking | Zip-Slip and absolute members refused |
| Cross-project access | another project's resource is not reachable (RBAC + 404) |
| MCP tool permissions | every tool declares a scope; an undeclared tool is refused |

The HTTP cases run against the real web server: the policy route needs a token
and returns no secret, and adding an account with a metadata base URL is a 400
before any key is stored.

## 8. Open items (real, not exhaustive)

1. Legacy tables use permissive RLS (`USING (true)`). New `nexus_*` tables and
   the secret tables (`conversation_routes`, `cloud_sandboxes`, migration `0016`)
   are locked; the legacy ones are not project-isolated.
2. The `direct` transport selects a repository by instruction, not natively
   (`OPENHANDS_DIRECT_REPO_MODE`; `supports_repository_selection` is `False`).
   Whether the agent actually clones is only provable with a live key.
3. Single-owner auth: no membership-management UI; the bearer is owner-level.

Resolved: the workspace claim is now a database-enforced lock (migration
`0015`); the sandbox session-key tables have RLS (migration `0016`).

Passing the test suite is not, by itself, a claim that the system is secure.
