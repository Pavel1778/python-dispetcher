# Security Audit — NEXUS (2026-11-18/19)

Scope: branch `arena/aa1abcbc-python-dispetcher`. Method: code review of auth,
input handling, persistence and deployment surfaces; automated security suites
(`tests/test_security_checks.py`, `test_route_auth.py`, `test_auth.py`,
`test_write_gate.py`, `test_project_isolation.py`); a live smoke boot; a
backup/restore drill. Classification: Critical / High / Medium / Low / Info.
Fixes applied where confirmed; nothing below was waved through.

## Verified foundations (measured, not assumed)

- Password storage: `pbkdf2_sha256$<iterations>$<salt>$<digest>`, per-password
  random salt, `hmac.compare_digest` verification; scheme is self-describing so
  iteration counts can be raised without breaking existing hashes.
- Sessions: tokens stored only as hashes (`token_hash`); cookie
  `HttpOnly; SameSite=Strict`; `Secure` when `X-Forwarded-Proto: https` or
  `WEB_COOKIE_SECURE=1`; sliding renewal only on answers that are already
  `no-store`.
- CSRF: every non-public POST requires either the machine bearer or a session
  with a valid `X-CSRF-Token` (`_require_write`); GET never mutates;
  AST-order write-gate regression test in place.
- Authorization: membership-first, `NotFound` for non-members (no existence
  disclosure); scopes enforced via `nexus_policy.require_scope`; 18-surface
  cross-project refusal battery passes (`test_project_isolation.py`).
- Authentication required for all routes except a closed public allowlist
  (`test_route_auth.py`, 100+ probes; `/api/migrations` added to the protected
  set as of this branch).
- Login throttling: per-IP 30/min + global 120/min backstop; in-memory by
  design (documented as a backstop, not authoritative rate limiting).
- CSP: `default-src 'self'; script-src 'self'; style-src 'self';
  object-src 'none'; frame-ancestors 'none'` — no inline scripts; frontend
  marks all dynamic content through an HTML escaper (`MD.escape`).
- SQL: 100% parameterized values; f-string interpolation only for constant
  column/identifier allowlists (reviewed `control_plane`, `nexus_binding`,
  `nexus_openhands_accounts`, `nexus_lab_store`, `nexus_projects`, `nexus_specs`).
- Subprocess: argument lists only (`test_no_module_runs_a_shell`); restore
  points use arg lists; no `shell=True` in the codebase.
- Archives: zip-slip/symlink/bomb refusal with size, member-count and ratio
  caps; nothing is written to disk by the validator (`archive_safety.py`);
  covered by tests including a decompressed-limit bomb.
- SSRF: `net_guard.py` refuses metadata/private/link-local targets by default,
  non-http(s) schemes and credential-bearing URLs; private targets require an
  explicit opt-in; enforced at CloudClient and provider construction.
- Secrets at rest: OpenHands API keys and GitHub tokens encrypted
  (`cryptography`, Fernet-class); never returned in views (masked hints only);
  `.env` is git-ignored; Docker copies no secrets; logs scrub credential
  shapes (`openhands_diagnostics` filter + tests).
- GitHub tokens: bound per account/project; never logged; test asserts the PAT
  never leaves the server in views.
- Uploads: size caps (`_json_body_cap`, route-specific 413 handling), filename
  sanitization, content-type checks, workspace path containment.

## Findings

### Critical — none found.
### High — none found (no unauthenticated write, no auth bypass, no SQLi demonstrated).

### Medium

- **M1 — `Secure` cookie flag depends on deploy-time configuration.** Behind a
  TLS-terminating proxy the app only sees `X-Forwarded-Proto`; if the proxy does
  not set it (or `WEB_COOKIE_SECURE=1` is not configured), session cookies are
  sent without `Secure`. Mitigation documented (`docs/SINGLE_VM.md`,
  `docs/deployment/SINGLE_VM_OPERATIONS.md`: set `WEB_COOKIE_SECURE=1` or
  configure the proxy). Not exploitable on plain-HTTP loopback-only setups;
  severity Medium when exposed publicly without the flag.
- **M2 — Session invalidation after secret rotation is coarse.** Rotating
  `AUTH_SESSION_SECRET` invalidates all sessions (fail-safe), but there is no
  per-session revocation list beyond "delete row"; acceptable for
  single-owner deployments. Planned: none; documented.
- **M3 — Machine bearer is an owner-level credential.** Compromise = full API.
  Mitigations: header-only transmission, never logged, constant-time compare,
  documented rotation via `WEB_AUTH_TOKEN`. By design; risk accepted and
  documented (recommend short-lived secrets + rotation procedure in ops doc).
- **M4 — Error detail varies by route class.** `/api/v1/projects/*` returns a
  generic `internal error`; other routes return `type(e).__name__: message`,
  which can echo internal names (table/file names). Low information disclosure;
  no secrets are ever included (the store strips driver conninfo; messages are
  not shown over anonymous requests since reads require auth). Classified
  Medium-Low; hardening = normalize remaining routes to the generic form (not
  done this pass; see KNOWN_LIMITATIONS).
- **M5 — CSP allows `img-src data: blob:`.** Needed for in-app previews;
  acceptable. Inline styles are blocked. No `script-src` exception exists. OK.

### Low

- **L1 — Login throttle is in-memory.** Resets on restart; bounded per-IP and
  globally regardless. Documented as backstop, not a WAF.
- **L2 — Dependency ranges, not an exact lock** (`psycopg>=3.2,<4`,
  `cryptography>=42,<46`). Docker builds resolve fresh within ranges; supply-chain
  drift possible between rebuilds. Recommend a `requirements.lock` generated by
  `pip-tools` (not added this pass; no new deps introduced).
- **L3 — Version banner** `SrJrDispatcher/2.0` reveals a codename; harmless.
- **L4 — `/readyz` is public** (upgrade: exposes only `{"status","database"}`).
  Required for LB health checks; information-free. OK by design.
- **L5 — `/logout` is CSRF-exempt by deliberate design** (POST-only; needs a
  session or bearer; revokes only the caller's own session). No cross-site
  escalation; an attacker forcing logout only inconveniences the owner.

### Informational

- **I1 — Single-owner web auth model.** Cross-human isolation is enforced at
  the service layer (verified); the web login configures one account. Multi-web-
  user is out of scope; the service layer will keep enforcing membership if web
  accounts are ever added.
- **I2 — Logs.** `log_message` is silenced in the handler; application logs go
  through the scrubbing filter; no request bodies are logged; tokens never
  printed (CLI reports presence only).
- **I3 — Diagnostics endpoints** (`/api/security/policy`, `openhands/diagnostics`)
  return names/booleans/counts only — verified in tests.
- **I4 — Evidence signing key** optional; when unset, evidence is recorded
  unsigned and flagged `UNVERIFIED` in verification rather than skipped silently
  (fail-visible). Doctor/CLI surface the absence.

## Fixes applied in this increment

1. `/api/migrations` added behind the read gate (not public) + regression probe
   in `test_route_auth.py` + functional test in `test_web_server.py`.
2. Cross-project refusal battery `test_project_isolation.py` (18 surfaces) —
   passes; membership-revocation case covered.
3. `nexus_smoke2` drill: `/api/migrations` 401 anon / 200 bearer; scratch DB
   dropped afterwards.

## Remaining risks (owner decision / future work)

- M1/M4 hardening, requirement lock file: see KNOWN_LIMITATIONS.
- Supply chain: CI installs from PyPI without hashes.
- No runtime WAF/rate-limit at the edge (single-VM deployment documents
  `Caddy`/nginx + firewall in front; app throttle is a backstop).
