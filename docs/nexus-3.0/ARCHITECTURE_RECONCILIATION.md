# Architecture reconciliation

Decision: keep Python/PostgreSQL in the request and execution paths. `rust/crates/nexus-storage` is the **one Rust storage candidate**, not an alternative project database or a worker. No main/production cutover.

`evidence/inventory.json` records every branch/tag in a full temporary mirror (`git clone --mirror`; `for-each-ref`, `ls-tree -r`, `merge-base`, `rev-list --all -- <glob>`). The working checkout was shallow; inspecting it alone would have missed #56 and #57. Local worktree count 1; no stash. Base is the merge of #55 on top of #54. Neither a `nexus/3.0-foundation` ref nor the two cited commit abbreviations resolved. This is an absence claim about **reachable** sources, not a claim about another developer's disk.

| Contract | Python production | `nexus-storage` | `nexus-store` / `nexus-worker` / `nexus-api` |
|---|---|---|---|
| Interface / migrations | HTTP, CLI, MCP, signed evidence, dispatch, checksummed SQL | trusted SQLx `claim_task`, `reclaim_expired`, example probe; same schema | no manifest in reachable history; unavailable for each comparison |
| Isolation / auth | authenticated principal and project membership, project predicates; PG role grants/RLS | explicit project/run, row locks and advisory locks; **no end-user authorization** | unknown, not redundant |
| Leases / recovery | fenced grants, budget + prepared journal, reconciliation and stop/signing debts | bounded reclaim; grant alone does **not** create a prepared dispatch intent | unknown |
| Observability / tests | HTTP/worker integration, SIGKILL tests, reports and benchmark | CLI JSON probe, transaction-level parity only | unverified |

`nexus-core`, `nexus-domain`, `nexus-cli` are pure decisions and caller interfaces, not competing persistence implementations. See `reports/nexus3/{COMPATIBILITY,STORAGE}.md` and `reports/nexus3/dispatch/PROTOCOL.md`. #56 and #57 both diverged at `e866666`, not the current base. #56's tests/claims about adoption do not supersede #55's later lost-reply counterexample. Selected #57's real concurrency/SQL-input tests and lint fixes; retained #55's exact-checkout CI. Did not import old acceptance verdicts. Supply a bundle with missing objects for unique-behaviour porting; until then none of the missing crates is assessed as equivalent.
