# NEXUS — Safe Restore Point (2026-11-12)

A restore point was created before the OpenHands Complete Integration work
began, so any change made afterwards can be reverted to a known-good state.

Author: OpenHands agent, NEXUS run `a1e46e80-5c2f-401d-8da0-f38dd3d00d66`,
task `t3` (Create Safe Restore Point).

---

## The restore point

| Item | Value |
|---|---|
| Repository | `Pavel1778/python-dispetcher` (`origin` = `https://github.com/Pavel1778/python-dispetcher.git`) |
| Branch it was taken on | `main` |
| Baseline HEAD | `78416031045c168d8ff9afeb747b9210df671556` (`7841603`, `docs: repository and architecture audit (2026-11-12)`) |
| Working tree at creation | **clean** (`git status --porcelain` empty) |
| Ahead / behind `origin/main` | `0 / 0` |
| Tag | **`preservation/pre-openhands-integration`** (annotated) |
| Tag object | `42529e35a8d369984b6398d169f35cebf200df66` |
| Where it lives | pushed to `origin` — reachable by anyone with read access, and survives the loss of this sandbox |

## Why it is *safe* (not just created)

The tag is **annotated** (it carries the metadata above as its message, not just
a commit pointer) and it is **pushed to `origin`**, so it is durable and
recoverable from a fresh clone.

This is the correction of a real gap. The audit step (`docs/AUDIT-2026-11-12.md`,
§0) recorded a recovery point `preservation/audit-2026-11-12` -> `4097ed6`, but
explicitly noted it was *"local; not pushed"*. That tag does **not** exist in
this checkout and does **not** exist on `origin`: a sandbox-local tag is lost
when the sandbox ends, which is exactly the situation a restore point must
survive. A restore point that cannot be reached after the machine that made it
is gone is not safe. This one is pushed.

It also adds no risk of its own: an annotated tag does not rewrite history, does
not move a branch, does not touch `main`, and can be deleted without a
force-push. Creating and pushing a tag is a non-destructive operation.

## How to restore

A restore is a **checkout of the tag into a detached HEAD** or a new branch —
never a reset of `main`, never a force-push. Inspect first.

```bash
# 1. Fetch the tag (it is on origin).
git fetch --tags origin

# 2. Confirm it points at the baseline commit.
git rev-parse preservation/pre-openhands-integration^{commit}
# expect: 78416031045c168d8ff9afeb747b9210df671556

# 3. Inspect what a change looks like against the baseline.
git --no-pager diff preservation/pre-openhands-integration..HEAD --stat

# 4. Recover: either look around without leaving your branch ...
git switch --detach preservation/pre-openhands-integration

# ... or start a recovery branch from the baseline.
git switch -c restore/pre-openhands-integration preservation/pre-openhands-integration
```

Reverting committed work to the baseline is done on a **new** branch (a revert
or a reset there), and reaches `main` only through review. `main` itself is never
force-pushed.

## What a restore point does and does not cover

- **Covers:** the entire source tree, history and migrations as of
  `7841603` — every file in this repository.
- **Does not cover:** external state. The PostgreSQL database is not in git, so
  a code restore does **not** roll back applied migrations or stored rows.
  Migrations are additive and idempotent by design (see `DEPLOY.md`); a schema
  that has already moved forward is not reverted by checking out an older
  commit. The restore point is a code and history snapshot, not a database
  backup. For the database, use the dump/restore drill in `DEPLOY.md`
  ("Backup and restore").
- **Does not cover:** secrets or environment. `NEXUS_SECRET_KEY`, API keys and
  tokens live in the environment and are never committed (a secret scan over the
  tree finds none).

## Related records

- `docs/AUDIT-2026-11-12.md` — the repository and architecture audit taken at the
  baseline, including the note that its own recovery tag was left local.
- Preservation tags already on `origin` (13 before this one) are the project's
  established convention for frozen snapshots; this tag follows it.
