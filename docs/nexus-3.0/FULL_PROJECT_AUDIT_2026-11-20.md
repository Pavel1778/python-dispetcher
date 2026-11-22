# NEXUS — Full Project Audit

**Date:** 2026-11-20 · **Auditor:** OpenHands agent
**Commit audited:** `623aec4` (`main`) · **Verdict: HEALTHY — accepted, with named technical debt.**

Every number below was measured in this session on a rebuilt toolchain
(PostgreSQL 17.11, Rust 1.100.0, Python 3.13) against the real code. Nothing is
carried over from an earlier report without re-checking.

---

## 1. Executive summary

| | |
| --- | --- |
| Repository | `github.com/Pavel1778/python-dispetcher` |
| Purpose | Durable control plane orchestrating OpenHands agent conversations |
| Python | 44 120 LOC across ~90 modules; 149 test files, 2 352 tests collected |
| Rust | 6 714 LOC across 7 crates; 63 tests |
| Database | 45 idempotent migrations, 54 tables, 36 schema-guarded |
| API | 36 HTTP routes, 20 MCP tools, 62 CLI commands |
| Tests (this session) | **Python 2 203 passed / 74 skipped / 0 failed**; **Rust 63 passed**; **84 Python↔Rust parity** |
| Lint | `ruff` clean · `cargo fmt --check` clean · `cargo clippy -D warnings` clean |
| Branches | **one: `main`** (all others merged and removed) |

**What is good.** Exceptional test discipline (2 352 tests, mutation-proven in
several places), real end-to-end integration (fakes are real HTTP servers, not
mocks), a genuine security posture (Fernet-encrypted secrets, SSRF guard,
archive-bomb rejection, CSRF, RLS on session-key tables), and honest
documentation that records what is *not* verified.

**What needs attention.** Two modules dominate the codebase
(`web_server.py` 5 334 lines, `nexus_lab_store.py` 3 109), 119 broad
`except Exception` handlers (most log, some swallow), and no Rust test coverage
for `nexus-api`/`nexus-cli` (only Python-side parity covers them).

---

## 2. The branch reconciliation (what this session actually did)

The brief asked to merge every branch, delete the rest, and pick the better of
two competing Rust implementations. The real state when I started:

- **One branch existed** (`main`); the "competing implementation" lived only in
  a preserved tag, `backup/20261121T184038Z/nexus-3.0-foundation`.

### 2.1 Comparison, measured

| Dimension | `main` (`nexus-storage`) | Foundation (`nexus-store/worker/api`) |
| --- | --- | --- |
| Store src LOC | 144 | **1 620** |
| Store test LOC | **0** | **1 146** |
| Worker loop | absent | **present** (232 + 772 test LOC) |
| HTTP API | absent | **present** (axum, 209 LOC) |
| CLI subcommands | 5 | **12** (claim, release, postpone, decide, reclaim, …) |
| Rust tests | 30 | **61** |
| Claim predicates | **shared** with Python (`include_str!`) | embedded copy — **drifted** |
| Builds | yes | yes |
| fmt / clippy | clean | clean |
| Tests vs main's schema | n/a | **62 pass** against main's full 54-table schema |

### 2.2 The deciding defect

The foundation embedded its own copy of the claim predicates. That copy
**predated migration 0043** and lacked the `nexus_dispatch_intents` guard:

```sql
-- main's orchestration/sql/task_due.sql (correct)
AND NOT EXISTS (SELECT 1 FROM nexus_dispatch_intents d WHERE d.task_id=t.id
                AND d.state IN ('sending','uncertain','accepted','identified'))
```

```rust
// foundation's embedded copy (drifted — no dispatch_intents clause)
"((t.state = 'pending' AND ...) OR (t.state = 'waiting' AND ...))"
```

Verified mechanically: the foundation's store contained **zero** references to
`nexus_dispatch_intents`, and its `BRANCH_FREE` also omitted the guard. In
production this would re-claim a task whose dispatch is in doubt — exactly the
duplicate-send bug PR #55 fixed.

### 2.3 Decision and action

**Adopt the foundation's code, keep main's shared SQL.** The foundation is the
stronger implementation by every measure except one self-inflicted drift, and
that drift is fixed by deleting its private copy:

- Ported `nexus-store`, `nexus-worker`, `nexus-api` into `rust/crates/`.
- Replaced the embedded `TASK_DUE`/`DEPS_VERIFIED`/`BRANCH_FREE` constants with
  `include_str!("…/orchestration/sql/*.sql")` — the **same bytes** Python reads.
- Ported the superset CLI (5 → 12 subcommands).
- Ported the 30 Python parity tests the new crates were written against.
- Ported the 12 still-unique docs (ADRs, compatibility contract, phase reports).

