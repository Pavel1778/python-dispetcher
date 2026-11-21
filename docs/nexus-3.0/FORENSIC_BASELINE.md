# FORENSIC BASELINE — NEXUS 3.0

**Date:** 2026-11-20 · **Auditor:** OpenHands agent · **Status: PROVENANCE FAILURE**

This document records what the repository actually contains, before any audit or
change. Every claim below was checked against the local clone **and** the GitHub
API; where they disagree with the mission brief, the repository wins.

---

## 1. Verdict first

**Three of the four references in the mission brief do not exist.**

| Reference in the brief | Reality |
| --- | --- |
| `main` baseline `2edf3aa70eefb0628fa88997a1d1c095b133e007` | **Exists.** But it is *not* the current `main`. |
| Phase 2 candidate `nexus/3.0-foundation` | **Does not exist** — `GET /branches/nexus%2F3.0-foundation` → **404** |
| Phase 2 commit `9c11f52` | **Does not exist** — `GET /commits/9c11f52` → **422** (not a valid object) |
| Previous Phase 0+1 commit `5a9989b` | **Does not exist** — `GET /commits/5a9989b` → **422** |

Per the mission's own rule ("If the candidate cannot be found … Do not substitute
another branch and pretend it is the candidate"), no substitute was used. The
Phase 2 candidate is **unavailable**, and every Phase 2 audit item that depends
on it is **BLOCKED**, not "passed".

---

## 2. Repository identity (verified)

```
origin  https://github.com/Pavel1778/python-dispetcher.git
```

| Fact | Value |
| --- | --- |
| `origin/main` SHA | `e866666fb69a13e0edc0c7670b9f56e3561b34c0` |
| `origin/main` subject | `Merge pull request #54 from Pavel1778/arena/6f80793e-python-dispetcher` |
| Local branches | `main`, `release/nexus-2.0-pr-ready`, `nexus/phase2-dispatch-durability`, `nexus/3.0-audit` (this one) |
| Remote branches | 11 (listed below) |
| Worktrees | 1 (the clone itself) |
| Stashes | none |
| Tags | 10 `preservation/*` tags |

### Remote branches (all of them)

```
origin/HEAD -> origin/main
origin/arena/3a132869-python-dispetcher
origin/arena/6f80793e-python-dispetcher
origin/arena/71f72168-python-dispetcher
origin/arena/75afb703-python-dispetcher
origin/arena/aa1abcbc-python-dispetcher
origin/arena/e1a9f455-python-dispetcher
origin/feat/run-restart-duplicate
origin/main
origin/nexus/phase2-dispatch-durability
origin/release/acceptance
origin/release/nexus-2.0-pr-ready
```

**No branch is named `nexus/3.0-foundation`.** No tag, stash or worktree holds it
either.

---

## 3. What Phase 2 actually left behind

The mission brief describes six Rust crates: `nexus-domain`, `nexus-core`,
`nexus-store`, `nexus-worker`, `nexus-api`, `nexus-cli`. The repository has
**four**:

| Crate | Lines of Rust | Present |
| --- | ---: | --- |
| `nexus-domain` | 1 025 | yes |
| `nexus-core` | 387 | yes |
| `nexus-storage` | 239 | yes |
| `nexus-cli` | 354 | yes |
| `nexus-store` | — | **absent** |
| `nexus-worker` | — | **absent** |
| `nexus-api` | — | **absent** |

Checked mechanically: for **every** remote branch, `git ls-tree -r --name-only
<branch>` was filtered for `rust/crates/(nexus-worker|nexus-api|nexus-store)/`
and the count was **0 on all 11 branches**. These components do not exist
anywhere in this repository's history, on any ref.

What *does* exist of Phase 2 is the earlier, smaller increment that was merged as
PR #54:

- `nexus-storage` — two operations, `claim_task` and `reclaim_expired`, over
  SQLx, sharing the eligibility predicates with Python via
  `orchestration/sql/*.sql` (`include_str!`).
