# NEXUS — Final Acceptance Report

**Status: ISSUED.** This replaces the Phase-0 placeholder (which said, correctly
at the time, "NOT YET ISSUED"). It is the §15 deliverable of the request
"NEXUS — OpenHands Complete Integration, Configuration & Autonomous Execution
Acceptance": the twelve completion conditions, each with the evidence that
earns it, and the twelve requested items.

Author: OpenHands agent (Senior Infrastructure / OpenHands Integration /
Backend Architect / QA), NEXUS run
`a1e46e80-5c2f-401d-8da0-f38dd3d00d66`, task `t17` (Final Acceptance Testing).

Everything below was produced in this sandbox against the live OpenHands Cloud
and a local PostgreSQL 17. Where something could not be done here it says so as
**BLOCKED** or **UNVERIFIED** — never as a pass it did not observe.

---

## 1. Baseline and result

| Item | Value |
| --- | --- |
| Repository | `Pavel1778/python-dispetcher` (`origin`) |
| Branch | `main` |
| **Source HEAD (before this task)** | `6331f0aa0d084dba03bb70541f28eaea7dea00fd` (`6331f0a`) |
| Run baseline (restore point) | `78416031045c168d8ff9afeb747b9210df671556` (`7841603`), tag `preservation/pre-openhands-integration` |
| **Final HEAD (after this task)** | the commit that adds this report (named in the task report and the commit message) |
| Working tree before | clean |
| Lint | `python -m ruff check .` → **All checks passed!** |
| Full suite (final) | **1337 passed, 38 skipped, 0 failed** in 18:12, PostgreSQL 17 |
| Browser E2E (Chromium installed) | **29 passed** in 2:25 |
| Live OpenHands diagnostics | **17 VERIFIED / 0 FAILED / 0 BLOCKED / 1 UNVERIFIED** |

The source HEAD was clean and already green except for **one** load-sensitive
test (see §2.1). This task fixed that, ran the whole suite twice to confirm, ran
the live OpenHands diagnostics and a genuine live development task through
NEXUS, and issued this report.

---

## 2. What was changed

### 2.1 The one real defect found (acceptance condition 8)

`tests/test_acceptance_recovery.py::test_a_sigkilled_worker_is_recovered_by_a_new_process`
failed in the first full run (`1 failed, 1336 passed, 38 skipped`) and passed in
isolation. It is a race in the *test*, not in the code.

The test killed the first worker as soon as its conversation existed in the
Cloud. That is an instant **before** the worker has bound the conversation to
the task. A `SIGKILL` inside that window leaves a claimed task with no
conversation; the documented recovery is to take the task back when its 900 s
claim runs out and adopt the conversation (`tests/test_durable_runs.py`) — a
correct path that takes longer than this test's 20 s wait, so it timed out.

The repository had already fixed exactly this for the sibling test in
`tests/test_lab_worker_process.py` (commit `5b2efec`, "Worker restart test: kill
the worker once it has recorded the conversation, not in the instant before").
The acceptance copy was added later (commit `1499e07`) without that guard. The
fix applies the same guard: wait until the task **records** the conversation it
started, then kill.

- File changed: `tests/test_acceptance_recovery.py` (9 lines added).
- Verified: 5 isolated runs, and two full-suite runs — both green.

The code was **not** changed: the recovery behaviour it exercises is correct,
and this was a test-only race.

---

## 3. The twelve completion conditions (§15)

Each condition is stated, then the evidence. `VERIFIED` means it was observed.

### 1. OpenHands реально подключается — **VERIFIED**
Live probe against `https://app.all-hands.dev` with the environment key:
`cloud_api: VERIFIED (reachable)`, `conversation_create: VERIFIED`,
`message_and_events: VERIFIED`, `run_command: VERIFIED`,
`task_completion: VERIFIED (state 'finished')`, `workspace_rw: VERIFIED
(file uploaded and read back with a matching SHA-256)`. The probe created and
deleted exactly one conversation (`"deleted": true`).

### 2. Все добавленные аккаунты проверяются независимо — **VERIFIED**
`tests/test_acceptance_account_pool.py` (15 tests): one, two, three and twelve
accounts are supported and each is verified against **its own** endpoint with
its own identity. The live run stored one account and `accounts: VERIFIED
(1 account(s) probed)`; the account view reported `auth: ok`, `llm_key_set:
true`, `mcp_servers: 20`. A second live account is not available here — that
specific case is BLOCKED (§12), and the multi-account logic is proven against
real HTTP servers and real PostgreSQL.

### 3. Переключение между двумя аккаунтами работает — **VERIFIED (fakes + real PG)**
`tests/test_openhands_failover.py` (11 tests) and
`tests/test_acceptance_e2e_failover.py` (2 tests): a real task starts on #1, #1
fails, the task moves to #2, the context is handed over (a durable
`nexus_task_handoffs` row and the first message), and #1 is **stopped** so it
cannot write to the same checkout. A **two-live-account** switch is BLOCKED here
(one key only) and is not faked.

