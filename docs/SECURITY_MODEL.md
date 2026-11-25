# NEXUS Security Model

The threat model, the controls that implement it, and where each control is
tested. It supersedes the informal notes in `docs/SECURITY.md` for the model
itself; `docs/SECURITY.md` remains the operator checklist.

## 1. Assets

| Asset | Where it lives | Consequence of disclosure |
|---|---|---|
| OpenHands API keys | `nexus_openhands_accounts.api_key_encrypted`, env | an attacker spends the account's quota, runs work |
| GitHub PATs | `nexus_github_accounts.token_encrypted`, env | read/write to every repository the token reaches |
| LLM provider keys | env only (`GEMINI_API_KEY`, …) | spend and exfiltration of prompts |
| Session cookies + CSRF | `sessions` table, browser | impersonation of the owner |
| `AUTH_SESSION_SECRET`, `NEXUS_SECRET_KEY`, `NEXUS_EVIDENCE_PRIVATE_KEY` | env | forge sessions, decrypt secrets, forge evidence |
| Sandbox `session_api_key` | `conversation_routes`, `cloud_sandboxes` | drive a live sandbox |
| User content | files, workspaces, conversations, memory | privacy |

## 2. The model

- **One owner, one trust boundary.** NEXUS is a single-tenant control plane: one
  operator (`AUTH_USERNAME`) and machine clients holding `WEB_AUTH_TOKEN`. There
  is no self-service registration. "Membership" (`nexus_project_members`)
  isolates *projects from each other within the deployment*; it is not a
  multi-tenant boundary against a hostile owner.
- **The database is trusted; everything else is not.** The app connects as the
  database owner and bypasses RLS by design (RLS is enabled on the session-key
  tables to stop *other* clients, not the app).
- **Least privilege outward.** NEXUS holds only the credentials the operator
  gave it, and every outbound request is checked against an allowlist of public
  addresses.
- **Nothing is proved by a claim.** Only a trusted asymmetric signature makes an
  evidence record `VERIFIED`; only a real process exit code makes a Self-QA check
  pass.

## 3. Controls

### Authentication and sessions

| Control | Implementation | Test |
|---|---|---|
| Password storage | PBKDF2 (`web_auth.hash_password`) | `test_auth.py` |
| Session storage | server-side rows; only `HMAC(secret, token)` stored | `test_auth.py` |
| CSRF | double-submit token on every write | `test_route_auth.py` |
| Sliding TTL with a hard cap | `AUTH_SESSION_TTL_SECONDS`, `AUTH_SESSION_MAX_SECONDS` | `test_auth.py` |
| Sessions bound to credentials | replacing password/username/secret ends every session | `test_auth.py` |
| Login ceilings | per (username, IP), per IP, endpoint-wide | `test_auth.py` |
| Client IP | rightmost trusted hop, all header lines joined | `test_auth.py` |

The client-IP rule is load-bearing: a proxy only *appends* to
`X-Forwarded-For`, so the leftmost entry is caller-controlled. Taking the
rightmost trusted hop is what makes the per-IP lockout engage.

### Authorization

| Control | Implementation | Test |
|---|---|---|
| Project isolation | service methods take the principal; a non-member gets `NotFound` | `test_project_isolation.py` |
| Isolation is a 404, not a 403 | asserted at the service and route boundaries | `test_project_isolation.py` |
| Route protection | every non-public route needs a session or bearer | `test_route_auth.py` |
| MCP scopes | every tool has a scope in `nexus_policy.MCP_TOOL_SCOPES` | `test_cli_mcp.py` |
| Role enforcement | `ProjectService.require_scope` | `test_nexus_projects.py` |

### Secrets

| Control | Implementation |
|---|---|
| One cipher | `secrets.py` (Fernet, `NEXUS_SECRET_KEY`); a missing key is a hard refusal |
| Never returned | every read returns a token-free view; `_VIEW_COLUMNS` is the single shape |
| Never logged | `nexus_policy.install_log_redaction`; `Provider.__repr__`/`GitHubClient.__repr__` redacted |
| Masked hint | `api_key_hint` (first+last four) is what the UI shows |
| Sandbox credentials | a task's Git token is passed as a `StaticSecret`, which the agent-server masks in the agent's output |

Rotation procedure is in section 6.

### Outbound requests (SSRF)

| Control | Implementation | Test |
|---|---|---|
| Public addresses only | `net_guard.check_outbound_url` | `test_nexus_openhands_accounts.py` |
| Re-checked at connect time | `net_guard` on the stored connection | same |
| One switch | the opt-in is read from `nexus_policy`, not a second env var | `test_nexus_openhands_accounts.py` |
| No redirect leak | `CloudClient._NoRedirect`; `OpenHandsClient._SameOriginRedirect` | `test_dispatcher.py`, `test_cloud_integration.py` |
| Base URL scheme | `OPENHANDS_CLOUD_URL` validated as `http(s)` at construction | `test_cloud_integration.py` |

