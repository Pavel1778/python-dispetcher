# Performance: how NEXUS is measured

A claim of "faster" is worth nothing without a number. This document describes
the reproducible benchmark and records a baseline. It is deliberately about the
*real* paths a person and the loop use, not synthetic micro-benchmarks.

## The benchmark

`tools/benchmark.py` starts the real web server (`orchestration.web_server.App`
+ `WebServer`) against a real PostgreSQL and the same stand-in OpenHands,
GitHub and LLM servers the test suite uses (`tests/fake_cloud.py`,
`tests/fake_github.py`, `tests/fake_runtime.py`, `tests/test_providers.py`). It
resets the schema, seeds several projects, each with conversations and messages,
then measures each path with `perf_counter`, reporting min/p50/p90/p99/max in
milliseconds.

```bash
TEST_SUPABASE_DB_URL=postgresql://user:pw@host/db \
  python tools/benchmark.py --out after.json \
    --repeat 15 --projects 5 --conversations 20 --messages 50
```

`tools/benchmark_compare.py before.json after.json` prints the p50/p90 of each
path in both runs and the change, so a before/after comparison is a table, not
a sentence.

## What is measured

| Path | What it exercises |
| --- | --- |
| `studio_status_ms` | `GET /api/studio` — what is configured, booleans only |
| `list_projects_ms` | `GET /api/v1/projects` |
| `open_project_ms` | `GET /api/v1/projects/{id}` |
| `list_conversations_ms` | the sidebar list (archived included) |
| `open_conversation_ms` | one conversation with its joined repository |
| `fetch_history_ms` | the message history of a conversation |
| `commands_registry_ms` | `GET /api/commands` (static) |
| `search_by_content_ms` | search by the text of messages |
| `export_conversation_ms` | the self-contained export |
| `chat_turn_ms` | a full chat turn through the provider cascade |
| `start_run_ms` | planning + an autonomous run start through the chat |
| `store_dashboard_counts_ms` | the store's own aggregate query |
| `file_upload_ms` | an opaque file upload (4 KiB) |

## Baseline

Measured in this sandbox (Python 3.13.15, local PostgreSQL, the stand-in
servers on loopback) with `--repeat 5 --projects 2 --conversations 5
--messages 20`:

| Path | p50 (ms) | p90 (ms) |
| --- | ---: | ---: |
| studio_status | 0.82 | 4.76 |
| list_projects | 1.11 | 1.27 |
| open_project | 1.72 | 1.83 |
| list_conversations | 1.42 | 1.49 |
| open_conversation | 1.44 | 2.42 |
| fetch_history (20 msgs) | 1.74 | 2.11 |
| commands_registry | 0.77 | 0.93 |
| search_by_content | 1.71 | 2.02 |
| export_conversation | 2.38 | 2.72 |
| chat_turn | 23.30 | 23.92 |
| start_run | 47.35 | 61.50 |
| store_dashboard_counts | 0.61 | 2.09 |
| file_upload (4 KiB) | 3.14 | 4.04 |

The read paths are single-digit milliseconds: the store holds one connection and
every query is a small indexed lookup. `chat_turn` and `start_run` are dominated
by the provider and the planning, not by NEXUS's own code — on the stand-in
they are already a fraction of a second, and against a real provider they are
the provider's latency. There is no NEXUS-side hotspot to optimise on these
paths; the numbers above are the evidence for that, not an assertion.

### Re-measured (2026-11-14, this session)

`--repeat 10 --projects 3 --conversations 10 --messages 40` (a larger dataset
than the baseline, so slightly higher on the list paths that scan more rows):

| Path | p50 (ms) | p90 (ms) |
| --- | ---: | ---: |
| studio_status | 1.16 | 1.43 |
| list_projects | 1.34 | 2.04 |
| open_project | 3.32 | 4.66 |
| list_conversations | 2.61 | 3.50 |
| open_conversation | 1.71 | 2.29 |
| fetch_history (40 msgs) | 2.31 | 2.77 |
| commands_registry | 0.83 | 0.95 |
| search_by_content | 1.85 | 2.05 |
| export_conversation | 3.58 | 5.37 |
| chat_turn | 27.35 | 36.99 |
| start_run | 65.07 | 79.66 |
| store_dashboard_counts | 0.55 | 0.64 |
| file_upload (4 KiB) | 3.78 | 8.20 |

Every path stays in the same band as the baseline: the read paths are a few
milliseconds, the provider-bound paths are dominated by the stand-in LLM, and
the store's own aggregate query is sub-millisecond. **No regression, and no
speedup is claimed** — the comparison table (`tools/benchmark_compare.py`) is
the instrument for any such claim.

## Optimisations applied

- **Indexes for the list order.** The conversation list orders by
  `(pinned DESC, pinned_at DESC NULLS LAST, updated_at DESC, id)` and filters
  `deleted_at IS NULL`; migration `0037` adds `idx_nexus_conversations_pinned`
  and `idx_nexus_conversations_live` so neither list is a sort.
- **One connection, no reconnect churn.** `PostgresStore` reuses one connection
  and checks it before each transaction with a single `poll` (no round trip on
  a healthy connection); a dropped connection is replaced on evidence
  (`tests/test_store_reconnect.py`).
- **Content search is a bounded `EXISTS`.** Searching by message text is an
  `EXISTS (SELECT 1 … lower(content) LIKE …)` over the conversation's messages,
  not a join that duplicates rows.
- **Pagination and limits on every list.** Conversations cap at 200, messages at
  500, runs at 50 by default — a page, not the whole table.
- **The pinned section is a client-side partition of one read**, not a second
  request: `GET /conversations` already returns the `pinned` flag and the list
  is already ordered.

## What was not done, and why

- **No language migration.** A Go/Rust rewrite of the API would not move these
  numbers: the read paths are already sub-millisecond of database time and the
  slow paths are network-bound (provider, OpenHands, GitHub). The cost — a
  second implementation, a second test suite, the loss of the stdlib-only
  adapters and the entire existing test corpus — is not justified by the
  measured profile. A hybrid (a Rust streaming engine, a Go worker) would only
  pay off under a load this deployment does not have, and would split the
  invariants the AGENTS.md documents in one place. The evidence is the table
  above.
- **No caching layer.** The read paths are cheap and correctness (project
  isolation, a message appearing at once) matters more than a cache that could
  serve one project's data to another. Nothing is cached between projects.

## Adding a measurement

Add one entry to `tools/benchmark.py`'s `results["measurements"]` and it appears
in both the JSON and the comparison table. Keep the method: a real server, a
real database, a real round trip — never a simulated one.