### 4. Контекст незавершённой задачи сохраняется — **VERIFIED**
`tests/test_acceptance_handoff.py` (6 tests): the hand-off carries project, task,
goal, plan, completed steps, remaining steps, repository, branch, last commit,
**decisions**, **constraints**, **test results**, **uncommitted work**, acceptance
criteria, previous report and last error; the message marks the completed step
"do NOT redo"; a real two-task run never restarts the verified task. The four
items in bold were the one gap the traceability matrix had recorded; they are
now named fields (`AccountRouter.handoff_context`), read from the project memory
(`decisions`/`constraints`) and the run's durable QA verdicts (`test_results`),
with `uncommitted` stating honestly that a fresh sandbox inherits no working
tree.

### 5. Старый исполнитель не может конфликтовать с новым — **VERIFIED**
`tests/test_race_protection.py` (13 tests) and
`tests/test_acceptance_e2e_failover.py`: a switch records the abandoned
conversation, stops it, and the branch stays held (`_BRANCH_FREE`) until the
stop is confirmed; the partial unique index `uq_tasks_workspace_active` (inside
a `SAVEPOINT`) is the real lock, and the fencing token (`lease_token`) makes a
stale worker's write a no-op. The live `recovery` check proved the token
advances and a bound conversation is not reclaimed.

### 6. lab_worker действительно запускается — **VERIFIED**
`tests/test_lab_worker_process.py`: a real `python -m orchestration.lab_worker`
**child process** registers (`kind=worker`, fresh heartbeat), passes the
`lab_worker` diagnostic as `VERIFIED`, and takes a run to `completed` with the
server's own loop off (`NEXUS_LAB_INLINE=0`). `tests/test_worker_setup.py`
checks the three launch paths (systemd unit, Docker Compose, Render) point at the
same module.

### 7. Автономная задача продолжается без браузера — **VERIFIED**
`tests/test_acceptance_worker.py` (3 tests) and
`tests/test_comprehensive_suite.py`: with no client connected the separate
worker claims, dispatches and completes the run; a restarted server reads the
same completed run from the database.

### 8. Worker восстанавливается после перезапуска — **VERIFIED** (fixed here)
`tests/test_acceptance_recovery.py` (7 tests) and
`tests/test_lab_worker_process.py`: a `SIGKILL`ed worker is replaced by a new
process that continues the run and starts **no** task twice; an expired lease
with no conversation is reclaimed and re-claimed; an expired lease **with** a
conversation is not requeued (that would duplicate it); a stale worker cannot
settle a task another worker has taken. The flaky acceptance copy was fixed in
§2.1.

### 9. Реальная тестовая задача выполняется через NEXUS — **VERIFIED (live)**
Driven through the real `LabStore` + `OpenHandsExecutor` + `GitVerifier` +
`LabScheduler` against the live Cloud (no mocks). The agent really created
`hello_nexus.py` (`add(a,b)`), `test_hello_nexus.py`, `.gitignore`, installed
`pytest` from PyPI and ran it: `1 passed in 0.01s`. NEXUS recorded the task
`completed_unverified` — honestly, because no repository was bound
(`no repository bound; cannot verify remote state`) — and blocked the run
(`blocked: tasks remain unverified`). This is the request's own rule: a text
answer is not proof of work. The full agent report is in §8.

