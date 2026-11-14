# Security audit (2026-11-14)

A focused audit of the areas the request names, with the enforcement point and
the test for each. Statuses: **VERIFIED** (enforced and tested), **FIXED**
(a defect found and corrected), **BLOCKED** (cannot be exercised here).

## Authentication & sessions

- Browser session: PBKDF2 password hash + server-side session rows +
  double-submit CSRF (`web_auth.py`). Only `HMAC(AUTH_SESSION_SECRET, token)` is
  stored; logout is immediate. **VERIFIED** — `tests/test_auth.py`.
- Machine bearer `WEB_AUTH_TOKEN`. **VERIFIED** — `tests/test_route_auth.py`.
- Login ceilings: per-(username, IP) durable lockout plus per-IP and
  endpoint-wide in-memory limits; the client IP is the rightmost *trusted*
  `X-Forwarded-For` hop, never the caller's own leftmost value. **VERIFIED** —
  `tests/test_auth.py` (proxy/lockout section).
- A session belongs to the credentials it was opened with: replacing
  `AUTH_PASSWORD_HASH`/`AUTH_USERNAME`/`AUTH_SESSION_SECRET` ends every open
  session. **VERIFIED**.

## Authorization & tenant isolation

- Every project-scoped route re-checks membership; a non-member gets **404**,
  never 403 (no existence disclosure). **VERIFIED** — `tests/test_route_auth.py`
  fails if a protected endpoint is added without `_require_read`/`_require_write`.
- RBAC: `require_scope` gates each write on the caller's role; a service asks
  for a scope, never hard-codes "is this an owner". **VERIFIED** —
  `tests/test_security_checks.py`.
- IDOR: an id from another project resolves to 404 because the query is scoped
  by `project_id` in the same statement. **VERIFIED** — the isolation cases in
  `test_conversations.py`, `test_security_checks.py`, and the new
  `test_project_lifecycle.py`.

## Input handling

- **SSRF**: `net_guard` refuses loopback/metadata targets for a caller-supplied
  URL, re-checked at connect time; `check_outbound_url` obeys the same policy.
  **VERIFIED** — `tests/test_openhands_connections.py`, `test_security_checks.py`.
- **Redirects**: `CloudClient` refuses *any* redirect; `OpenHandsClient` follows
  one only while scheme/host/port are identical, so a 3xx cannot leak the bearer
  to another host. **VERIFIED** — `tests/test_dispatcher.py`.
- **Path traversal / Zip-Slip / bombs**: `archive_safety.py` is the only place an
  upload is read; it refuses traversal, absolute paths, drive letters, UNC,
  symlinks, hardlinks, device/FIFO entries and bombs before any row is written,
  and never writes to disk. **VERIFIED** — `tests/test_archive_safety.py`.
- **XSS**: untrusted text reaches `innerHTML` only through `markdown.js` /
  `highlight.js`, which escape by construction; the page carries
  `default-src 'self'; script-src 'self'`. **VERIFIED** —
  `tests/studio_render_test.js`, `tests/test_studio_ui.py`.
- **SQL injection**: every query uses parameters; no string interpolation of a
  value. The one `format()` in a migration is `%I` on a role name taken from
  `pg_roles`, never from a caller. **VERIFIED** by review; the codebase has no
  `execute("..." % value)`.

## Secrets

- Every stored secret (GitHub PAT, OpenHands key) is Fernet-encrypted with
  `NEXUS_SECRET_KEY`; a missing key is a hard refusal, never a plaintext write
  (`secrets.py`). **VERIFIED** — `tests/test_github_accounts.py`.
- No secret is returned by any read: `_VIEW_COLUMNS` is the single key-free
  shape; `GitHubClient.__repr__` is redacted. **VERIFIED**.
- No secret in the repository: `.env` is gitignored, only `.env.example` is
  tracked and it holds no value; a diff scan finds only test placeholders.
  **VERIFIED**.
- Log redaction: `install_log_redaction` scrubs credential-looking values from
  every log record. **VERIFIED** — `tests/test_security_checks.py`.

## Destructive operations (this increment)

- **Permanent delete is two-step** and the server enforces it: a live project or
  conversation cannot be removed for good; it must be trashed first. **VERIFIED**
  — `tests/test_project_lifecycle.py`.
- **A census precedes a project delete** (`GET .../dependents`), so the
  confirmation names what is at stake. **VERIFIED**.
- **An active run blocks a destructive parent action.** **VERIFIED**.
- **Account removal is refused while a project depends on it.** **VERIFIED**.

## Audit trail

- `nexus_project_events` records create/update/archive/trash/restore/permanent
  actions with the actor, the kind and the resource id. `_event` is written in
  the same transaction as the change, so the trail cannot disagree with the row.
- **Not implemented:** a central, queryable audit log across *all* entities
  (evidence, runs and conversations each have their own trail; there is no single
  "who changed what" endpoint). Recorded as a gap in `docs/CRUD-MATRIX.md`.

## Not exercised here (BLOCKED)

- Live production smoke tests (no production credentials in this environment).
- GitHub writes against the live API (exercised against `tests/fake_github.py`).
- A two-live-account OpenHands failover (only one key available).
