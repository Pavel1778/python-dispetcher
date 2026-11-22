# OPENHANDS CONFIGURATION PERSISTENCE — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2`
**Verdict: VERIFIED.** Model, provider endpoint and the OpenHands API URL are
persisted in PostgreSQL, survive restarts, and reach every new session. One
value was newly added this session (the LLM provider Base URL); the rest already
held.

---

## 1. The three distinct values (kept apart, never conflated)

| Value | Column | Example | What it is |
| --- | --- | --- | --- |
| OpenHands API URL | `nexus_openhands_accounts.base_url` | `https://app.all-hands.dev` | where NEXUS talks to OpenHands |
| LLM provider Base URL | `nexus_openhands_accounts.llm_base_url` | `https://api.openai.com/v1` or `NULL` | where the *sandbox's* model reaches |
| LLM model | `nexus_openhands_accounts.default_model` | `openhands/deepseek-v4.1-flash` | the model identifier |

A fourth, `llm_provider` (a short label like `openai`), is informational.
The API key lives Fernet-encrypted in `api_key_encrypted`, separate from all
four, so **rotating the key never erases the model or the URL**.

## 2. What changed this session

- **Migration `0045_llm_provider_config.sql`** adds `llm_base_url`,
  `llm_provider`, `config_synced_at`, `config_sync_state`, `config_sync_error`,
  with a `CHECK` that `llm_base_url` is `http(s)`.
- `add()`/`edit()` accept and validate the provider fields; an omitted value
  keeps the stored one (`COALESCE`), an explicit `null` in `edit` clears it.
- `CloudDirectClient._sandbox_agent_settings` now applies **both**
  `llm.model` and `llm.base_url` to the sandbox's `agent_settings`, so a session
  runs on the configured provider endpoint — previously only the model was set.
- `execution_client_for_account` forwards the stored provider settings to every
  client it builds (the single builder the worker, pool and diagnostics share).
- `_VIEW_COLUMNS` and `REQUIRED_COLUMNS` name the new fields (the schema guard
  fails if any is missing).

## 3. Precedence (deterministic, no hidden fallback)

1. **Explicit session override** — `OPENHANDS_LLM_BASE_URL` /
   `OPENHANDS_DEFAULT_MODEL` in the environment, or `build_direct_client(llm_…)`.
2. **Project override** — the account a project is bound to (its stored
   `default_model` / `llm_base_url`).
3. **Global default** — used only when no account is bound.

A stored value is never silently replaced by a different provider: if the model
or endpoint is unavailable the diagnostics report it (`FAILED`/`BLOCKED`), the
pool does not switch to an unrelated model.

## 4. Persistence proven (this session)

| Scenario | Result | Evidence |
| --- | --- | --- |
| Save model + provider URL | stored | `test_openhands_config_persistence` |
| Re-add without the model | model kept (was wiped before the earlier fix) | `test_readding_a_connection_without_a_model_keeps_the_saved_model` |
| Backend restart (fresh store) | values read back | `test_the_model_and_base_url_survive_a_backend_restart` |
| New session receives them | `agent_settings.llm.model` **and** `.base_url` | `test_the_llm_provider_base_url_reaches_the_sandbox_agent_settings` (mutation-proven) |
| Unset base URL | provider default kept, model still applied | `test_an_unset_llm_base_url_leaves_the_provider_default` |

Run: `pytest tests/test_openhands_config_persistence.py tests/test_cloud_direct.py
tests/test_config_reconcile.py -q` → all pass.

## 5. Automatic synchronization

`orchestration/openhands_config_sync.py` (`ConfigReconciler`) compares NEXUS's
stored model/endpoint with what OpenHands reports (`GET /api/v1/settings` →
`agent_settings.llm`) and records `synced` / `drift` / `pending` / `blocked` on
the account. It never raises on a comparison failure and never rewrites a value
it cannot write back.

**Honest limitation:** the Cloud V1 settings surface is read-only for
`llm.model`/`llm.base_url` (`PATCH`/`PUT` → `405`, verified live). So NEXUS
*applies* the configuration at **session creation** (the supported mechanism),
not by mutating the account's server-side default. The reconciler therefore
reports drift; it does not pretend to fix a default it cannot write.

Surface: `POST /api/openhands/config/sync` (protected) reconciles one account or
all of the caller's. `App.config_sync` is wired at startup.

## 6. Acceptance

> **Normal restarts, reconnections and redeployments must not require manual
> reconfiguration.**

Met: the configuration is in PostgreSQL; a restart reads it back; a new session
receives it; an omitted field is never cleared; rotating the key leaves the
model and URL intact.
