# Rust / Python Compatibility

The two implementations coexist during the migration. This document is the
contract between them: what must stay identical, how a change to one is checked
against the other, and how the switch is made. Read it with
[`RUST_MIGRATION.md`](RUST_MIGRATION.md), which says what is migrated.

## 1. The rule

**The Python core is authoritative. Rust is a verified candidate.** Nothing in
the running NEXUS service calls Rust. A component becomes authoritative in a
separate, reviewable change that also removes the Python version - never by
adding a Rust path beside a Python one and leaving both live.

That is what stops "duplicate Python and Rust implementations indefinitely"
(requirement, strict engineering rule 3). Until the switch, the parity test is
what keeps the two honest.

## 2. What must be preserved, and how each is checked

The requirement lists ten things a migration must preserve. Each maps to a
mechanism:

| Must be preserved | Mechanism |
|---|---|
| Existing API contracts | Not migrated yet: the HTTP layer is unchanged. When it is, the same JSON shapes and the same status codes are asserted by the existing HTTP tests. |
| Database schema compatibility | No schema change. The Rust code reads no database. When the claim path moves, migrations are untouched and the schema guard (`cloud_store.REQUIRED_COLUMNS`) is the check. |
| Transaction and locking semantics | Not migrated yet. The Rust decision mirrors the *conditions* of the claim SQL; the SQL itself is unchanged. |
| Error behaviour where observable | The Rust error model is compared against Python's exceptions by category (`_reason_class` in the parity test): the wording may differ, the reason may not. |
| Authentication and authorization | Not migrated: no Rust code runs in the service. |
| Idempotency | The plan hash, which binds an approval to a plan version, is compared **byte for byte**. A different hash would break the "a replan must be re-approved" rule. |
| Restart and recovery behaviour | Not migrated: no Rust code holds state. |
| No serialization ambiguity | The contract is JSON with explicit shapes; the parity oracle reads and writes exactly those shapes. |
| Explicit protocol versions | The parity document is versioned by shape; a field the other side does not know is a parity failure, not silently ignored. |
| Graceful fallback during migration | Rust is not in the request path, so there is nothing to fall back from. The parity test skips cleanly when no toolchain is present. |

## 3. The contract, concretely

### Wire shapes

The parity oracle (`nexus-rs parity`) reads one JSON document and writes one:

```jsonc
// input
{
  "run_transitions":        [["running", "cancelled"], ...],
  "workflow_transitions":   [[null, "EXECUTING"], ...],
  "task_transitions":       [["queued", "assigned"], ...],
  "plans":                  [ { "tasks": [ ... ] }, ... ],
  "plan_hashes":            [ { "tasks": [ ... ] }, ... ],
  "cycle_waits":            [ {"cycle": 1, "seed": "t1", "base": 60, "ceiling": 3600}, ... ],
  "refused_waits":          [ {"retry_after": null, "daily": false, "refusals": 0}, ... ],
  "claim_cases":            [ {"run": {...}, "tasks": [...], "task_id": "..."}, ... ]
}

// output
{
  "run_transitions":        [true, false, ...],
  "workflow_transitions":   [false, ...],
  "task_transitions":       [true, ...],
  "plans":                  [ {"ok": true, "tasks": [...]} | {"ok": false, "error": "..."} ],
  "plan_hashes":            ["<sha256 hex>" | null],
  "cycle_waits":            [60.0, ...],
  "refused_waits":          [60.0, ...],
  "claim_cases":            [ {"claimable": true, "reason": null} | {"claimable": false, "reason": "..."} ]
}
```

`nexus-rs claimable` is the single-case form for the scheduler, used by the
real-database parity cases.

### Numbers

Floating-point results (`cycle_wait`, `refused_wait`) are compared with a
tolerance of `1e-9`. Both compute the same expression; the tolerance absorbs the
difference between Python's `float` and Rust's `f64` for the same arithmetic,
and nothing larger.

### The plan hash

Compared **exactly**, no tolerance: it is a SHA-256 hex digest over a canonical
JSON encoding, and a difference of one byte is a different approval. The Rust
`canonical_json` sorts object keys and uses compact separators, matching
Python's `json.dumps(..., sort_keys=True, separators=(",", ":"))`. The parity
test asserts the two agree over plans with nested acceptance lists, long
descriptions and non-ASCII text.

## 4. How a change is checked

Any change to a migrated component on **either** side must keep the parity test
green. The test is the gate:

```bash
cd rust && cargo build            # rebuild the oracle
TEST_SUPABASE_DB_URL=... python -m pytest tests/test_rust_parity.py -q
```

A change that adds a transition to the Rust table and not the Python one (or the
reverse) fails the exhaustive comparison. A change to the normalisation that
drops a field fails the plan comparison. A change to the hash inputs fails the
byte comparison.

The Python-side tests (`cargo test` is the Rust-side mirror) are also required:
the parity test proves the two *agree*, the unit tests prove each is internally
correct. A table that is wrong on both sides identically would pass parity and
fail the unit tests that encode the intended behaviour.

## 5. Versioning the contract

There is no numeric protocol version yet, because the two sides are built
together and shipped together in this repository. When the Rust core becomes a
separate service, the JSON document gains a `version` field and the reader
refuses an unknown one. Until then, a shape change on one side is a shape change
on the other, enforced by the shared test - which is a stronger guarantee than a
version number that nobody checks.

## 6. Switching a component to authoritative

The order, for any future component:

1. widen the parity gate to the component's *full* behaviour (for the claim
   path, that means the SQL transaction under concurrency, not just the
   decision);
2. make the Rust version authoritative behind a switch that defaults to Python;
3. run both in the deployment, comparing real answers, for a period;
4. remove the Python version and the switch in one change;
5. keep the parity test as a regression guard for the Rust behaviour against the
   recorded Python answers.

Step 4 is what satisfies "one authoritative implementation". A migration that
stops before it has two implementations, which the requirement forbids.
