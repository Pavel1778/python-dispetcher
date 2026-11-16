# NEXUS — Self-QA Architecture

> **STATUS: SPECIFICATION.** Autonomous verification of NEXUS itself. The
> Self-QA engine is **design-only today** (VERIFIED — `docs/SELF_QA_ARCHITECTURE.md`
> is design; `docs/KNOWN_LIMITATIONS.md` records it as not implemented).
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [RUST_DOMAIN_BOUNDARIES](./RUST_DOMAIN_BOUNDARIES.md) §17,
[EXECUTION_ENGINE_SPEC](./EXECUTION_ENGINE_SPEC.md).

---

## 1. Levels

### L1 — Local (no external service)

- Tests (`pytest` / `cargo test`).
- Lint (`ruff` / `clippy`), static analysis.
- Local build.

### L2 — Staging

- PostgreSQL integration (real server, not mocks).
- Browser tests (Chromium).
- Migration tests (fresh + re-apply + refuse-edited).
- API contract tests.
- Security tests (SSRF, archive safety, secret redaction, RBAC).

### L3 — Production

- **Read-only diagnostics by default.**
- **Any write requires separate authorization** (a human gate + a token), never
  granted to the agent automatically.

---

## 2. Verification policy

| Level | May run | May write | Approval |
|---|---|---|---|
| L1 | tests, lint, build | local only | none |
| L2 | integration, browser, migrations | staging DB | none (staging) |
| L3 | read-only diagnostics | **only with explicit authorization** | human gate |

---

## 3. Evidence format

Every Self-QA run produces a record with:

- the commit SHA under test;
- the command(s) run and their exit codes;
- the test counts (passed / failed / skipped);
- the environment (Python/Rust version, PostgreSQL version);
- a signed digest (reuse the Ed25519 evidence trust from `evidence_trust.py`);
- a verdict: `pass` / `fail` / `flaky` / `blocked`.

The record is stored (a table, e.g. `nexus_qa_runs`) and is the only accepted
proof of a QA claim.

---

## 4. Test execution isolation

- Each run executes in a disposable environment (a container or a fresh schema).
- The L2 database is a **separate** database from production; the DSN is a
  throwaway credential (as CI already does).
- No run touches production data.

---

## 5. Agent permissions

- The Self-QA agent has the authority of an authenticated operator **for
  read-only diagnostics** on L3, and the authority of a CI runner on L1/L2.
- It **must not** receive unbounded production permissions.
- Its credentials are scoped: a read-only production token; a staging token for
  L2.

---

## 6. Retry budgets, cost limits, repair loops

- **Retry budget:** a bounded number of retries per check (e.g. 2), then the
  check is `fail` (never an infinite loop). This mirrors the bounded-retry
  discipline in `nexus_lab`.
- **Cost limits:** a per-run cap on LLM tokens/spend; exceeding it stops the
  run (`blocked`), it does not continue.
- **Repair loops:** a Self-QA run may propose a fix, but applying it is a
  human-gated step (a PR), never an automatic production write.

---

## 7. Human approval gates

- Any L3 write.
- Any schema change.
- Any deployment.
- Any change to `render.yaml` or Render settings.

---

## 8. Audit logging

Every Self-QA run, its evidence and every gate decision are recorded
(`nexus_qa_runs`, `task_events`). A run that touches production (read-only) is
logged with the operator principal.

---

## 9. Safety model summary (VERIFIED as a rule)

**Self-QA must never be able to obtain unbounded production permissions.** The
default is read-only; every write is a separate, human-gated authorization; the
agent's credentials are scoped and audited.
