# Branching, merging and cleanup

## Policy

- **`main` is the integration branch and is always green.** `ruff check .` and
  the full pytest suite pass against a disposable PostgreSQL before anything is
  pushed to it. No force push, ever.
- **Work happens on a branch** and is integrated through a pull request. The
  default prefix for agent work is `arena/<id>`; `nexus/<topic>` is used for
  larger, named efforts.
- **A merge is a design decision, not a mechanical operation.** Two branches
  that implement the same feature twice are not "merged"; one is chosen, the
  other's unique work is ported, and the migration numbers are reconciled.
- **A branch is only deleted when its commits are ancestors of `main`** (or its
  unique work has been ported), and a backup ref exists first.

## Before deleting a branch

1. `git rev-list --count main..origin/<branch>` — if non-zero, it has unique
   commits. Inspect them: `git log --oneline main..origin/<branch>`.
2. `git diff --stat main origin/<branch>` — what it would change. A diff that
   mostly *removes* files is a stale snapshot of an older `main`, not new work.
3. Create a backup ref:
   `git tag backup/pre-<label>/<branch-with-slashes-as-underscores> origin/<branch>`.
4. Only then delete the local and remote branch.

## Backup refs

`backup/pre-master/*` tags preserve the tips of every branch that carried
unique commits at the time of the last synchronization. They are cheap, local,
and never pushed by a normal `git push`. Keep them until the work is confirmed
merged.

## Inventory (this session)

| Branch | State | Action | Result |
| --- | --- | --- | --- |
| `main` | Integration | Kept | Ahead of `origin/main` by the session's commits |
| `arena/ab0d7665-python-dispetcher` | Open PR #38: unique bug fix + §10 suite | Ported into `main` (fix, tests, report); PR closed | Integrated |
| `arena/d7bb7e3c-python-dispetcher` | Unique migration-integrity framework | The one real defect (silent re-run of an edited migration) fixed directly; the manifest/effect framework left out (higher risk, not exercised by the suite) | Partly integrated, rest documented |
| `nexus/mainline-hardening` | Superset of `arena/d7bb7e3c` (same commits + a merge) | Same treatment | As above |
| `nexus/continue-longtask-hardening` | Stale snapshot of an older `main`; its source diff mostly *removes* current files | Not merged | Kept (superseded) |
| `nexus/gap-analysis` | Unique doc `docs/GAP-ANALYSIS.md` | Ported into `main` | Integrated |
| `nexus/ground-truth-verify` | Unique doc + `tools/verify_build_lint_test.sh` | Ported into `main` | Integrated |
| `arena/2161f211`, `arena/44b9ec6d`, `arena/4f942c7f`, `arena/5ee71838`, `arena/78a1a036`, `arena/8b83d99f`, `arena/a54ad8b6`, `arena/c56cbbf3`, `arena/dcb992a2` | Ancestors of `main` (merged) | Deleted (remote) after backup | Merged |
| `audit/pr29`, `docs/intent-and-env`, `nexus/chat-first-workflow`, `nexus/durable-autonomy-multiconn` | Ancestors of `main` (merged) | Deleted (remote) after backup | Merged |

Branches whose work is already in `main` and whose purpose is served are
deleted to keep the list honest. A branch with unique, unfinished work is never
deleted for tidiness; it is kept, with a backup ref, until a decision is made.

## Still open after this session

| Branch / PR | State | Why it is kept |
| --- | --- | --- |
| `arena/ab0d7665` (PR #38) | Closed | Its unique work was ported into `main` (`_continues_analysis`, the §10 suite, the concurrent-runs contract, the P0 report, and the project lifecycle as migration `0038`). The branch is left in place (not deleted) until the operator confirms; its commits are preserved by `backup/pre-master/arena_ab0d7665-python-dispetcher`. |
| `nexus/perf-reliability-lifecycle` (PR #39) | Open | A large, coherent parallel contribution (44 files: tracing/observability, an evidence-based acceptance change in `GitVerifier`, project/conversation lifecycle, `tools/benchmark.py`, several docs). It is based on `42609a4` and its migrations `0037_task_kind.sql` / `0038_lifecycle.sql` collide with `main`'s `0037_conversation_management.sql` / `0038_project_lifecycle.sql`. **Not merged**: merging it as-is would revert the command system, conversation management, stop generation and run history that landed since its base, and its lifecycle work overlaps `main`'s. It needs its own rebase and review. |
| `arena/d7bb7e3c`, `nexus/mainline-hardening` | Kept | The migration-integrity framework. Its one real defect fix (refuse an edited, applied migration) is in `main`; the manifest/effect framework was deliberately not taken (the suite runs `*.sql` directly, so a manifest would not be exercised). `backup/pre-master/*` preserves the tips. |
| `nexus/continue-longtask-hardening` | Kept (superseded) | A stale snapshot of an older `main`; its content is already in `main`. |
| `nexus/gap-analysis`, `nexus/ground-truth-verify` | Kept (content ported) | Their unique docs are in `main`; the branches can be deleted once the operator confirms. |

Deleting the remaining branches is an operator decision; each has a
`backup/pre-master/*` tag, so nothing is lost if they are removed.