### Input handling

| Control | Implementation | Test |
|---|---|---|
| Archive extraction | `archive_safety.py`: refuses traversal, absolute paths, drive letters, UNC, symlinks, hardlinks, device/FIFO, bombs | `test_archive_safety.py` |
| Decompression bombs | entry count, total/per-file size, compression ratio; decompressed-member bound in terminal output | `test_artifacts.py` |
| Upload limits | per-file and total caps; a body over the route's cap is 413 | `test_file_lab.py` |
| Path validation | a workspace path is normalised and re-checked by a DB `CHECK` | `test_file_lab.py` |
| SQL injection | every query is parameterised; column lists are constants | audit + `test_migration_sql_guards.py` |
| Self-QA command execution | Declared argv avoids implicit shell parsing, but the declared program itself can execute arbitrary code. Host mode is **not** sandboxed; optional Docker mode is described in [SELF_QA_SANDBOX.md](SELF_QA_SANDBOX.md) | `test_selfqa.py`, `test_selfqa_sandbox.py` |

### Evidence and verification

| Control | Implementation |
|---|---|
| Only a trusted signature verifies | `evidence_trust.py`: Ed25519 over a canonical encoding, a trusted keyring by `key_id` |
| HMAC can never verify | asserted: HMAC yields `UNVERIFIED`, never `VERIFIED` |
| Project-scoped evidence signs the project | format v2; editing `project_id` afterwards fails verification |
| Revoked keys | never verify |

## 4. Threat table

| Threat | Control | Residual risk |
|---|---|---|
| Stolen bearer token | one secret, rotate by setting `WEB_AUTH_TOKEN` | no per-token revocation list (single-tenant) |
| Stolen session cookie | `HttpOnly`, `SameSite=Strict`, `Secure`; sliding TTL; credential binding | a stolen cookie is valid until the TTL |
| Cross-project read | service-layer membership; 404 | none known; `test_project_isolation.py` |
| Cross-project write | same, plus the repository allowlist and `authorize_dispatch` | none known |
| SSRF to internal network | `net_guard`, re-checked at connect | an operator who sets `NEXUS_ALLOW_PRIVATE_TARGETS` opts in |
| Redirect key leak | no-redirect / same-origin redirect | none known |
| Secret in a log | redaction filter, redacted `__repr__` | a new logger must use the same filter |
| Secret in an API response | token-free views | none known; asserted |
| Zip-slip / zip-bomb | `archive_safety` before any row is written | none known |
| Unapproved public repository | `acknowledge_public=True` only from the confirmed-action handler | none known |
| Push to `main` | project-bound branch; a task with no branch uses the project's bound branch, never silently the default | an outside writer can still move a branch head |
| Agent writes outside its task | the branch lock holds the branch; the verifier reads the real head | an agent can push to any branch its token reaches |
| Evidence forgery | Ed25519, trusted keyring | a compromised signing key |

## 5. What is explicitly out of scope

- **Multi-tenancy against a hostile owner.** NEXUS is single-tenant; the owner
  can read everything. Isolation is between projects of one owner's team.
- **A compromised host.** An attacker with root reads the environment and the
  database directly; no application control can prevent that.
- **The agent's own sandbox.** An OpenHands agent runs arbitrary code by design;
  the controls are the token's scope and the branch, not a sandbox escape.
- **DoS beyond the login ceilings.** Rate limiting of general API traffic is the
  reverse proxy's job.

## 6. Secret rotation

1. **OpenHands / GitHub / provider keys** — add the new key as a new account in
   the studio, verify it, make it primary, disable the old one, then remove it.
   Sessions and runs are unaffected.
2. **`NEXUS_SECRET_KEY`** — this decrypts every stored secret, so it cannot be
   changed without re-encrypting. Rotate by: export the plaintext secrets,
   set the new key, re-add each account. There is no automatic re-encryption.
3. **`WEB_AUTH_TOKEN`** — set the new value and restart; every machine client
   must be updated. No session is affected (sessions are separate).
4. **`AUTH_PASSWORD_HASH` / `AUTH_USERNAME` / `AUTH_SESSION_SECRET`** — changing
   any of them ends **every** open session (the session key is bound to them).
   That is the intended behaviour of a password change.
5. **`NEXUS_EVIDENCE_PRIVATE_KEY`** — keep the old public key in the keyring so
   existing records still verify; sign new records with the new key.

## 7. Incident response

- **A leaked token**: rotate it (section 6). The token's own scope limits the
  damage; NEXUS stores no copy that outlives the account row.
- **A push to the wrong branch**: the transcript is the record; the branch can be
  reset by an operator. NEXUS never force-pushes.
- **Suspected cross-project access**: `nexus_project_events` is the audit trail;
  every project-scoped change appends one in the same transaction.
- **A forged evidence record**: `reverify` re-checks the signature against the
  trusted keyring; a record signed by an unknown or revoked key does not verify.
