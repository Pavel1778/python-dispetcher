# Real integration testing

The test suite runs against **real** infrastructure, not mocks of the clients:
a real PostgreSQL, and real local HTTP servers that speak the exact surfaces of
OpenHands Cloud (`tests/fake_cloud.py`), the sandbox agent-server
(`tests/fake_runtime.py`), GitHub (`tests/fake_github.py`) and an
OpenAI-compatible LLM (`tests/test_providers.py`). A client under test makes
real requests over a real socket.

This document describes how to go one step further: exercise the **live**
services, with the operator's own credentials. Never commit a credential; never
print one in a report or a log.

## 1. PostgreSQL (always required for the DB-backed modules)

```bash
sudo -u postgres createdb nexus_test            # or any disposable database
export TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus_test"
python -m pytest tests/ -q -p no:cacheprovider
```

Without `TEST_SUPABASE_DB_URL` the DB-backed modules skip cleanly; that run is
**not** a full verification. The backup/restore drill
(`tests/test_backup_restore.py`) needs the PostgreSQL client tools (`pg_dump`,
`pg_restore`) that match the server major version.

## 2. OpenHands Cloud (live)

The live probe creates and deletes exactly one conversation.

```bash
export OPENHANDS_API_KEY=...                    # the account's key
export OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash   # a model the sandbox can reach
python -m orchestration.cli openhands diagnostics --live
```

What to expect when it works: `conversation_create`, `message_and_events`,
`run_command`, `workspace_rw` and (with a repository bound) `task_completion`
VERIFIED. A **429 is BLOCKED** (quota), not FAILED. An account with **no GitHub
provider token** cannot clone, so a task that must push ends
`completed_unverified` — that is correct, not a defect.

Trap: a bare sandbox defaults to a model whose key may be absent; pass
`OPENHANDS_DEFAULT_MODEL` or the conversation ends with an LLM auth error.

## 3. GitHub (live)

```bash
export GITHUB_TOKEN=...                          # a token with repo read scope
python -m orchestration.cli github list          # discovery (read-only)
```

A real PAT can be added as a stored account (`POST /api/github/accounts`); the
account is verified through `GET /user` before it is trusted, and the token is
Fernet-encrypted and never returned. Writes (create repository, change
visibility) are guarded: a public change requires `acknowledge_public=True`,
which only the confirmed-action handler sets.

## 4. LLM providers (live)

```bash
export GEMINI_API_KEY=...        # or GROQ_API_KEY / OPENROUTER_API_KEY
python -m orchestration.cli providers --live
```

A provider is never reported connected without a real read-only probe. Exit
code 6 means a key was refused or a configured model is missing.

## 5. The full user journey

`tests/test_pipeline_e2e.py` drives `USER PROMPT → PLANNER → DAG → SCHEDULER →
OPENHANDS → EXECUTION → QA → EVIDENCE → ACCEPTANCE → NOTIFICATION` over real
HTTP and PostgreSQL against the stand-in servers. To run the same shape against
the live services, use the studio or the CLI with the credentials above; the
server-side code path is identical.

## 6. Browser E2E

```bash
python -m playwright install chromium           # or use a system Chromium
E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
  python -m pytest tests/test_browser_e2e.py -q
```

The journeys take `E2E_CHROMIUM_EXECUTABLE` / `E2E_CHROMIUM_LD_LIBRARY_PATH`
for a Chromium that is not Playwright's own; outside CI the module skips when no
browser starts.

## 7. Reporting honestly

- A check that could not run is **BLOCKED** with the reason (a missing key, a
  quota), never silently passed and never simulated.
- A configured tool is not an available tool, and availability is not exercise:
  only an observed `ActionEvent` proves a tool ran.
- "Deployed" is not "seen working": say what was exercised live and what was
  only exercised against the stand-ins.
