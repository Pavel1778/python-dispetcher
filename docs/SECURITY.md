# NEXUS security model

This describes what the MVP actually enforces. It is not a claim of complete
security; the open items are listed at the end.

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
  redacted. `/api/system` and `/api/capabilities` report presence only.
- HTTP clients refuse redirects (`CloudClient` all; `OpenHandsClient` same-origin
  only) so an `Authorization` header is never re-sent to a `3xx` target.
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

## 7. Open items (real, not exhaustive)

1. `session_api_key` is stored in `conversation_routes`/`cloud_sandboxes`
   (migration `0007`); those tables need a grant/RLS review.
2. Legacy tables use permissive RLS (`USING (true)`). New `nexus_*` tables are
   locked; the legacy ones are not project-isolated.
3. `claim_task`'s workspace check is not a cross-process lock.
4. Single-owner auth: no membership-management UI; the bearer is owner-level.
5. The `direct` transport does not prove it applied
   `selected_repository`/`selected_branch` to the remote checkout.

Passing the test suite is not, by itself, a claim that the system is secure.
