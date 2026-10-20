# Multi-agent orchestration platform

Durable control-plane state for coordinating multiple OpenHands agents.

A **workflow** holds a goal. A **Sr** (senior) OpenHands conversation plans it and
delegates each unit of work to **Jr** tasks through client tools. A dispatcher
runs each Jr to completion, records the result, and feeds it back to the Sr.

```
workflow ──▶ Sr conversation ──sr_create_task──▶ task ──▶ Jr conversation
                     ▲                                        │
                     └────────────── result ──────────────────┘
```

State lives in PostgreSQL; conversations run on OpenHands Cloud. The default
Cloud transport provisions a sandbox and drives its agent-server directly
(the `direct` transport); the legacy app-conversation endpoint is kept behind
`OPENHANDS_TRANSPORT=app`. See `DEPLOY.md` for the deployment guide,
`AGENTS.md` for the repository layout, and `CLOUD-INTEGRATION-MAP.md` for the
Cloud API surface.

## Local development

```bash
export SUPABASE_DB_URL="postgresql://postgres@127.0.0.1:5432/ohp_test"
python3 -m pytest -q                     # unit + integration (needs PostgreSQL)
python3 tests/e2e_local_sr_jr.py         # real Sr→Jr on a local agent-server
python3 tests/e2e_cloud_sr_jr.py         # real Sr→Jr on OpenHands Cloud
```

The end-to-end scripts are not collected by pytest; they need a live LLM (and,
for the Cloud one, `OPENHANDS_API_KEY` and `OPENHANDS_DEFAULT_MODEL`).