- `nexus-core` — the scheduler decision logic used by the parity tests.
- `nexus-domain` — pure domain types (state machines, DAG validation, retry
  arithmetic).
- `nexus-cli` — the `nexus-rs` binary used as a parity oracle.

**There is no Rust worker loop, no Rust HTTP API, no Rust run-lifecycle code and
no Rust stop/cancel code in this repository.** The Phase 1 audit items for
"nexus-worker", "nexus-api", `set_run_state`, `_apply_run_state`,
`reclaim_expired_full`, `release_task`, `postpone_task` and `holds_lease` in
*Rust* therefore have nothing to inspect, and are recorded as **BLOCKED —
component absent**, not as passing.

---

## 4. All pull requests (56 total, checked via API)

| # | State | Head branch | Head SHA |
| ---: | --- | --- | --- |
| 56 | open | `nexus/phase2-dispatch-durability` | `1c827462` |
| 55 | open | `arena/75afb703-python-dispetcher` | `7d35ae5e` |
| 54 | closed | `arena/6f80793e-python-dispetcher` | `99239166` |
| 53 | closed | `arena/e1a9f455-python-dispetcher` | `60eba442` |
| 52 | open | `release/nexus-2.0-pr-ready` | `cc509e20` |

(51 earlier PRs are closed; none is `9c11f52`.) **No PR references
`nexus/3.0-foundation` or `9c11f52`.**

Note: PR #54 is **closed and merged** — the earlier hand-over report described it
as an open draft. That is a second, independent discrepancy with the brief.

---

## 5. Divergent implementations?

**None found.** There is no second, unmerged Phase 2 implementation anywhere in
this repository: no branch, tag, stash or worktree contains a Rust worker or
API. The only Rust code is the four crates above, all on `main`.

---

## 6. What this audit can and cannot do

| Mission area | Feasible? | Why |
| --- | --- | --- |
| Phase 0 forensics | **yes** | done above |
| Phase 1 Rust lifecycle audit | **BLOCKED** | the Rust lifecycle components do not exist |
| Phase 1 Python lifecycle audit | **yes** | the real implementation is in `orchestration/` |
| Phase 2 parity expansion | **yes, on the real surface** | `nexus-storage`, `nexus-core`, `nexus-domain` |
| Phase 3 crash consistency | **yes** | the Python dispatch path is real and testable |
| Phase 4 PostgreSQL hardening | **yes** | disposable PostgreSQL 17 is available |
| Phase 5 security audit | **yes** | Python boundaries are real |
| Phase 6 Rust production engineering | **yes, on four crates** | not on three that do not exist |
| Phase 7 performance | **yes** | must re-measure; the brief's numbers are unverifiable |
| Phase 8 executor design | **design only** | no live OpenHands key in this environment by default |
| Phase 10 API migration matrix | **yes, for Python** | the Rust API does not exist to compare |
| Phase 11 CI | **yes** | workflows are real |

Everything marked **BLOCKED** above is blocked by a missing artifact, not by a
test that failed. No audit result in this package was invented to fill a gap.

---

## 7. Environment

| Component | Version |
| --- | --- |
| Python | 3.13.15 |
| PostgreSQL | 17.11 (Debian) |
| Rust | rustc 1.100.0 / cargo 1.100.0 |
| Chromium | 159.0.8154.6 (Playwright build) |

---

## 8. Recommended action for the owner

1. **Locate the real Phase 2 candidate.** It is not in this repository. If it
   exists, it is in a different remote, a different account, or was never
   pushed. Provide the remote URL or the PR number.
2. **Correct the brief's baseline.** `main` is `e866666`, not `2edf3aa`; the
   brief's baseline predates PR #54's merge.
3. Until then, this audit covers **what exists** (the Python orchestrator and the
   four Rust crates), and states plainly what could not be audited.
