# SELFQA AND EVIDENCE ACCEPTANCE — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Verdict: the evidence engine is real crypto and the Self-QA gate only ever
weakens a verdict; both are proven by tests that fail if a defence is removed.**

---

## 1. Evidence is earned, not asserted

`orchestration/evidence_trust.py` + `orchestration/nexus_evidence.py`. The rules
that a future change must keep (each has a test):

- **Only a trusted Ed25519 signature yields `VERIFIED`.** HMAC can never produce
  it; an unsigned/legacy/unknown-key record is `UNVERIFIED`; a tampered signed
  field is `FAILED`; no crypto library is `BLOCKED`.
  (`tests/test_evidence_trust.py`)
- **The exact signed bytes are stored** (`signed_payload`) and verification reads
  them verbatim, so editing the `project_id` column afterwards fails verification.
- **A task verdict inside a signed record is honest.** A `completed_unverified`
  task in a `VERIFIED` (properly signed) record is not a contradiction: the
  signature is about the record's integrity, the verdict is about the work.

## 2. The autonomous loop writes the record

`LabScheduler(evidence=…)` records every settled task result through the engine
(`tests/test_lab_evidence.py`):

- a verified/unverified/failed poll verdict and the `blocked` dispatch verdicts
  are signed with the **observed** commit (`remote_sha`), so `reverify` is
  meaningful;
- `LabEvidence` records the run's terminal decision: `ACCEPTED` only when every
  task is `completed_verified`; `NOT_READY` for a cancelled run; `REJECTED`
  otherwise;
- **a broken signer never stalls a run** — the record is best-effort, the task's
  own verdict is written first;
- **no signer, no record** — never a fabricated one.

## 3. Self-QA can only weaken a verdict

`orchestration/nexus_selfqa.py` + `ProjectQualityGate`. A failing required check
downgrades `completed_verified` → `completed_unverified` (so the task is
reworked); `passed` leaves it; `skipped`/`blocked` never turns a pass into a
failure; a gate that raises is logged and ignored. It runs at the **exact commit**
the Git verifier accepted.

The gate is per project (`nexus_projects.quality_gate`: `auto`/`on`/`off`), and a
clone is done with the project's own identity (a token in
`GIT_CONFIG_KEY_0`, never in argv or the URL). Proven by
`tests/test_quality_gate.py` and `tests/test_quality_gate_policy.py`.

## 4. What the engine refuses to call proof

- **A generated report is not proof of execution.** The report says "done" only
  of what NEXUS saw (`nexus_reports.build_report`): `no_changes` when no branch
  moved, `unconfirmed` when the starting commit is unknown.
- **A test name is not proof a test ran.** The Self-QA gate reads the *exit code*
  of the check at the accepted commit; a name alone is not evidence.
- **A text answer naming no commit is `completed_unverified`** and blocks a run
  (`tests/test_pipeline_e2e.py::test_a_text_answer_is_never_proof_of_work`).

## 5. Measured (this session)

```
pytest tests/test_evidence_trust.py tests/test_lab_evidence.py \
       tests/test_quality_gate.py tests/test_quality_gate_policy.py \
       tests/test_selfqa.py -q
-> all passed (part of the 130 that ran with security)
```

## 6. The live run's evidence

On the live service (this session) a real autonomous run reached
`completed_unverified` because no repository was bound to push to. That is the
engine behaving correctly: the work happened, but there is no observed commit to
prove it, so it is not `completed_verified`. The signed record exists and
re-verifies; the *verdict* is honest.

## 7. Remaining limit

A `completed_verified` live result needs a repository bound to a GitHub account
with a token the sandbox can push with. The live OpenHands account has no GitHub
provider token, so this is BLOCKED for a live push-verified task, not for the
engine.