### 10. Ошибки обнаруживаются и исправляются — **VERIFIED**
The one failure this task found was read, its cause identified (a test race,
already fixed for the sibling test), fixed, and confirmed by two full runs. The
diagnostics distinguish `FAILED` (ran and failed) from `BLOCKED` (could not run)
and `UNVERIFIED` (not run); the live report had 0 FAILED.

### 11. Evidence Engine подтверждает результаты — **VERIFIED**
`tests/test_evidence_trust.py`, `tests/test_lab_evidence.py`,
`tests/test_pipeline_e2e.py`, and a live round trip in this sandbox: a record
signed with an Ed25519 key from `NEXUS_EVIDENCE_PRIVATE_KEY` re-verified as
`VERIFIED`; editing the `commit_sha` column afterwards made re-verification
`FAILED`. Only a trusted asymmetric signature yields `VERIFIED`; HMAC never can.

### 12. Существующие тесты NEXUS не регрегируют — **VERIFIED**
Full suite green before the fix apart from the one flake; green after the fix,
twice: **1337 passed, 38 skipped, 0 failed**. `ruff check .` clean.

---

## 4. What was delivered in the run (commits `7841603..6331f0a`)

The run baseline is `7841603`; the source HEAD is `6331f0a` (45 commits). The
capabilities added:

- **OpenHands account pool** — any number of accounts; per account: id, label,
  base URL, transport, encrypted key, masked hint, auth/health state, last
  check, live load, active tasks, error history, limits, priority, enabled. Add
  / edit / remove (guarded by active tasks) / enable / disable / test connection
  / verify authorization / set default / fallback order / selection mode.
- **Four selection modes** — `manual`, `auto`, `failover`, `load_balancing`,
  with a derived live load and a per-account concurrency ceiling.
- **Automatic failover with context hand-off** — a switch is a **new**
  conversation on the new account with the full context; the old conversation is
  stopped; the switch is recorded in `nexus_task_handoffs`.
- **Autonomous lab** — `lab_worker` as a separate process (systemd / Compose /
  Render), worker heartbeat (`nexus_lab_workers`), leases, fencing tokens,
  recovery, durable runs with retry cycles and reports.
- **The full pipeline** — USER PROMPT → PLANNER → DAG → SCHEDULER → OPENHANDS →
  EXECUTION → QA → EVIDENCE → ACCEPTANCE → NOTIFICATION
  (`tests/test_pipeline_e2e.py`).
- **AI provider cascade** — Gemini → Groq → OpenRouter → OpenHands, with a real
  read-only `probe()` per provider (never a prompt), separate from the account
  pool.
- **OpenHands tools & MCP diagnostics** — the agent-server tool catalog mapped to
  the required capabilities, the configured MCP server names, and the NEXUS tool
  surfaces; three earned statuses.
- **Security** — one permission policy for both address checks, an SSRF guard,
  MCP tool scopes, log redaction, RLS on the session-key tables.
- **Studio (section 12)** — OpenHands settings (accounts, add/edit/remove, test
  connection, default, fallback order, selection mode, health, error history)
  and the run view (current executor, conversation id, state, events, switches,
  a manual executor switch).
- **CLI & MCP** — `nexus openhands accounts|pool|diagnostics|tools|switch|…`,
  the MCP server over stdio and Streamable HTTP.
- **Documentation** — `ENVIRONMENT.md` (new, the single reference), plus
  `OPENHANDS_SETUP`, `OPENHANDS_ACCOUNTS`, `WORKER_SETUP`, `FAILOVER`,
  `TROUBLESHOOTING`, `TESTING`, `DURABLE-RUNS`, `OPENHANDS_INVENTORY`, `DEPLOY`,
  `README` brought in step.

