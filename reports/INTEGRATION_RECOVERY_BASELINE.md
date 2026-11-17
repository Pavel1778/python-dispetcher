# NEXUS — Integration Recovery Baseline (Phases 0–1)

**Date:** 2026-11-17 (UTC) · **Repository:** `Pavel1778/python-dispetcher`
**Active repository:** `/home/user/python-dispetcher` (branch `arena/71f72168-python-dispetcher`)
**Recovery clone:** `/home/user/python-dispetcher/.recovery/repo` (branch `recovery/nexus-integration-revalidated`)
**Scope of this document:** preservation of the pre-recovery state and the
search for the original integration objects. Nothing was modified in the
active working tree; the snapshot below is byte-verified.

---

## 1. Repository identity at the start of recovery (re-checked, not assumed)

| Item | Value (verified 2026-11-17T09:50Z, after a fresh clone at 09:46Z) |
|---|---|
| `git rev-parse HEAD` | `66291614ac2c47b33e825312c9f165f7b38de5cd` |
| Branch | `arena/71f72168-python-dispetcher` (local) |
| `main` / `origin/main` | **`6e5c2b410d531379c3599baa53492c83244551e5`** — changed since the forensic audit |
| Shallow | `.git/shallow` = `6629161…` and `6e5c2b4…` |
| Refs | `refs/heads/arena/71f72168-python-dispetcher` = `6629161…`; `refs/heads/main` = `refs/remotes/origin/main` = `refs/remotes/origin/HEAD` = `6e5c2b4…` |
| Tags / stashes / worktrees | none / none / one (the repository itself) |
| Fetch refspec | `+refs/heads/arena/71f72168-python-dispetcher*:refs/remotes/origin/arena/71f72168-python-dispetcher*` (arena branch only) |
| Working tree | **13 modified tracked files (+696 / −31)** and **4 untracked files** (3 integration deliverables + `PRE_MERGE_FORENSIC_AUDIT.md` from the forensic audit) |
| Reflog | two entries: fresh `clone` (09:46:34Z) → `checkout` to the arena branch (09:46:35Z) |

**Material change discovered during baseline:** the repository was re-created
by a fresh clone at **2026-11-17T09:46:34Z** (later than the forensic audit),
and `main` had moved from `6629161` to **`6e5c2b4`** — the merge of **PR #49**
(*release/acceptance: live-verified autonomous run, Git credential +
bound-branch fixes, Self-QA engine*, merged 2026-11-17T09:24:41Z, merge
commit `6e5c2b410d531379c3599baa53492c83244551e5`). The reconstructed
integration must therefore sit on `6e5c2b4`, preserving PR #49's content;
`6629161` alone is no longer a valid base.

`origin/feat/run-restart-duplicate` exists on the remote and is exactly
`f6e77c9682eb7df296a68f6720e85d89de01f5c7` (== PR #47 head, verified via both
`git ls-remote` and `gh pr view 47`).

**PR #49 content (diff `6629161..6e5c2b4`, 16 files, +1178/−15):** Self-QA
engine (`orchestration/nexus_selfqa.py`, `tests/test_selfqa.py`),
`docs/USER_GUIDE.md`, Git-credential injection for the sandbox
(`tests/test_git_credential.py`), bound-branch dispatch fix
(`web_server.py`), live acceptance recorded in `FINAL_RELEASE_REPORT.md` §16/§17
and the known-issues tables. None of it is in the surviving integration diff,
so the two must be **unioned**, not chosen between.

## 2. Numbers re-checked (none assumed)

