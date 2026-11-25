# OPENHANDS LIVE EXECUTION — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED (with one condition).** A real OpenHands agent performed a
real development task on a real GitHub repository: it cloned, changed source,
added a test, ran pytest, committed and pushed. The result was verified
**independently** — not from the agent's text.

**Condition:** the target was the purpose-built disposable repository
`Pavel1778/nexus-lab-smoke`, not a user production repository (the mission
forbids destructive experiments on real user repositories).

---

## 1. What ran

| Item | Value |
| --- | --- |
| Transport | `direct` (sandbox agent-server) |
| Model | `openhands/deepseek-v4.1-flash` (the stored account's model) |
| Repository | `Pavel1778/nexus-lab-smoke` (disposable smoke repo) |
| Initial `main` SHA | `7ab0c0b647dfa48d69769e2e7f310e1b2bf4947f` |
| Branch the agent pushed | `lab-fix` |
| Commit | `9afb666bafa63c949ea2fb6db76b37851bbefa39` |
| Author | `openhands` |
| Message | `guard div against division by zero` |

The task given to the agent (verbatim intent): clone with the `GITHUB_TOKEN`
secret, create branch `lab-fix`, make `div()` raise
`ValueError("division by zero is not allowed")` on a zero divisor, add a
`test_div_by_zero`, run `pytest -q`, commit and push, report the SHA and the
pytest summary.

## 2. A pre-flight check first (a trivial conversation)

Before the real task, a minimal conversation was run to confirm the configured
model actually executes:

```
conv 9eb873fa… -> execution_status "finished", final_response "READY"
```

That isolates "the model answers" from "the agent can do work".

## 3. The agent's own report (a claim — not yet trusted)

> Commit SHA pushed: `9afb666…` (branch `lab-fix`)
> pytest summary: `5 passed in 0.01s`
> Changes: `calc.py` div() raises ValueError on zero; `test_calc.py` adds
> `test_div_by_zero`; removed the stale NOTE comment.
> It also flagged that `git add -A` swept in `__pycache__`, so it amended the
> commit and pushed `--force-with-lease` on its own fresh branch.

An agent that reports a deviation unprompted is a good sign, but a report is
still a claim. Section 4 is the part that matters.

## 4. Independent verification (the agent's text is NOT evidence)

Every claim below was checked against GitHub and by running the code myself.

### 4.1 The branch and commit exist, at the exact SHA

```
lab-fix head : 9afb666bafa63c949ea2fb6db76b37851bbefa39
claimed      : 9afb666bafa63c949ea2fb6db76b37851bbefa39
MATCH: True
```

### 4.2 The diff is exactly the claimed change (compare by SHA)

```
status ahead, ahead_by 1, total_commits 1
commit 9afb666  "guard div against division by zero"  author openhands
files:
  calc.py        modified  +2 -1
  test_calc.py   modified  +3 -3
files_listed: 2
```

Two files, one commit — no unrelated changes, no `__pycache__`.

### 4.3 The pushed content is the intended fix

`calc.py` on `lab-fix`:

```python
def div(a, b):
    if b == 0:
        raise ValueError("division by zero is not allowed")
    return a / b
```

`test_calc.py` on `lab-fix` gains:

```python
def test_div_by_zero():
    with pytest.raises(ValueError):
        div(1, 0)
```

### 4.4 I ran the tests myself, on a fresh clone

```
$ git clone --branch lab-fix …/nexus-lab-smoke
$ git log --oneline -1
9afb666 (HEAD -> lab-fix, origin/lab-fix) guard div against division by zero
$ python -m pytest -q
5 passed in 0.01s
```

Matches the agent's "5 passed" — verified, not trusted.

### 4.5 The fix was actually needed (negative control)

On `main` (before the change):

```
$ python3 -c "from calc import div; div(1,0)"
ZeroDivisionError: division by zero
$ python -m pytest -q
4 passed
```

So the branch genuinely changes behaviour (`ZeroDivisionError` → `ValueError`)
and adds a test (4 → 5). This is a real change, not a no-op that happens to
pass.

## 5. What this proves, and what it does not

**Proves:** OpenHands received a NEXUS task, started a real agent session on the
configured model, authenticated to GitHub with the delivered credential, cloned
the correct repository, edited real source, added a real test, ran real
verification, committed and pushed — and NEXUS can independently confirm all of
it (branch, SHA, diff, file content, test exit status).

**Does not prove (stated honestly):**
- A task that pushes to a *user's* production repository (deliberately not
  attempted).
- The full NEXUS lab run loop (`LabScheduler` → `GitVerifier` →
  `completed_verified`) against this live repo end to end — that path is proven
  against the local fakes and real PostgreSQL in the existing suites, and the
  live pieces (conversation, push, verifier reading the branch head) are each
  verified above; the composition through the scheduler on a live repo is the
  next step in `AUTONOMOUS_EXECUTION_ACCEPTANCE.md`.

## 6. End-to-end `completed_verified` (the real GitVerifier, live)

The final gate is not "the agent pushed" — it is "NEXUS's own verifier accepts
it from the real repository". The real `GitVerifier` was run against the live
branch and the live report:

```
task : repository Pavel1778/nexus-lab-smoke, branch lab-fix,
       base_sha 7ab0c0b6…, kind implementation, expects_change True
report: "Commit SHA pushed: 9afb666…; pytest 5 passed"

VERDICT : completed_verified
REASON  : remote branch is at the reported commit
EVIDENCE: remote_sha 9afb666bafa63c949ea2fb6db76b37851bbefa39
          named_sha  9afb666bafa63c949ea2fb6db76b37851bbefa39
          base_sha   7ab0c0b647dfa48d69769e2e7f310e1b2bf4947f
          changed    True
```

That is the acceptance gate: the branch moved from the base, its head is a
commit the report named, and the head was read from GitHub now — not taken from
the agent's word.

## 7. The verifier cannot be fooled (live negative controls)

Same task, same real verifier, against the live branch:

| Agent report | Verdict | Reason |
| --- | --- | --- |
| "Done, all good!" (no commit named) | `completed_unverified` | no verifiable commit reported by the agent |
| "pushed abc1234def" (wrong commit) | `completed_unverified` | reported commit is not the branch head |
| conversation `error` | `failed` | conversation error |

A textual claim of success never becomes `completed_verified`. This is the
mission's "do not trust only the agent's textual claim" requirement, proven on
the live repository rather than asserted.