---

## 5. Environment variables

Full list and defaults: `docs/ENVIRONMENT.md`. The variables this work added or
made load-bearing (names only — **no secret value is in any committed file**):

| Variable | Purpose |
| --- | --- |
| `OPENHANDS_TRANSPORT` | `direct` (default) or `app` |
| `OPENHANDS_DEFAULT_MODEL` | the model a bare sandbox uses (required in practice) |
| `OPENHANDS_DIRECT_REPO_MODE` | `prompt` / `refuse` / `allow` for repository selection on `direct` |
| `OPENHANDS_AGENT_PROFILE_ID` | opt-in profile for the web path |
| `OPENHANDS_CLOUD_URL` | Cloud base URL |
| `OPENHANDS_ORCHESTRATOR_MODEL` | the orchestrator provider's model |
| `NEXUS_LAB_INLINE` | `1`/`0` — advance runs in the web process |
| `NEXUS_TASK_TIMEOUT_SECONDS` | per-task time limit (default 7200) |
| `NEXUS_TASK_STALL_SECONDS` | durable-run stall watch |
| `NEXUS_RETRY_BASE_SECONDS` / `NEXUS_RETRY_MAX_SECONDS` / `NEXUS_RETRY_MAX_CYCLES` | retry policy |
| `NEXUS_ALLOW_PRIVATE_TARGETS` / `NEXUS_OPENHANDS_ALLOWED_HOSTS` | the one address-policy opt-in |
| `NEXUS_SPEC_MAX_CHARS` / `NEXUS_SPEC_INLINE_CHARS` / `NEXUS_SPEC_MESSAGE_CHARS` / `NEXUS_SPEC_STAGE_TOKENS` / `NEXUS_SPEC_SANDBOX_DIR` | task-text sizing |
| `NEXUS_EVIDENCE_PRIVATE_KEY` / `NEXUS_EVIDENCE_KEY_ID` / `NEXUS_EVIDENCE_KEYRING` | Evidence Trust Engine signing |
| `NEXUS_SECRET_KEY` | Fernet key for stored credentials |
| `TRUSTED_PROXY_HOPS` | client-IP resolution |
| `LOGIN_MAX_ATTEMPTS_PER_IP` / `LOGIN_MAX_ATTEMPTS_GLOBAL` | login ceilings |
| `GEMINI_*` / `GROQ_*` / `OPENROUTER_*` (`_API_KEY`, `_MODEL`, `_BASE_URL`) | provider cascade |
| `NEXUS_DB_URL` / `SUPABASE_DB_URL` / `DATABASE_URL` / `PSYCOPG_PREPARE` / `AUTO_MIGRATE` | database |

---

## 6. Run commands

```bash
# Tests (PostgreSQL 17 required for the DB-backed modules; they skip without it).
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/db" \
  python -m pytest tests/ -q -p no:cacheprovider

# Lint (same version as CI).
python -m ruff check .

# The web instance (UI + dispatcher + inline lab loop).
python -m orchestration.web_server --host 0.0.0.0 --port "$PORT" --token "$WEB_AUTH_TOKEN"

# The autonomous lab worker as a separate process.
python -m orchestration.lab_worker --worker-id lab-1

# MCP (stdio) and the CLI.
python -m orchestration.mcp_server
python -m orchestration.cli doctor

# OpenHands diagnostics (opt-in live probe) — HTTP and CLI.
curl -s -X POST "$NEXUS_URL/api/openhands/diagnostics" -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  -H 'Content-Type: application/json' -d '{"live": true}'
python -m orchestration.cli openhands diagnostics --live

# A safe restore point (local, offline).
python -m orchestration.cli restore-point create --label before-change
```

Migrations are applied by the process on first connect (`AUTO_MIGRATE=true`),
idempotently, in filename order (`0001`–`0011`, `0014`–`0032`; `0012`/`0013` are
reserved for the deferred File Lab).

---

## 7. Test counts

