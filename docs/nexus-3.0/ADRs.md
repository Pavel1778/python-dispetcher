# NEXUS 3.0 — Architecture Decision Records (ADRs)

Short, dated, and honest. Each records a decision, why, and what it costs. They
exist so a future change does not silently reverse a choice made for a reason.

---

## ADR-001 — Incremental, parity-gated migration (not a big-bang rewrite)

**Decision.** Move components to Rust one at a time, each behind a parity test
that drives the Python and Rust implementations over the same cases on real
PostgreSQL. The Python version stays authoritative until the Rust one is
switched deliberately; the switch removes the Python version in the same change.

**Why.** The requirement forbids a blind one-shot rewrite and requires one
authoritative implementation per component. A pure function proven by parity is
safe to make authoritative; an unproven rewrite is not.

**Cost.** Both implementations exist during the transition; the parity test is
load-bearing and must not be weakened.

---

## ADR-002 — The claim transaction is the first I/O component to move

**Decision.** `nexus-store` (SQLx) implements `LabStore.claim_task` exactly:
run gate on the locked row, global cap under an advisory lock, the three SQL
predicates, `FOR UPDATE SKIP LOCKED`, the per-branch advisory lock and re-check,
the fenced take.

**Why.** It is the worker's hot path and it has the hardest invariants
(atomicity, fencing, branch exclusion). Proving the Rust store on the hardest
path is a stronger signal than moving an easy one.

**Cost.** The claim is DB-bound, so latency is the same as Python; the win is
memory and a tighter tail, not speed (see ADR-003).

---

## ADR-003 — Report memory, not a speedup, as the claim's win

**Decision.** The benchmark reports Python 43.1 MB vs Rust 4.7 MB peak RSS (~9×)
and latency parity (p50 1.22 vs 1.39 ms), and claims **no** end-to-end speedup.

**Why.** The slow paths are network-bound (provider, OpenHands, GitHub) and
database-bound; a faster language around a round trip does not move them.
Claiming a speedup that was not measured is forbidden.

**Cost.** The case for Rust is the operating budget on the 8 GB target and the
tail, not a headline number — which is the honest case.

---

## ADR-004 — Two processes: API and worker

**Decision.** One API process (HTTP, auth, studio, MCP HTTP) and one worker
process (claim, DAG, dispatch, verify, selfQA, evidence, reports), sharing
crates and one PostgreSQL.

**Why.** They have different scaling and failure profiles: a slow OpenHands poll
must not stall a request. No further processes are added without a measured
reason.

**Cost.** Two things to deploy and supervise (systemd units already exist).

---

## ADR-005 — PostgreSQL is the only queue; LISTEN/NOTIFY is only a hint

**Decision.** The durable queue is PostgreSQL (`FOR UPDATE SKIP LOCKED`, leases,
fencing). `LISTEN/NOTIFY` may wake a worker but is never the source of truth; the
worker polls regardless, so a lost notification is not a lost task.

**Why.** A notification is not durable; a table row is. The requirement states
this explicitly.

**Cost.** A polling floor (already the existing behaviour) plus a wake-up path.

---

## ADR-006 — The studio's appearance is frozen; only its speed may change

**Decision.** The front end keeps its visual identity (layout, navigation,
patterns). Allowed changes are JavaScript/DOM/render optimisations, lazy loading,
list virtualisation, fewer requests, incremental updates. A screenshot baseline
(`docs/nexus-3.0/baseline/`) is the reference; a change is compared against it.

**Why.** The requirement makes visual identity a hard constraint. "Faster" is
allowed; "different" is not.

**Cost.** Every UI change is checked against the baseline; a deliberate visual
change needs its own justification.

---

## ADR-007 — Do not migrate the intent router or task-kind derivation

**Decision.** `nexus_intent.py` and `nexus_planner.task_kind` stay in Python.

**Why.** They are large, language-sensitive regexes with a history of subtle
production bugs; a Rust rewrite is a re-implementation with divergence risk and
no behavioural gain. The Rust validator carries whatever `kind` it is given.

**Cost.** A small amount of logic stays in Python even in a "Rust-first" system;
that is a deliberate exception, documented in `docs/RUST_MIGRATION.md` §4.

---

## ADR-008 — Native apps are Tauri 2 around the unchanged studio

**Decision.** APK/DEB/EXE are Tauri 2 applications that host the same front end
and the same API; the native bridge provides only OS functions (secure storage,
file dialogs, notifications, deep links, lifecycle, safe file access). Secrets
are never in open front-end storage.

**Why.** One front end, one API, three native shells. The current DEB/APK/EXE are
WebView/launcher shells, not Tauri — recorded as a Phase 5 gap, not reported as
done.

**Cost.** A real toolchain per platform (Android SDK/NDK, mingw-w64, Linux) is
required to build and verify; where absent, the platform is NOT VERIFIED.
