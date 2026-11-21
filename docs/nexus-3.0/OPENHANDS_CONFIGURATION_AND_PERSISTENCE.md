# OPENHANDS CONFIGURATION AND PERSISTENCE — NEXUS 3.0

**Date:** 2026-11-20 · **Branch:** `nexus/3.0-mission`
**Status: the model and Base URL now persist across restart/reconnect, and a new
session receives them. Two root-cause defects were found and fixed, each with a
test that fails if undone.**

---

## 1. Root causes found (not symptoms)

### Defect 1 — re-adding a connection wiped its model

`OpenHandsAccountService.add` upserts on `(principal, label)`:

```sql
ON CONFLICT (principal, label) DO UPDATE SET
  default_model = EXCLUDED.default_model,   -- NULL when the caller omitted it
  ...
```

A **reconnect** (re-entering the same connection form, or an automated re-add)
that did not repeat `default_model` wrote `NULL` over the saved model. The next
session then fell back to the sandbox default, whose LLM key may be absent — the
exact "OpenHands forgot its model" symptom.

**Fix.** Only an explicit value changes a field; an omitted field keeps its
stored value (`COALESCE(%s, nexus_openhands_accounts.default_model)`), for
`default_model`, `transport`, and both limits. The HTTP route no longer defaults
`transport` to `"direct"` either, so a stored `app` connection is not flipped.

**Regression test:** `tests/test_openhands_config_persistence.py::
test_readding_a_connection_without_a_model_keeps_the_saved_model` (failed with
`None == 'openhands/deepseek-v4.1-flash'` before the fix).

### Defect 2 — the `code` workflow path ignored the stored connection

`SrRuntime` was constructed with the process-wide `build_cloud_client()`, so a
`code` turn's Sr/Jr conversations never used the project's stored base URL or
model. `GET /api/studio` said "configured", but the session did not get it.

**Fix.** `SrRuntime(client_for_workflow=…)` resolves the Cloud client per
workflow from the project's bound account (`App._workflow_client` →
`OpenHandsAccountService.execution_client_for_account`, the single builder). A
project with **no** bound account raises `NotFound`, and the runtime falls back
to the process client — never a silent switch to a *different* account.

**Regression test:** `tests/test_openhands_config_persistence.py::
test_a_code_workflow_runs_on_the_stored_connection`.

---

## 2. Configuration authority and precedence

One authority: **PostgreSQL**, table `nexus_openhands_accounts` (the connection)
and `nexus_projects.openhands_account_id` / `nexus_project_executors` (the
binding). Precedence, highest first:

1. **Session override** — `OPENHANDS_AGENT_PROFILE_ID` / explicit
   `SrRuntime(plugins=…)` (opt-in, never written by the UI).
2. **Project override** — the account the project is bound to
   (`bind_project`, `nexus_project_executors` order).
3. **Global default** — `OPENHANDS_DEFAULT_MODEL` / `OPENHANDS_TRANSPORT`, used
   only when no account is bound.

There is **no hidden default that replaces a configured model**: an account's
`default_model` is applied to every sandbox it starts
(`CloudDirectClient._sandbox_agent_settings` overrides the sandbox's `llm.model`).

---

## 3. What persists, and how it is proven

The configuration lives in PostgreSQL, so it survives browser refresh, logout,
application restart, backend restart, worker restart, container recreation and
redeployment — anything that keeps the database.

Proven in this session:

| Scenario | Evidence |
| --- | --- |
| Reconnect without a model | `test_readding_a_connection_without_a_model_keeps_the_saved_model` |
| Backend restart (fresh process, same DB) | `test_the_model_and_base_url_survive_a_backend_restart` (a second `PostgresStore`) |
| Key rotation keeps model + URL | `test_an_edit_that_rotates_the_key_keeps_the_model_and_url` |
| A `code` workflow gets the stored config | `test_a_code_workflow_runs_on_the_stored_connection` |
| A fresh process reads back model + URL + transport | live shell probe: `recovered model : openhands/deepseek-v4.1-flash`, `recovered base_url : https://app.all-hands.dev` |
| The model reaches a **real** session | live diagnostics: `conversation_create`, `message_and_events`, `run_command`, `task_completion`, `workspace_rw` all **VERIFIED** |

---

## 4. Live verification (real OpenHands, this session)

```
VERIFIED  accounts              1 account(s) probed
VERIFIED  account_pool          mode auto selects live-verify
VERIFIED  conversation_create   conversation created
VERIFIED  message_and_events    message delivered; events read; conversation finished
VERIFIED  run_command           the agent echoed the marker
VERIFIED  task_completion       conversation reached the terminal state 'finished'
VERIFIED  workspace_rw          file uploaded and read back with a matching SHA-256
```

17 VERIFIED / 0 FAILED / 0 BLOCKED with the stored account
(`direct` transport, `openhands/deepseek-v4.1-flash`). The live account has **no
GitHub provider token** (`provider_tokens_set: {'github': False}`), so a task
that must clone and push ends `completed_unverified` — correct, not a defect.

**Trap (unchanged):** without `OPENHANDS_DEFAULT_MODEL`, a bare sandbox ends
`error` (LLM auth). The stored account's `default_model` is what prevents this,
which is why Defect 1 mattered.

---

## 5. Verification / reconciliation service

`OpenHandsAccountService` reconciles on demand:

- `check_connection` — reachability only (`ping`: any HTTP answer means
  reachable, including 401/429).
- `verify` — authorization: `GET /api/v1/settings` proves the key is accepted.
- `edit` — the one way to change a connection; a new `base_url` re-verifies the
  stored key against it before committing.
- `reprobe_unavailable` — asks connections written off whether they are back.

`OpenHandsDiagnostics` separates **connectivity**, **authentication**, **model
configuration** and **actual execution** into distinct checks, so "the URL
answers" never reads as "the agent runs".

---

## 6. UI

The studio already shows base URL, transport, model, key hint (masked),
health, compatibility and last error, with Test Connection / Test Model /
Default / Errors / Edit controls. Empty fields are sent as `undefined`, so the
server keeps the stored value — consistent with Defect 1's fix. No API key is
ever rendered.

---

## 7. What remains BLOCKED / limited

- **Two-account failover** is BLOCKED: only one OpenHands key is available; the
  switch is proven against the local fakes + PostgreSQL only.
- **A live push-verified task** (`completed_verified`) needs a GitHub provider
  token on the OpenHands account, which the live account lacks. The task runs,
  the agent works, and NEXUS honestly records `completed_unverified`.