| Run | Result |
| --- | --- |
| Full suite, before the fix | 1 failed, 1336 passed, 38 skipped |
| Full suite, after the fix (×2) | **1337 passed, 38 skipped, 0 failed** |
| Browser E2E alone (Chromium installed) | 29 passed |
| Acceptance sections A–G (`tests/test_acceptance_*.py`, 7 files) | 50 tests |
| Pool / handoff / recovery / worker / failover focus | 46 passed |

Test files: 83. Files added since the run baseline: 26. The 38 skips are
environmental, not failures: **29** browser E2E (Chromium was absent — installing
it makes those 29 pass, leaving 9 skipped), 4 Android APK (no SDK build-tools), 3
Windows `.exe` (no mingw-w64), 1 pooler schema-bootstrap (`TEST_POOLER_DSN`
unset), 1 section F live variant (see §12). With Chromium installed the full
suite is **1366 passed, 9 skipped**.

---

## 8. Real OpenHands results

**Live diagnostics** (`OpenHandsDiagnostics.run("machine:default", live=True)`
against `https://app.all-hands.dev`, model `openhands/deepseek-v4.1-flash`):

```
counts: {'VERIFIED': 17, 'FAILED': 0, 'BLOCKED': 0, 'UNVERIFIED': 1}
VERIFIED  database / schema / accounts / account_pool / cloud_api
VERIFIED  queues_leases   (claim → release → re-claim round trip)
VERIFIED  recovery        (expired lease reclaimed, higher fencing token,
                           a bound conversation NOT re-claimed)
VERIFIED  providers       (gemini, openrouter, openhands configured)
VERIFIED  provider_probe  (gemini ok, openrouter ok, openhands ok)
VERIFIED  openhands_tools (19 tools; terminal, workspace, git, testing available)
VERIFIED  mcp_configuration (20 MCP servers configured)
VERIFIED  nexus_tools     (15 MCP tools, 7 client tools)
VERIFIED  conversation_create / message_and_events / run_command
VERIFIED  task_completion (terminal state 'finished')
VERIFIED  workspace_rw    (file uploaded and read back, SHA-256 matched)
UNVERIFIED lab_worker     (no live worker, but no active run needed one)
```

The agent's own report from the live probe (marker echoed, file written) is in
the evidence above. The account has **no GitHub provider token**
(`provider_tokens_set: {'github': None}`), so a live clone-and-push is not
possible from this account — the documented limitation, unchanged.

**A real development task through NEXUS** (acceptance condition 9), driven by
the real scheduler against the live Cloud:

```
[03:34:18] run=running  tasks=[('T1','running','82da8f0b-…')]
[03:34:52] run=blocked  tasks=[('T1','completed_unverified','82da8f0b-…')]
FINAL_STATE blocked
TASK T1 completed_unverified attempt=1
  reason: "no repository bound; cannot verify remote state"
```

The agent's report: it inspected the workspace (an empty git repo), created
`hello_nexus.py` (`add(a, b)` → `a + b`), `test_hello_nexus.py`, `.gitignore`,
installed `pytest` 9.2.0 from PyPI (the only package, because running the tests
was an acceptance criterion), ran `python -m pytest` → `1 passed in 0.01s`, and
named its local commit `7c760674…`. It could not push (no remote, no provider
token) and said so. NEXUS did **not** accept the text as proof: with no
repository bound it is `completed_unverified`, and the run blocked — exactly the
required behaviour.

---

## 9. Account-switching results

`tests/test_openhands_failover.py` and `tests/test_acceptance_e2e_failover.py`,
over real HTTP servers + real PostgreSQL + real `git`:

- a real task starts on account #1 (a file is written in #1's real clone);
- #1 fails (its conversations vanish); the task moves to #2;
- the context is handed over (message + a durable `nexus_task_handoffs` row);
- #1 is **stopped** — its conversation is stopped and its checkout shows only
  the artefact from before the failure; a stopped client refuses to write;
