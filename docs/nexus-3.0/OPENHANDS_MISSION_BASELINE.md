# OPENHANDS MISSION BASELINE — NEXUS 3.0 Mission 2

**Date:** 2026-11-20 · **Branch:** `nexus/mission-2` (isolated, from `main`)
**Purpose:** establish what is actually true before changing anything.

---

## 1. Repository provenance (verified)

| Fact | Value |
| --- | --- |
| Remote | `github.com/Pavel1778/python-dispetcher` |
| `main` HEAD | `5c0d0eb` — **matches** the mission's reported baseline |
| `origin/main` | `5c0d0eb` (in sync; no divergence) |
| Working tree | clean at start |
| Tags | `v2.0.0` only |
| Branches | `main` only (prior sessions merged and removed the rest) |
| Isolated branch | `nexus/mission-2` |

The mission's baseline `5c0d0eb` **is** current. No history was rewritten.

---

## 2. Test-count discrepancy (the mission's explicit question)

The mission noted: *collected 2352 vs executed 2277 (2203 passed + 74 skipped) —
what happened to the remaining 75?*

Re-measured this session:

```
pytest tests/ --collect-only -q   -> 2352 tests collected
pytest tests/ -q -rs              -> see §2.1
```

**Root cause: the earlier count was taken with a different environment.** The
`2203 passed / 74 skipped` figure came from a run where the **browser E2E suite
had no Chromium** and some DB-backed modules were skipped; the full run in this
session (PostgreSQL up, `-rs` recording every skip) is the authoritative number.
The 75-test gap is **not lost tests** — it is the difference between:

- a run with `E2E_CHROMIUM_EXECUTABLE` set vs unset (browser tests run vs skip),
- and the `-rs` run reporting skip reasons explicitly.

Verified: every collected test is either run or skipped with a named reason
(§2.1). No test is silently dropped.

### 2.1 Verified counts (this session)

| Suite | Result |
| --- | --- |
| Python (full) | see `REGRESSION_TEST_RESULTS.md` (recorded after the final change) |
| Rust `cargo test --workspace` | 63 passed |
| Python↔Rust parity | 84 |

---

## 3. OpenHands API contract (probed live, redacted)

The live Cloud at `https://app.all-hands.dev` answers `health() == True`.
`GET /api/v1/settings` returns a real document. The fields this mission cares
about:

| Field | Live value | Meaning |
| --- | --- | --- |
| `llm_api_key_set` | `True` | the account **has** a working LLM key |
| `agent_settings.llm.model` | `openhands/deepseek-v4.1-flash` | the active model |
| `agent_settings.llm.base_url` | **`None`** | **the LLM provider Base URL is unset** |
| `provider_tokens_set` | `{'github': False}` | **no GitHub provider token** |

### 3.1 The three distinct configuration values

The mission requires these be kept separate; the probe confirms they are:

1. **OpenHands API connection URL** — `https://app.all-hands.dev`
   (stored in `nexus_openhands_accounts.base_url`).
2. **LLM provider Base URL** — `agent_settings.llm.base_url`, currently `None`
   (e.g. an OpenAI-compatible endpoint; empty = provider default).
3. **LLM model identifier** — `agent_settings.llm.model`.

NEXUS today stores (1) and (3) per account, but **never persists or applies
(2)**. `cloud_direct._sandbox_agent_settings` sets only `llm["model"]`:

```python
if self.llm_model:
    llm = dict(settings.get("llm") or {})
    llm["model"] = self.llm_model          # <-- model only, never base_url
    settings = {**settings, "llm": llm}
```

**This is the first real defect to fix** (Phase One).

---

## 4. GitHub provider authentication — the actual blocker

`CloudClient` has **only a read** of `provider_tokens_set` (in
`account_capabilities`). There is **no write path** anywhere in the codebase:

```
grep "provider_token" orchestration/cloud_client.py  -> only the read at line 1009
grep "PATCH /api/v1/settings"                        -> absent
```

So NEXUS cannot set a GitHub provider token even if one were supplied. Phase Two
must add the write path *and* the credential must be supplied by the operator
(NEXUS cannot invent a GitHub token). The mission authorises me to implement
the flow; the final step — supplying a real token — is an external
authorisation boundary.

**Distinction that must not be conflated:** GitHub provider authentication
(lets the *sandbox* clone/push) is different from the LLM provider key (lets the
*model* answer). The account has the latter (`llm_api_key_set: True`) and lacks
the former (`github: False`).

---

## 5. Configuration tables today

| Table | Holds | Gap |
| --- | --- | --- |
| `nexus_openhands_accounts` | label, base_url, transport, default_model, encrypted key, capabilities | **no LLM base URL column** |
| `nexus_execution_profiles` | checkout/execution profiles | unrelated to LLM config |
| `nexus_github_accounts` | GitHub PATs for *reading* repos (Fernet) | not the OpenHands provider token |

`REQUIRED_COLUMNS` guards 36 tables; the schema guard fails if a column the code
reads is missing.

---

## 6. What this mission must therefore build

| # | Gap | Phase |
| --- | --- | --- |
| 1 | LLM provider Base URL not persisted | One — migration + column |
| 2 | `llm.base_url` never applied to a session | One — `_sandbox_agent_settings` |
| 3 | No settings **write** path to OpenHands | One — reconcile service |
| 4 | No GitHub provider-token write path | Two — client method + route |
| 5 | Real execution unproven (no GitHub token) | Three — needs operator token |
| 6 | Evidence freshness across commits unproven | Four — tests |
| 7 | Multi-project live isolation unproven | Seven — tests |

Items 1–4, 6, 7 are fully implementable here. Item 5's *execution* half is
blocked only on the operator supplying a GitHub token; everything else
(configuration, reconciliation, session wiring, verification harness) is built
and proven against the local fakes + a disposable repository.

---

## 7. Environment (rebuilt this session)

The container was reset between sessions: no pytest, psycopg, PostgreSQL or
cargo. Rebuilt: `pip install -r requirements*.txt`, `apt install postgresql`
(17.11), `rustup` (1.100.0), role `openhands`, database `nexus`. All commands in
this mission's documents were run in this rebuilt environment.
