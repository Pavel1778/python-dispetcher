# NEXUS — Master Completion Decisions (ADR log)

Every architectural decision that is safe and reversible is recorded here with
a date, the choice, and the reasoning. Decisions that touch data loss, security,
the public API or irreversible operations are marked **OWNER DECISION** and are
not taken unilaterally.

---

## ADR-001 — Phase 0 produces documents on a branch, not on `main`

Date: 2026-11-08. Status: accepted.

Context: the docs commit `dff2966` had been placed directly on local `main`,
violating the programme rule "never modify `main` directly".

Decision: reset local `main` to `origin/main`; preserve the commit as
`docs/branch-topology-env-table` plus tag `preservation/docs-dff2966`; open PR
#19. All Phase 0 work lands on `nexus/phase0-recon`.

Consequences: `main` is byte-identical to `origin/main`. No history rewrite, no
force-push.

---

## ADR-002 — The repository is the source of truth, not prior reports

Date: 2026-11-08. Status: accepted.

Context: the programme brief referenced migration `0015`, an independent audit
file, and preservation snapshots that do not exist.

Decision: every fact in `MASTER_COMPLETION_AUDIT.md` was re-verified from the
code. Discrepancies are recorded in AUDIT §1.4 rather than silently resolved.

---

## ADR-003 — CI must be fixed before any acceptance evidence

Date: 2026-11-08. Status: accepted.

Context: `.github/workflows/tests.yml` pins `python-version: "3.12.6"`, which
the Ubuntu 26.04 runner no longer provides. Lint and tests never run in CI.

Decision: fix the pin on its own small branch/PR in Phase 1. Until then, local
runs are the only evidence, and they are labelled as such.

---

## ADR-004 — No local sandbox; execution stays remote

Date: 2026-11-08. Status: accepted (documenting reality).

Context: `grep subprocess|docker` in `orchestration/` returns nothing. The
"Sandbox" in this system is OpenHands Cloud's remote sandbox.

Decision: do not introduce local untrusted-code execution. The safe execution
path is the Cloud sandbox. Phase 2 documents this and verifies no NEXUS check
runs untrusted code locally.

---

## §6 — OWNER DECISIONS (blocking Phase 1)

These three cannot be decided unilaterally: they change the DB schema, product
limits, or the public API.

### DECISION-1 — Migration numbering

**Situation:** highest existing migration is `0011`. There is no `0015`.
Both branches add `0011` with an incompatible `nexus_files` table.

**Options:**

- **(a) Recommended.** Author the unified migration as **`0016`** and reserve
  `0012`–`0015` as intentionally unused, documenting the reservation. Rationale:
  matches the brief's stated target number, avoids renumbering already-shipped
  files, and is unambiguous.
- (b) Renumber the nexus branch's `0011_file_lab.sql` to `0012` and reconcile
  inside it. Rejected: it rewrites a file that exists on a published branch.
- (c) Squash both `0011`s into one. Rejected: rewrites shipped history.

**Needs from owner:** confirm (a) or specify otherwise.

### DECISION-2 — File and archive limits

**Situation:** `main` caps 5 MiB/file, 20 MiB and 100 files/project.
`nexus` caps 32 MiB/file (env `NEXUS_MAX_UPLOAD_BYTES`), with archive limits in
`archive_safety.py`.

**Recommendation:**

| Limit | Value | Rationale |
|---|---|---|
| Per file | 32 MiB | source archives routinely exceed 5 MiB |
| Per project | 512 MiB | 1000 × 32 MiB would be unbounded in practice |
| Files per project | 1000 | generous but finite |
| Archive entries | 2000 | bounded extraction |
| Archive total extracted | 512 MiB | bounded, matches project cap |
| Compression ratio ceiling | 20×, applied above 1 MiB | catches bombs without false positives on small files |
| Nesting depth | 1 (no nested archives) | simplest safe policy |

**Needs from owner:** confirm or adjust the numbers.

### DECISION-3 — Canonical GitHub flow

**Situation:** two confirmation mechanisms exist.

- `main`: `github_confirmations.py`, routes `/api/github/prepare|confirm`,
  gated by `GITHUB_ENABLE_WRITES` + session + CSRF + one-use token.
- `nexus`: `nexus_actions.py` + `GitHubWriter`, routes
  `/api/v1/projects/{id}/github/*`, session-bound, one-use, project-scoped.

Both enforce: human session + CSRF + separate confirmation + explicit
public-repo acknowledgement. They differ in route shape and module layout.

**Options:**

- **(a) Recommended.** Keep `main`'s routes as canonical (they are the shipped
  contract, documented in `DEPLOY.md` and `docs/DEVELOPMENT-LAB.md`), and port
  nexus's project-scoped GitHub **reads** (`list_branches`, `list_commits`,
  `list_issues`, `list_pulls`) as new routes under the same guard.
- (b) Adopt nexus's project-scoped routes as canonical and keep `main`'s as
  deprecated aliases. Higher churn; breaks documented contract unless aliased.

**Needs from owner:** confirm (a) or (b).

---

## ADR-005 — Parallel work uses disjoint file ownership

Date: 2026-11-08. Status: accepted.

Decision: after Phase 1 freezes contracts, work splits into the eight streams in
`MASTER_COMPLETION_ROADMAP.md`. Shared files (`cloud_store.py`,
`web_server.py`) are serialized through the coordinator. No two streams edit one
file concurrently.