- #2 commits and pushes; an independent check reads the real commit;
- each account started exactly one conversation (no duplication).

A **two-live-account** switch is **BLOCKED**: only one OpenHands key is
available in this environment. It is not simulated — the switch is proven end to
end against real PostgreSQL with honest transport fakes, which is the strongest
evidence available without a second key.

---

## 10. Recovery results

- **Worker killed mid-run**: a new process continues the run and starts no task
  twice (`test_acceptance_recovery.py`, `test_lab_worker_process.py`).
- **Expired lease, no conversation**: reclaimed and re-claimed; the fencing
  token advances, so the dead worker's writes are no-ops.
- **Expired lease, conversation bound**: not requeued (that would be a
  duplicate); no second task is claimable.
- **Lost connection to OpenHands**: the task is requeued for failover with the
  failed account excluded and the abandoned conversation recorded.
- **Database blip**: the worker loop catches `StoreUnavailable`, logs it and
  retries (`test_planner_worker.py::test_the_worker_loop_outlives_a_transient_database_error`).
- **Live**: the `recovery` diagnostic was `VERIFIED` against the real database
  (expired lease reclaimed, higher token, bound conversation untouched).

---

## 11. Known limitations

- **One live OpenHands key.** A two-live-account failover is BLOCKED here.
- **No GitHub provider token on the account.** The live account cannot clone or
  push, so a live task ends `completed_unverified` and the run blocks.
- **`delivery.mode = part` is weaker than a file.** On the `app` transport an
  agent cannot read what a cross-reference points outside its part.
- **The `app` transport pauses at the sandbox level only** (`stop_conversation`
  returns `False`); the `direct` transport pauses the conversation. The branch
  lock — not the stop — is what keeps two executors off one checkout.
- **Cancelling a run / a deadline / a time limit does not stop a conversation
  already running.** "Resume" after a time-out starts a second conversation.
- **Render's free plan stops the process after ~15 min idle**; a run that must
  work unattended needs a paid instance or an external keep-alive.
- **Nothing limits concurrent runs on different branches**; on one branch tasks
  take turns, but a person or bot pushing still moves the head.
- **Stored OpenHands accounts are not used for execution in the studio chat** —
  workflows run under the server `OPENHANDS_API_KEY`; the pool is used by the
  lab loop and the diagnostics.
- **Safari and Firefox were not run**; only Chromium.

---

## 12. Remaining BLOCKED and UNVERIFIED items

| Item | Status | Why |
| --- | --- | --- |
| Two-live-account failover | **BLOCKED** | only one OpenHands key is available |
| Live clone-and-push through NEXUS | **BLOCKED** | the account has no GitHub provider token |
| `tests/test_comprehensive_suite.py` section F (live) | **BLOCKED** (skipped with reason) | no second key / sandbox in this environment |
| `lab_worker` diagnostic | **UNVERIFIED** | no live worker was running during the non-live report; the worker-process tests verify it |
| `openhands_tools` without `live` | **UNVERIFIED** | reading the catalog may provision a sandbox, so it is opt-in; with `live` it is VERIFIED |
| Pooler schema bootstrap | **BLOCKED** (skipped) | `TEST_POOLER_DSN` unset |
| Android APK / Windows `.exe` runtime | **BLOCKED** (skipped) | no SDK build-tools / no mingw-w64, no host to run them |

Nothing above is reported as a pass that was not observed. Where a live check
could not run, it is BLOCKED; where one was not requested, it is UNVERIFIED.

---

## 13. How to reproduce this report

```bash
# 1. A disposable PostgreSQL 17 and the pinned tooling.
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium           # for the browser E2E

# 2. The whole suite (DB-backed modules skip cleanly without the DSN).
TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/db" \
  python -m pytest tests/ -q -p no:cacheprovider

# 3. The live OpenHands diagnostics (creates and deletes one conversation).
NEXUS_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
OPENHANDS_API_KEY=... OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash \
  python -m orchestration.cli openhands diagnostics --live
```

No secret is written to any file by this report or by the code it describes.
