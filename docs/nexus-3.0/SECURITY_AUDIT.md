# SECURITY AUDIT — audited scope: the Python boundary

**Date:** 2026-11-20 · **Verdict: ACCEPTED WITH CONDITIONS** — no critical or high
defect found in the audited scope; the Rust API does not exist, so its security
cannot be assessed.

Scope note: the brief asks for a review of "the candidate Rust and existing
Python boundaries". There is no Rust HTTP API, worker or store in this
repository (see `FORENSIC_BASELINE.md`), so the Rust boundary review is
**BLOCKED — component absent**. The Python boundary was audited in full.

No credential value appears in this document. No production data was read.

---

## 1. Authentication and authorization

**Verified by inspection and by test.**

| Control | Implementation | Evidence |
| --- | --- | --- |
| Reads need a session or a bearer | `_require_read` (`web_server.py:1939`) | `tests/test_route_auth.py` — 11 cases, all 401 without credentials |
| Writes need a session **with CSRF**, or a bearer | `_require_write` (`:1946`) | same |
| Every handler gates before serving data | mechanical scan of `do_GET/POST/PATCH/DELETE` | 4 handlers, each ≥1 gate call, **0** data paths before a gate |
| Public routes are only the intended ones | `/login`, `/login/state`, `/healthz`, `/readyz`, `/logout` | scan output; each response inspected below |
| Cross-project access is 404, not 403 | `require_scope` raises `NotFound` | `tests/test_project_isolation*.py` — 40 cases pass |

### The public routes leak nothing

| Route | Response | Contains |
| --- | --- | --- |
| `/healthz` | `{"status": "ok"}` | nothing else |
| `/readyz` | `{"status": "ready", "database": true}` | a boolean; no host, port, DSN or version |
| `/login/state` | `{"signed_in": <bool>}` | one bit about the caller's own cookie |
| `/login`, `/logout` | the form / a redirect | no data |

`/readyz` deliberately does a real database round-trip (so a process that is up
but cannot reach PostgreSQL reports not-ready), and it does **not** return the
driver's error text, which would name hosts and ports.

---

## 2. SQL injection — actively probed, refuted

**Verified by attack, not by reading.**

Five hostile payloads were sent through the API's free-text fields (project
name, conversation title) against the real server and real PostgreSQL:

```
'; DROP TABLE nexus_projects; --
x' OR '1'='1
"; DELETE FROM nexus_run_tasks; --
\'; UPDATE nexus_projects SET name='pwned'; --
') UNION SELECT password_hash FROM sessions --
```

Afterwards, read back directly from the database:

- `nexus_projects` and `nexus_run_tasks` still exist;
- no row was renamed `pwned`;
- all five payloads were stored **verbatim** as data.

Pinned by `tests/test_sql_injection.py`.

### Why the f-strings are not a risk

`grep` finds several `f"SELECT …"` / `f"UPDATE … SET {sets}"` sites. Inspected
each: the interpolated parts are **module constants** (`_LINK_COLUMNS`,
`_VIEW_COLUMNS`, `_BRANCH_FREE`, `_DEPS_VERIFIED`) or a list of hard-coded
literals (`sets.append("enabled=%s")`). Every *value* travels as a bound
parameter. No caller-supplied string reaches the SQL text.

---

## 3. Secret handling

**Verified.** The token is never logged, returned or rendered:

- `GitHubClient.__repr__` is redacted (`tests/test_github_client.py` asserts it);
- `/api/system` and `/api/capabilities` report **presence** only (a bool);
- `_VIEW_COLUMNS` in `nexus_openhands_accounts.py` is the single key-free shape
  every account read returns; the key lives only in `api_key_encrypted`;
- `nexus_policy.redact_text` scrubs credential-shaped substrings from every log
  record (`RedactingFilter`) and from any command output the Self-QA engine
  stores;
- `tests/test_github_accounts.py` asserts the token never comes back from any
  method.

Stored secrets are Fernet-encrypted with `NEXUS_SECRET_KEY`; a missing key is a
hard refusal (`SecretsError`), never a plaintext write.

---

## 4. SSRF and outbound targets

**Verified, and observed working during this audit.** A connection URL a person
supplies is checked by `net_guard` (public addresses only) and again at connect
time. During this audit a harness that pointed a client at `127.0.0.1` was
**refused**:

```
CloudError: OPENHANDS_CLOUD_URL is not a safe target: refusing to call loopback address: 127.0.0.1
```

The opt-in is a single policy switch (`NEXUS_ALLOW_PRIVATE_TARGETS`), read from
`nexus_policy` — there is no second switch, so a test cannot enable loopback
through a different door than production.

---

## 5. Redirects and credential leakage

**Verified.** `urlopen` re-sends every header (including `Authorization`) to
whatever host a `3xx` names, so:

- `CloudClient` refuses **any** redirect (`_NoRedirect`);
- `OpenHandsClient` follows one only while scheme, host and port stay identical;
- `OPENHANDS_CLOUD_URL` is validated as `http(s)` at construction, because
  `urlopen` would otherwise honour `file://`.

Covered by `tests/test_dispatcher.py` and `tests/test_cloud_integration.py`.

---

## 6. Client identity and login ceilings

**Verified.** The client IP is the **rightmost trusted hop** of
`X-Forwarded-For` (joining every header line first), not the leftmost — the
leftmost is whatever the caller sent, which defeats a per-IP lockout. Two
in-memory ceilings back it up: per IP and endpoint-wide, so a caller who varies
the header still hits a limit. `tests/test_auth.py`.

---

## 7. Archive extraction and path traversal

**Verified.** `archive_safety.py` refuses traversal, absolute paths, drive
letters, UNC, symlinks, hardlinks, device/FIFO entries and bombs (entry count,
total/per-file size, compression ratio) **before any row is written**, and never
writes to disk. `tests/test_archive_safety.py` asserts every rejection by name.

---

## 8. Findings

| # | Severity | Finding | Status |
| ---: | --- | --- | --- |
| 1 | — | No SQL injection reachable through the API | **verified absent** |
| 2 | — | No unauthenticated data route | **verified absent** |
| 3 | — | No secret in logs, API responses or the UI | **verified absent** |
| 4 | low | The Rust API does not exist, so its auth/authorization cannot be assessed. If it is added later it must reuse `_require_read`/`_require_write` semantics and be covered by `test_route_auth.py` before it serves anything. | **open — blocked on the component** |
| 5 | low | `POST /mcp` shares the web server's auth; a new MCP tool must be added to `MCP_TOOL_SCOPES` or it has no scope. A tool added without an entry would be a gap. | **mitigated by a test** (`tests/test_cli_mcp.py`, `test_security_checks.py`) |

No critical or high severity defect was found in the audited scope.
