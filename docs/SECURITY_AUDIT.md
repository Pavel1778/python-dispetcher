# NEXUS — Security Audit (restored 2026-11-16)

Phase B companion. **Not a penetration test.** Base: consolidated `main`
`42824896` + this branch's F-01 fix. Labels as in the master audit.

## Threat model

Single-tenant operator deployment; one owner; machine token bound to the
owner. Isolation boundary = **project**, not user. No multi-user RBAC —
saying otherwise would be false.

## Results (as consolidated, re-checked at HEAD)

| Area | Result | Label |
| --- | --- | --- |
| Authentication — pbkdf2(SHA-256), per-password salt, `hmac.compare_digest`; legacy upgrade | solid | STATICALLY REVIEWED |
| Sessions — `secrets.token_urlsafe(32)`, hash-only storage, TTL | no dump→replay | STATICALLY REVIEWED |
| CSRF — double-submit per session, not exposed to script | correct | STATICALLY REVIEWED |
| Project isolation — scoped 404s; suite coverage | provider tests present | TESTED (suite) |
| IDOR — no fetch-by-id-without-project found in reviewed routes | reviewed incl. terminal-output/artifacts routes | TESTED (`test_artifacts.py`, 15 tests) |
| SQL injection — parameterized psycopg; internal constant SQL fragments only | reviewed | STATICALLY REVIEWED |
| Command injection — argv lists, never `shell=True` | reviewed | STATICALLY REVIEWED |
| Path traversal — paths recomputed from event content; lexically validated | reviewed + tests | TESTED |
| Archive extraction — symlink/device/FIFO/absolute/UNC/drive-letter refused; size/ratio/count caps; decompression cap on **actual bytes** | reviewed | TESTED (archive tests) |
| SSRF — all server-side outbound via `nexus_policy.check_outbound_url` via `validate_base_url`; default refuses loopback/private/metadata; redirect-free opener | reviewed | STATICALLY REVIEWED |
| XSS — SPA escapes user content; markdown subset local | reviewed | STATICALLY REVIEWED |
| Resource exhaustion — 8 MiB output cap; body-size limits; `max_active_tasks()`; Validated Content-Length | reviewed | STATICALLY REVIEWED |
| Rate limiting — Cloud 429 backoff exists; **no generic HTTP rate limiter** | gap | documented, Low in single-tenant |
| CLI base-URL scheme | **fix restored this branch** (F-01) | TESTED (`test_cli_client.py`) |

## Negative tests this branch adds

`tests/test_cli_client.py` — refuses `file://`, schemeless, `ftp://`
(5 tests, green). No duplicate server-side coverage was fabricated.

## NOT claimed

- No live endpoint called with credentials; no live Cloud round trip.
- No dynamic scan/fuzz; no load-shed of the HTTP layer.
- Findings do not generalise beyond reviewed surfaces.
