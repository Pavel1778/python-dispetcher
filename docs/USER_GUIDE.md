# NEXUS — User Guide

How to start NEXUS, connect it, and run your first task. For the environment
reference see [ENVIRONMENT.md](ENVIRONMENT.md); for deployment see
[DEPLOY.md](../DEPLOY.md); for what is not done see
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

---

## 1. What NEXUS does

You describe work in plain language. NEXUS plans it, dispatches the parts to
**OpenHands** agents in isolated sandboxes, checks their results against the real
GitHub branch, and reports honestly — `completed_verified` when the change is
real, `completed_unverified` or `blocked` when it is not. It never says "done"
of work it did not see.

## 2. Start it

```bash
git clone https://github.com/Pavel1778/python-dispetcher && cd python-dispetcher
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# the secrets it needs (see ENVIRONMENT.md for every variable)
export SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/nexus"
export WEB_AUTH_TOKEN="$(python -m orchestration.web_auth secret)"
export AUTH_USERNAME=owner
export AUTH_PASSWORD_HASH="$(python -m orchestration.web_auth hash)"   # prompts
export AUTH_SESSION_SECRET="$(python -m orchestration.web_auth secret)"
export OPENHANDS_API_KEY="sk-..."        # your OpenHands Cloud key
export GITHUB_TOKEN="ghp_..."            # a PAT with repo scope (read + write)
export OPENHANDS_DEFAULT_MODEL="openhands/deepseek-v4.1-flash"

python -m orchestration.web_server --host 127.0.0.1 --port 8000 --token "$WEB_AUTH_TOKEN"
```

Open <http://127.0.0.1:8000> and sign in with `AUTH_USERNAME` / the password you
hashed. The schema creates itself on first start (idempotent migrations).

**The web server advances autonomous runs by itself** (its own loop). On a host
that stops idle processes (e.g. Render's free plan) run the worker as well, so a
run keeps moving: `python -m orchestration.lab_worker`.

Health: `GET /healthz` (liveness) and `GET /readyz` (liveness + database).

## 3. Connect a repository

1. **Settings → Подключения**. Add a **GitHub account** (paste a PAT). NEXUS
   verifies it with `GET /user` before storing it, encrypted.
2. Add an **OpenHands account** (paste the key). NEXUS verifies it and records
   its capabilities. You may add several and set a selection mode (manual /
   auto / failover / load-balancing) and a fallback order.
3. **Connect a repository**: pick an account, pick a repository and a **branch**.
   NEXUS looks both up on GitHub with that account's token and binds the project
   to them. The branch you choose is the one tasks run on — the executor is told
   to push there and the verifier reads *that* branch.

> Use a dedicated branch (not `main`) for agent work unless you intend the
> agent to push to the default branch.

## 4. Run your first task

In the chat, pick a mode and describe the work:

| Mode | What a message does |
| --- | --- |
| **Авто** (default) | A work order plans and runs immediately. |
| **Автономно** | Same, explicitly autonomous. |
| **Вручную** | A work order gets a **plan**; nothing runs until you reply **«Запускай»**. |
| **Вопрос / Разбор / План** | Answers and analysis only; never starts a run. |

A question is always answered, in every mode, and starts nothing.

Example: *"Создай файл hello.py с функцией hello() и тест к нему; прогони тесты,
закоммить и запушь в ветку."*

What happens, in order:

1. NEXUS **plans** the request into a task DAG (you can see it on the «Агент» tab).
2. The scheduler **claims** each ready task and **dispatches** it to OpenHands.
3. The agent works in an isolated sandbox that has your repository cloned and a
   Git credential to push.
4. NEXUS **polls** the conversation, then **verifies** the result against the
   real GitHub branch head.
5. A verified task unblocks its dependents; a task that did not verify is
   reworked (durable) or stops the run (bounded).
6. When every task is `completed_verified`, the run is `completed` and a
   **report** is written and delivered to the chat.

## 5. Watch and control a run

On the **«Агент»** tab:

- **run card** — state, iteration budget, the current executor and conversation
  id, a **time profile** (where the wall-clock time went), switches, events.
- **tasks** — each task's state and, when it settled, its verification reason.
- **actions** — **Пауза** (stop issuing new tasks; running conversations are not
  stopped), **Продолжить**, **Отменить** (terminal; leaves a stop signal),
  **Повторить неуспешные** (retry only the tasks that did not verify),
  **Вывод команд** (the terminal output a conversation cut off).
- **Архив прогонов** — finished runs.

**Notifications** (bell) tell you when work starts, when a task verifies or
fails, when a run is blocked and needs you, and when a plan awaits your
approval.

## 6. Verify honestly

NEXUS distinguishes, and shows, the difference between:

- **`completed_verified`** — the branch head on GitHub is a commit the agent
  named, and the branch moved (for a change-requiring task);
- **`completed_unverified`** — the agent finished but the result could not be
  confirmed (no push, or the branch did not move);
- **`failed` / `blocked`** — the work failed, or a check could not be made.

You can also run the project's own checks in an isolated checkout, by hand:

```bash
nexus qa run --project <id>            # or: nexus qa run --repo /path/to/repo
```

It runs the repository's declared checks (`.nexus-qa.json`, or lint + tests) and
reports `passed` / `failed` / `skipped` / `blocked`. `skipped` is never a pass.

## 7. The CLI

```bash
export NEXUS_URL=http://127.0.0.1:8000 NEXUS_TOKEN="$WEB_AUTH_TOKEN"
nexus doctor                 # connectivity and configuration
nexus status                 # dashboard counts
nexus project list
nexus project show <id>
nexus run show <id>          # a run, its tasks and its report
nexus run cancel <id>
nexus qa run --project <id>  # the project's own checks, isolated
nexus openhands diagnostics --live
nexus openhands inventory
nexus providers --live
nexus restore-point create   # a local git-bundle snapshot (no API needed)
```

## 8. After a restart

NEXUS is durable. Stopping the server (gracefully or with a kill) and starting it
again preserves projects, conversations, goals, runs, tasks, events and
notifications. A run that was in flight is picked up: an expired lease is
reclaimed and the conversation is adopted, never duplicated. A conversation that
was already running in the cloud keeps running and is collected when the service
is back.

## 9. Troubleshooting

- **"no orchestrator provider configured"** — set `GEMINI_API_KEY`,
  `GROQ_API_KEY` or `OPENROUTER_API_KEY` for the chat/planning cascade (the
  executor is OpenHands, a separate thing).
- **A task ends `completed_unverified` with "the branch did not move"** — the
  agent could not push. Check the GitHub credential (`GITHUB_TOKEN` or the bound
  account) and that the project is bound to the branch you expect.
- **A bare sandbox fails with an LLM auth error** — set
  `OPENHANDS_DEFAULT_MODEL` to a model the sandbox can reach.
- **`/readyz` fails** — the database is unreachable; see `DEPLOY.md`.
- The full check list is `GET /api/openhands/diagnostics` and
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 10. What NEXUS will not do

It does not claim success it did not verify; it does not delete your
conversations without a retention rule; it does not run a second conversation
for work already under way; and it does not give an agent a credential you did
not configure. Known gaps are in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