| Claim | Re-check | Result |
|---|---|---|
| 13 modified tracked files | `git status --porcelain=v1 -uall` | ✔ 13 |
| +696 / −31 | `git diff HEAD --stat` | ✔ 13 files, 696 insertions, 31 deletions |
| 3 untracked integration files | status + `wc -l` | ✔ `DEPLOYMENT_CHECKLIST.md` 116, `PR46_PR47_FINAL_INTEGRATION_REPORT.md` 447, `tests/test_cancel_lock_order.py` 142 = **705 added lines**; 696+705 = 1401 = the delivered diffstat |
| `PRE_MERGE_FORENSIC_AUDIT.md` | untracked, 496 lines (from the audit, not part of the integration) | ✔ |
| 41 migrations, `0001`–`0040` | `ls migrations/` | ✔ (unchanged by PR #49) |

## 3. Preservation snapshot (Phase 0)

**Location:** `/home/user/recovery-preservation-2026-11-17/` (outside the active repository).
Created 2026-11-17T09:50:06Z. Nothing in it is modified by this recovery after
verification.

Contents:

| File | Role |
|---|---|
| `repo-backup/` | **Complete byte-for-byte copy** of `/home/user/python-dispetcher` (working tree **and** `.git`) |
| `working-tree.patch` | `git diff HEAD --binary` of the 13 modified files |
| `repository-state.txt` | identity/refs/status/`git reflog`/`.git/shallow`/diffstat dump |
| `sha256-manifest.txt` | SHA-256 of all 17 modified+untracked files |
| `verification` | see below |

Verification performed:

1. `diff -a -r --exclude=.git` active repo vs `repo-backup` → **no differences** (every byte of the working tree identical).
2. `diff -a -r` of both `.git` directories → identical at snapshot time.
3. Manifest re-verified: all 17 files in `repo-backup` hash-match the manifest.
4. `git apply --stat` of `working-tree.patch` reproduces the recorded diffstat.

**Note:** the active repo gained only an empty `reports/` directory at
~09:54Z (created for this recovery's deliverables); no existing file was
touched. The empty `.recovery/` path was excluded from git via
`.git/info/exclude` (git metadata only; no refs, no working-tree bytes).
An earlier attempt to use `/home/user/recovery/` failed — the sandbox reaped
that path — so recovery scratch lives **inside** the workspace root instead
and is excluded from git status.

## 4. SHA-256 manifest (committed evidence)

```
079910314e05c886bbca48c79e0439505808b94156ebf7de91f194064bd85870  COMPLETION_LEDGER.md
b557d7ae77cc00094a14351174cd326126e629d17e569ad43d80dfaae55e2f12  FINAL_RELEASE_REPORT.md
74375ab83db237e0685d8daf28c29c6f148a8247c9847b7729b1ebfa01bcf47f  docs/CLI_REFERENCE.md
5947d2aea31784e1a2130bfe3cdb4cf36180a661abe542d758c7997a88444753  docs/CRUD-MATRIX.md
e6731dba3bcfd9881c71d82f5dee9022705dce6c271a4c3b926551dac899caf1  orchestration/cli.py
bde0a51aa631f086aa178ce28fa666edf11c95881659d66ea2b11054ddb49b71  orchestration/mcp_server.py
52dec7af1173a1c217ab794769fdb2db77bc1ac12efdba042a187b073cf9dd24  orchestration/nexus_lab_store.py
452f0fca44d22e602bf68d6803844f619a164aa03cf28baf9f3e5d3ccc5d846f  orchestration/nexus_policy.py
6efb74ba3ca1d18a05b37f3ec3a744d9fa350931217151a31905d953a7def054  orchestration/studio/app.js
7757d21f42c0c3439a3e4cc30ad9bbbe163c30b804e1db00d060a2bde557e2e2  orchestration/web_server.py
a749a35e3487a888cd89999faf37a392ae8bd00593c6ea03d6540e05ab08bc29  tests/test_cli_mcp.py
caf9a0702e2afef984aa6654d5db2503abb6f689c0415cb11fe4ae3fff09eddc  tests/test_route_auth.py
39605d818e92f8a8fdaef168f6dc2af1502a8d9aca5861d470266935483e3569  tests/test_run_history.py
8ef7424c38f59815b8cfe6678286f7900450fc9e3a5a840d3a3123813a625bec  DEPLOYMENT_CHECKLIST.md
425a48e6cf8bbbecb7a33e376afe4ce8fbf490f0434f28cf4a44404b08628bc4  PR46_PR47_FINAL_INTEGRATION_REPORT.md
5648da25f91e096ee30ee3cffdb9b6b34350cda9ac152dde3447cd5840c698d2  PRE_MERGE_FORENSIC_AUDIT.md
d89a9c1e2e843746bb503ce606e97f2990c57cb6daebfc5c70bd26d3390ac99b  tests/test_cancel_lock_order.py
44e6ffe741923d1eac4415a2ff9450828ae493d7c47c41a300616abf079b487f  .git/shallow
```

## 5. Phase 1 — recovery sources investigated (all read-only)

| Source | Search | Result |
|---|---|---|
| This repository (shallow) | `git cat-file -t ec0d553 / 2fbdcad / e2c7575 / ce21e47 / baa3251` | **fatal: could not get object info** for every one |
| | `git fsck --no-progress` | clean — the objects are absent, not dangling |
| | `git stash list` / `git tag` / `git worktree list` | empty / empty / one |
| Filesystem | `find /home /root /tmp /var/tmp -name .git` | only this repo and its preserved copy |
| | grep for the SHAs across `/home`, `/root`, `/tmp`, `/var/tmp` | matches only inside surviving documents (text references, no object stores) |
| GitHub API (`gh api repos/Pavel1778/python-dispetcher/commits/<sha>`) | all five integration SHAs | **HTTP 422 "No commit found for SHA"** — never pushed |
| | `f6e77c9682eb7df296a68f6720e85d89de01f5c7` (PR #47 head) | **exists** (verified 09:55Z) |
| `git ls-remote` | `main`, `refs/pull/47/head` | `6e5c2b4…`, `f6e77c9…` — both verified again before branching |
| Workflow runs (`gh run list`, 20 recent) | any run on the integration branch / merge candidate | **none** — CI never ran on the integration; nearest are PR #47 (`test` success 29m8s @ `f6e77c9`), PR #46 (success), PR #49 (in progress at audit time, success after) |
| CI artifacts / logs | check/artifact listing via `gh` | no artifact exists for the lost commits (their CI was never run; PR-branch logs concern PR branches only and are not evidence for the integrated tree) |
| Backups | `/home/user`, `/root`, `/tmp`, preservation snapshot | no other copies; environment venvs/databases were destroyed with the pre-reset sandbox |

**Conclusion (Phase 1):** the original integration history is **unrecoverable**.
The working tree is the only surviving representation of the integration
changes. What *is* recoverable and used:

- the exact PR #47 head commit `f6e77c9` (GitHub object, hash-verified);
- PR #46's merge (`6629161` == the merge commit of PR #46, GitHub-verified);
- the surviving working-tree content (manifest above);
- the recorded session notes (SHA→change mapping, test/stress numbers), used
  as *claims*, never as evidence for this branch.

**Classification:** this is a **new integration**, not a recovery of the
original history. Every commit on `recovery/nexus-integration-revalidated`
has a fresh, real SHA. Nothing in this document or in the branch may be read
as "the original commits were restored".

## 6. Environment inventory (at recovery start)

- OS: Debian 12 (bookworm), x86_64 · CPU: 2 · RAM: 3.8 GiB · disk ~20 GiB free
- `python3` 3.11.2 (`python3.11`) · `node` v22.22.3
- Network egress limited to: github.com, codeload.github.com, api.github.com,
  registry.npmjs.org, pypi.org, files.pythonhosted.org
- PostgreSQL: **not installed** (no `psql`/`initdb` on PATH at recovery start);
  the pre-reset environment's PG 16.2 cluster and venv are gone
- Docker: unavailable
- The active repository at `/home/user/python-dispetcher` was **not modified**
  by Phase 0–1 (only added: empty `reports/` dir; `.git/info/exclude` gains
  `.recovery/`).