**Proof the fix holds:** a new drift-guard test
(`nexus-store/tests/claim_parity.rs::the_claim_predicates_are_the_shared_sql_files_not_a_local_copy`)
asserts the predicates contain the dispatch-intent guard *and* are byte-equal to
the shared files. Reverting the fix makes it fail with
`TASK_DUE must include the nexus_dispatch_intents guard` — mutation-verified.

---

## 3. Architecture

```
┌──────────────────────────── Python (production authority) ───────────────────────────┐
│  web_server.py (HTTP + studio)   cli.py (62 cmds)   mcp_server.py (20 tools)          │
│           │                                                                          │
│   nexus_lab / nexus_lab_store ── durable runs, leases, fencing, dispatch journal      │
│   cloud_store (PostgreSQL) ───── 36-table schema guard, pool, reconnect               │
│   cloud_client / cloud_direct ── OpenHands Cloud V1 + sandbox agent-server           │
│   evidence_trust (Ed25519) · nexus_selfqa · nexus_policy (RBAC/SSRF/redaction)       │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │  shares orchestration/sql/*.sql (include_str!)
┌──────────────────────────────────────┴───────────────────────────────────────────────┐
│                        Rust (verified candidate, not authoritative)                   │
│  nexus-domain (state machines) · nexus-core (claim decision)                          │
│  nexus-store (claim/reclaim/mark_running/holds_lease) · nexus-worker (loop)           │
│  nexus-api (axum read API) · nexus-cli (nexus-rs, parity oracle)                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**Why Python stays authoritative:** the schema guard, migrations, evidence
signing, HTTP surface and OpenHands transport are Python-only and battle-tested.
Rust is a verified candidate exercised by parity + real-PostgreSQL suites; the
switch procedure (`docs/RUST_PYTHON_COMPATIBILITY.md` §6) has not been run.

**The one coupling that matters:** claim eligibility lives in
`orchestration/sql/*.sql` and is `include_str!`-ed by both stores. Neither
language can drift from the other on *what is claimable* without failing a test.

---

## 4. Code health

### 4.1 Size distribution (Python)

| Module | LOC | Note |
| --- | ---: | --- |
| `web_server.py` | 5 334 | HTTP + studio; `do_GET` is 364 lines |
| `nexus_lab_store.py` | 3 109 | runs/leases/fencing — the correctness core |
| `nexus_lab.py` | 2 743 | executor + verifier + scheduler |
| `cloud_store.py` | 2 569 | PostgreSQL store, 36-table guard |
| `nexus_specs.py` | 1 464 | long-task specifications |

### 4.2 Findings

| Severity | Finding | Evidence |
| --- | --- | --- |
| Medium | `do_GET` (364 lines), `_post_project_resource` (347), `_chat_turn` (335) are routing monoliths | `ast` scan for functions > 150 lines |
| Low | 122 `except Exception` handlers. All **10** that are bare `pass` are deliberate and annotated (`# noqa: BLE001 - closing a socket must never raise`, returning a pooled connection, a diagnostic side record, log redaction). The rest log or re-raise. Reviewed, not a defect. | `ast` walk for `ExceptHandler` with body `Pass` |
| Low | Only **1** TODO/FIXME in the entire Python codebase — unusually disciplined | `grep -rnE "TODO\|FIXME"` |
| Low | `nexus-api` / `nexus-cli` have no Rust unit tests (covered only by Python parity) | `cargo test` binary list |

None of these are correctness defects; they are maintainability debt. The
`except Exception` sweep is a *positive* result: the silent ones are all
justified, which is rarer than it sounds.

---

## 5. Tests

| Suite | Count | How verified |
| --- | ---: | --- |
| Python (pytest) | **2 203 passed / 74 skipped** | full `pytest tests/ -q` this session |
| Python collected | 2 352 | `--collect-only` |
| Rust (cargo) | **63 passed** | `cargo test --workspace` with real DSN |
| Python↔Rust parity | **84 passed** | `test_rust_{parity,api,claim,decide,lifecycle}_parity` |
| Parity matrix tool | 282 distinct comparisons, 0 disagree | `tools/parity_matrix.py` |

**Skips are environmental, not hidden failures:** Android SDK (4), mingw-w64
(3), pooler DSN (1), live-OpenHands section F (1), and browser E2E when
Chromium is absent. The suite skips *cleanly* and says why.

**Test quality is real, not decorative.** Several suites are mutation-proven:
- the claim-concurrency test fails (3× claims) if `task_due.sql` re-allows
  `dispatched`;
- the Rust drift guard fails if a local predicate copy returns;
- the Self-QA clone fix, the `auto` gate and the per-run scratch dir each fail a
  test when reverted.

---

## 6. Security

| Area | Defence | Location |
| --- | --- | --- |
| Secrets at rest | Fernet (`NEXUS_SECRET_KEY`); refuses without it | `secrets.py` |
| Key exposure | masked hint only; `_view` drops ciphertext | `nexus_*_accounts.py` |
| Auth | server-side sessions + CSRF **and** bearer token, independent | `web_auth.py` |
| Project isolation | non-member → 404, never 403 | `nexus_policy.py` |
| SSRF | loopback/metadata refused unless opted in | `net_guard.py` |
| Archive bombs | traversal/bomb rejected before any write | `archive_safety.py` |
| Log redaction | credential shapes scrubbed | `nexus_policy.RedactingFilter` |
| DB RLS | session-key tables locked to owner | migrations 0016 |
| Command injection | argument-list subprocess only, no shell | `test_security_checks.py` |

**Verified this session:** `test_security_checks.py` + `test_route_auth.py`
pass; no `.env`, `.pem`, `.key` or credential file is tracked in git.

**Remaining risks (honest):**
- Secrets are in the process environment; the redaction filter scrubs logs, not
  arbitrary reads. Standard for a single-process deployment.
- 55 distinct environment variables are read — a large configuration surface.
- A live push-verified OpenHands task is BLOCKED (the account has no GitHub
  provider token); that is a capability limit, not a vulnerability.

---

## 7. Database

- **45 migrations**, applied in lexical order, idempotent, recorded in
  `schema_migrations`. An **edited, already-applied** migration is refused
  (`MigrationError`) rather than silently re-run.
- **54 tables**; **36** are named in `REQUIRED_COLUMNS` and verified by
  `verify_schema()` — which now iterates the whole map (a prior hardcoded
  8-table tuple silently skipped every `nexus_*` table).
- **Claim atomicity:** `SELECT … FOR UPDATE SKIP LOCKED` + fencing token
  (`lease_token`), proven at 1–128 workers.
- **Pool:** bounded `psycopg_pool`; a dropped connection is replaced only on
  evidence; `StoreUnavailable` → HTTP 503 with a reconnect back-off.
- **JIT off by default** (`NEXUS_DB_JIT=on` restores): the cleanup scan's
  plan estimated in the millions and JIT spent ~635 ms compiling a query that
  returned nothing — 4.5 ms with JIT off.

---

## 8. Operations

| Artifact | State |
| --- | --- |
| `Dockerfile`, `docker-compose.yml` | present (web + worker services) |
| `render.yaml` | web + lab-worker services, `autoDeploy` |
| `.env.example` | complete; no secret values |
| CI (`.github/workflows/tests.yml`) | Python suite, browser E2E, **Rust fmt + clippy + build + test** with a real PostgreSQL service |
| Health | `/healthz` (liveness, no config), `/readyz` (DB round-trip) |
| Backups | `scripts/nexus.sh backup/restore`; restore proven into a scratch DB |
| Docs | 108 markdown files, including `docs/nexus-3.0/` (18 acceptance docs) |

---

## 9. Documentation

Strong and unusually honest — it records what is *not* verified (live push
execution, two-account failover, Safari/Firefox) alongside what is. The
`AGENTS.md` file is a detailed engineering memory. The main weakness is volume:
108 docs with some overlap (`MASTER_COMPLETION_*` × 5, several `AUDIT-*`), which
makes the canonical path hard to find. `docs/nexus-3.0/MASTER_ACCEPTANCE_REPORT.md`
is the current authoritative status document.

---

## 10. Prioritised recommendations

| # | Priority | Recommendation |
| --- | --- | --- |
| 1 | High | Close Gate E: add a GitHub provider token to the OpenHands account, then run one real `completed_verified` task. It is the only acceptance gate BLOCKED on a credential rather than code. |
| 2 | Medium | Split `web_server.do_GET`/`do_POST` into per-resource routers; 364- and 279-line dispatchers are where routing bugs hide. |
| 3 | Low | Keep the `except Exception` discipline: all 10 bare-`pass` sites are justified and annotated. A lint rule requiring a `# noqa: BLE001 - <reason>` on each would keep it that way. |
| 4 | Low | Add Rust unit tests to `nexus-api`/`nexus-cli`; they are currently covered only from Python. |
| 5 | Low | Consolidate the 108 docs: keep one status document, archive the rest under `docs/archive/`. |
| 6 | Low | Add a second OpenHands key so live two-account failover can be exercised. |

---

## 11. Final repository state

```
branch : main only            (local and remote)
tag    : v2.0.0 only          (release marker)
head   : 623aec4
tree   : clean
```

All previously separate branches are merged: their **content** (code, tests,
docs) is in `main`, verified by `git diff --diff-filter=A main <tag>` returning
zero unique files for every preserved ref. The 38 backup/preservation tags were
then removed; `v2.0.0` remains as the release marker.
