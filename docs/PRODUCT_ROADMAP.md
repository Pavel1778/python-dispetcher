# Product roadmap

What is done, what is next, and what is deliberately out of scope. Ordered by
the value it adds to a working product, not by novelty.

## Done (verified)

- The autonomous development loop: intent → plan → DAG → dispatch → verify →
  evidence → accept → report.
- Project isolation at every boundary (404, never 403).
- Evidence Trust Engine (Ed25519, re-verify).
- Self-QA engine (deterministic, isolated checkout).
- Restart / duplicate / cancel / retry of runs; cancel lock ordering.
- Multi-account OpenHands pool with failover and load balancing (fake + real
  PostgreSQL).
- Rust core with a proven Python parity contract (not in the request path).
- Adaptive Studio, CLI, MCP, PWA, Debian package.
- One-command self-hosting with backup/restore proven by a real round-trip.

## Next (P1 — finish the product)

1. **Self-QA as a default gate.** The gate exists and is opt-in
   (`NEXUS_SELFQA_GATE=1`). The next step is to make it the default for projects
   that declare `.nexus-qa.json`, with a per-project toggle in the Studio.
   *Why not done yet:* running arbitrary checks clones and builds a project,
   which is a cost and a trust decision the operator should make per project.
2. **Bounded autonomous replanning.** A durable run reworks a task that fails
   verification; it does not yet *invent new tasks* from failure evidence. The
   design is bounded (new tasks depend on the failure, the DAG is re-validated,
   iterations and time are capped, the run goes BLOCKED at the budget). *Not
   started:* it needs a careful safety review, and it must never loop.
3. **Studio screens for backend-only capabilities:** File Lab uploads, evidence
   viewer, GitHub create/visibility confirmations, the allowlist editor, the
   diagnostics page. The routes exist and are tested; the screens are the gap.

## Next (P2 — operations)

4. **A named Cloudflare Tunnel as a first-class install target** (a systemd unit
   and a config template), so a stable public URL is one command.
5. **A resource-usage baseline** on the target i3/8 GB laptop (idle, one run,
   several tasks) recorded in `docs/PERFORMANCE.md`.
6. **Android runtime testing** on a real device, and a Windows host run — both
   are build-verified only today.

## Out of scope (P3 — only with a concrete need)

- Multi-user IAM. The product is single-owner by design; adding multi-tenancy
  without a requirement would be a large, unproven surface.
- An MCP *client* (NEXUS → external MCP servers). NEXUS is the MCP server; no
  current flow needs it to be a client.
- Office-document previews, a native iOS app, additional AI providers — none is
  needed to complete the current workflows.

## How to read this roadmap

Each "Done" item names its test in `docs/PRODUCT_STATUS.md`. A "Next" item is not
a promise; it is the honest next step once the current product is solid. Nothing
here is a reason to skip finishing what is already started.
