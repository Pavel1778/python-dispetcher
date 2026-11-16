# NEXUS — Rust Migration Risk Register

> **STATUS: SPECIFICATION.** The risk register for the Python → Rust migration.
> Each risk: probability, impact, severity, detection, mitigation, acceptance
> criteria, owner domain.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [MIGRATION_ROADMAP](./MIGRATION_ROADMAP.md),
[API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md).

Severity = f(probability, impact): **Critical / High / Medium / Low**.

---

| ID | Risk | Prob | Impact | Severity | Detection | Mitigation | Acceptance criteria | Owner domain |
|---|---|---|---|---|---|---|---|---|
| R1 | Data migration corrupts or loses data | Low | High | High | row counts + checksums; diff before/after | expand/contract; snapshot before cutover; never destructive | a dump/restore drill passes; counts match | Audit & Lifecycle |
| R2 | Dual writers on one entity | Medium | High | **Critical** | cutover ledger; divergence monitor | one-writer-per-domain rule; proxy routing | ledger shows one owner per domain at all times | Foundation |
| R3 | API incompatibility breaks clients | Medium | High | High | contract tests; canary error rate | reproduce paths/shapes; version new endpoints | contract suite green; canary error ≤ baseline | API |
| R4 | Execution duplication (a task runs twice) | Medium | High | High | duplicate run/conversation detector | idempotency per text; lease+fencing; change-required verification | scenario 12 passes; no duplicate accepted run | Execution Engine |
| R5 | OpenHands recovery gap (lost contact) | Medium | High | High | reconciliation service; stale-run detector | poll Cloud; never assume dead; branch hold until stop observed | scenario 7 passes | OpenHands Runtime |
| R6 | Provider outage (LLM/OpenHands/GitHub) | High | Medium | High | failure classification | cascade failover; bounded retry; recorded failure | scenario 8/11 pass | AI Gateway |
| R7 | Project isolation breach | Low | High | High | 404 tests; cross-project read probe | membership check on every project-scoped call | scenario 13 passes; no 200 across projects | Identity & Access |
| R8 | Permissions / RBAC error | Low | High | High | security tests | single-owner model documented; membership enforced | security suite green | Identity & Access |
| R9 | Secret handling leak | Low | High | High | secret scan; `Debug` audit | secrets via handles; Fernet at rest; never logged | secret scan clean; no secret in logs | Foundation/Config |
| R10 | File security (unsafe archive/path) | Low | High | High | archive-safety tests | member name never a path; size cap | safety suite green | File Laboratory |
| R11 | Mobile limitations (Tauri 2 on Android) | Medium | Medium | Medium | capability spike | decide after the spike; fallback to Kotlin/WebView | spike proves or rejects Tauri 2 | Clients |
| R12 | Desktop packaging failures | Medium | Medium | Medium | build in CI | pin toolchain; signed artefacts | `.deb`/AppImage/`.exe`/`.msi` build | Clients |
| R13 | Resource constraints (Render Free) | High | Medium | High | metrics | paid plan / VPS before autonomy | staging runs unattended | Infrastructure |
| R14 | Cost overrun | Medium | Medium | Medium | spend monitor | token caps; budget tiers | per-run cost cap enforced | Self-QA/Infra |
| R15 | Python retirement too early | Medium | High | High | divergence monitor; stability window | retire per domain only after the window | zero divergence for the window | Audit & Lifecycle |
| R16 | Rollback not real (new data unreadable by Python) | Medium | High | **Critical** | rollback drill | expand/contract; rollback drill per domain | a rollback drill passes for every domain | Foundation |
| R17 | Scheduler double-claim at cutover | Medium | High | High | lease/ownership checks | drain outgoing scheduler; lease hand-off | scenario 5/6 pass at cutover | Scheduler |
| R18 | Event ordering / duplicate delivery | Medium | Medium | Medium | consumer idempotency tests | per-entity order; idempotent consumers | duplicate event test passes | Events |

---

## Top risks (Critical/High summary)

- **R2 (dual writers)** and **R16 (rollback not real)** are the two critical
  risks: both are structural, both are mitigated by the one-writer rule and
  expand/contract, and both have a drill as their acceptance test.
- **R1, R3, R4, R5, R7, R8, R9, R10, R13, R15, R17** are high-severity and each
  has a concrete scenario or suite as its acceptance test.

## Owner-domain map

| Domain | Risks owned |
|---|---|
| Foundation | R2, R9, R16 |
| Identity & Access | R7, R8 |
| API | R3 |
| Execution Engine | R4 |
| OpenHands Runtime | R5 |
| AI Gateway | R6 |
| File Laboratory | R10 |
| Scheduler | R17 |
| Events & Notifications | R18 |
| Audit & Lifecycle | R1, R15 |
| Clients | R11, R12 |
| Infrastructure | R13, R14 |
