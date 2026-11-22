# OPENHANDS GITHUB AUTHENTICATION — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED.** GitHub authentication works, and the previous audit's
"GitHub provider token is the blocker" conclusion was **wrong about the
mechanism**. The supported path is the sandbox's `secrets` at session creation,
not an account-level provider token.

---

## 1. What the previous audit said, and what is actually true

The audit (`FULL_PROJECT_AUDIT_2026-11-20.md`) concluded a live push-verified
task was BLOCKED because `provider_tokens_set: {'github': False}`. That read is
correct — the *account* has no provider token — but it is **not the mechanism
NEXUS uses** to authenticate a sandbox's git.

### 1.1 The Cloud V1 settings surface is read-only for provider tokens

Probed live against `https://app.all-hands.dev`:

```
PATCH /api/v1/settings  -> 405 Method Not Allowed
PUT   /api/v1/settings  -> 405 Method Not Allowed
GET   /api/v1/settings  -> 200, provider_tokens_set: {'github': False}
```

So NEXUS cannot set an account-level GitHub token through Cloud V1, and it never
tried to. That is not a gap in NEXUS; it is a surface the Cloud does not offer.

### 1.2 The mechanism NEXUS actually uses: per-session `secrets`

The agent-server's `StartConversationRequest` carries a `secrets` mapping of
`name -> {kind: "StaticSecret", value: ...}`. The server injects the value into
the agent's environment and **masks it in the agent's output**. NEXUS already
uses this (`orchestration/nexus_lab.py::git_credential_for`,
`orchestration/cloud_direct.py::git_secret`):

```
git_credential_for(task)  ->  {"GITHUB_TOKEN": {"kind": "StaticSecret", "value": <token>}}
                          ->  start_conversation(secrets=...)
                          ->  body["secrets"] on POST /api/conversations
```

The token is resolved from the **project's bound GitHub account** (the same
identity the repository is read with), never fabricated, never another
account's, and never written to a log or the transcript.

---

## 2. Proof the credential is real and works

### 2.1 The token authenticates a real git operation

```
$ git -c http.extraHeader="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 -w0)" \
      ls-remote https://github.com/Pavel1778/python-dispetcher.git HEAD
5c0d0ebbb58ede5a6b137ab675cafb3da5d730d1        HEAD
```

A real HTTPS git operation against the live repository succeeded with the same
credential a sandbox receives. `GitHubClient.current_user()` also returns
`Pavel1778` (token valid, 21 repositories visible).

### 2.2 The credential reaches the create body as a masked StaticSecret

`tests/test_cloud_direct.py::test_a_git_credential_reaches_the_create_body_as_a_static_secret`
asserts the body the agent-server actually received carries
`secrets.GITHUB_TOKEN.kind == "StaticSecret"`. It passes.

### 2.3 The credential enabled a real push (Phase Three)

In the live development task (see `OPENHANDS_LIVE_EXECUTION.md`), OpenHands
cloned `Pavel1778/nexus-lab-smoke`, committed, and **pushed to `lab-fix`** — a
push that is refused without a working credential. Verified independently: the
branch and commit `9afb666` exist on GitHub.

---

## 3. Authorized vs unauthorized repository

| Case | Behaviour | Evidence |
| --- | --- | --- |
| Authorized repo (project bound to a GitHub account with the repo) | token delivered, clone/push succeeds | the live task above |
| No repository on the task | `git_credential_for` returns `None`; the task runs with no credential | `tests/test_git_credential.py` |
| Unbound / revoked account | `None` (logged, never a different identity's token) | `tests/test_git_credential.py` |
| A transport without `secrets` (the `app` transport) | left unchanged — no secret is sent to a surface that cannot mask it | `supports_git_secrets` gate in `nexus_lab.py` |

A task with no credential is not a dispatch failure: it runs, and the Git
verifier honestly records `completed_unverified`.

---

## 4. Security posture

- The token is **never** in a log, an API response, the transcript, or an
  exception message. It is passed only into `secrets`, which the agent-server
  masks.
- `GitHubAccountService` stores the PAT Fernet-encrypted (`NEXUS_SECRET_KEY`) and
  never returns it; `resolve_token` is the only reader.
- A missing/revoked account yields no token, never another account's.

---

## 5. Conclusion

**GitHub authentication is not a blocker.** It was already implemented through
the correct mechanism; the previous audit mistook "the account has no
account-level provider token" for "a sandbox cannot push", and the live push
above disproves that. No new credential write path is needed, and none was
added — adding one would target a surface the Cloud returns `405` for.
