# Phase 4 consolidation audit (union of Phase 4 and PR #42)

Status of each claim: **VERIFIED / MEASURED / INFERRED / BLOCKED / NOT VERIFIED**.
Every SHA was re-checked on 2026-11-15; nothing here is assumed from a report.

## 1. Repository preservation

| Fact | Value |
|---|---|
| Working branch | `engineering/performance-evolution` |
| Its HEAD | `ce70f07073d3a14f0a160fe7fc2393308e3c348e` (uncommitted Phase-4 work on top) |
| `origin/main` | `28aa026b0d22b82a0befd9a36492aabc09aabc49` (PR #42 merged) |
| Merge base | `5ebbcec9b69a245c2458fbe131fd75039d103da2` |
| `ce70f07` merged into `origin/main`? | **No** (`merge-base --is-ancestor` fails) |
| `0e5b2dd` | **Does not exist** (`git cat-file -t 0e5b2dd` → not a valid object) |

Worktrees present (none removed, none rewritten):

| Path | HEAD | Branch |
|---|---|---|
| `/workspace/nexus-audit/repo` | `ce70f07` | `engineering/performance-evolution` |
| `/tmp/p34_wt` | `cfeda5a` | `consolidate/phase34` — **a parallel OpenHands session** |
| `/tmp/main_p41` | `2021458` | detached |
| `/tmp/p42_test` | `55a0d5e` | detached |
| `/tmp/p5_wt` | `ce70f07` | `phase5/execution-intelligence` — **this work** |

The parallel session (`/tmp/p34_wt`) is doing its own consolidation. It was
**not touched**: no reset, clean, checkout or commit from it. This work is in a
separate worktree on a separate branch, against a **dedicated PostgreSQL 17
cluster on port 5435** (`/tmp/pg_p5`, database `nexus_p5`).

## 2. The two implementations of terminal-output recovery

| | Phase 4 (this branch's uncommitted work) | PR #42 (`935ebfe`) |
|---|---|---|
| Transport to the file | conversation-scoped `download-trajectory` ZIP | **global** `GET /api/file/download?path=` |
| How the file is found | bytes **start with the preview prefix** | file name **recomputed** from the event |
| ZIP decompression | bounded per member (`bounded_zip_member`, 64 MiB) | n/a (no ZIP) |
| Scoping | project/run/task checked; archive is the conversation's own dir | project/run/task checked |
| MCP / CLI | `list_run_task_outputs` / `get_run_task_output` / `nexus run outputs\|output` | none |
| Tests | `tests/test_artifacts.py` (15) | none in the commit ("WIP, unverified") |
| Claim about the file | "the full text" | the **clipped rendering**, not raw stdout |

The task forbids the **global unscoped `/api/file/*` route**, so PR #42's
transport is not adopted. PR #42's **source verification** (recompute the file
name from the event's own fields) is strictly stronger than a prefix match and
is adopted on top of the scoped transport.

### 2.1 Source verification (PR #42) — VERIFIED against the live runtime

`expected_file_name`/`render_observation` (in `nexus_outputs.py`) rebuild the
SDK string `TerminalObservation.to_llm_content` hashes, then
`terminal_output_<sha256(rendered)[:8]>.txt`. Checked against **every** real
truncated observation on the local agent-server:

- recomputed name is present in the conversation's own `observations/` dir: **11/11**;
- the saved file **equals** the recomputed rendering **byte for byte**: **11/11**.

So the recovered file is provably *this* event's rendering, not a file that
happened to match.

### 2.2 ZIP security (Phase 4) — kept, and it is load-bearing

- compressed archive bounded (`TRAJECTORY_MAX_BYTES`, 64 MiB);
- each member bounded on its own (`bounded_zip_member`, `OUTPUT_MAX_BYTES`),
  reading `ZipInfo.file_size` **and** capping the stream;
- a `..` segment is skipped; only `terminal_output_*` members are read;
- a corrupt archive is an error; no global file route is used.

The decompressed bound was a real defect found earlier (a ~190 KB archive
expanded to 200 MB via `archive.read`). Removing it fails
`test_a_zip_bomb_member_is_refused_by_the_decompressed_limit`.

## 3. The union applied

`orchestration/nexus_outputs.py`:

- `render_observation(observation)` and `expected_file_name(observation)` — PR #42's recomputation, adopted.
- `run_task_output(...)` now recomputes the expected name from the event and
  passes it to the transport; when the event cannot rebuild its rendering it
  refuses (`NotFound`) rather than guessing.

`orchestration/cloud_direct.py`, `orchestration/local_runtime.py`:

- `full_terminal_output(conversation_id, prefix, *, expected_name=None)` — the
  scoped ZIP transport, now refusing a member whose name is not `expected_name`.

`orchestration/cloud_client.py` (app transport): same signature, still
`ArtifactsUnsupported`.

`orchestration/studio/app.js`: the dialog now says the file is OpenHands' own
clipped rendering with its wrapper, **not** the raw command output.

## 4. Overlapping files (Phase 4 vs PR #42)

| File | Phase 4 | PR #42 | Resolution |
|---|---|---|---|
| `cloud_direct.py` | `full_terminal_output` via trajectory | `download_file` via global route | **Phase 4 kept**; `expected_name` param added |
| `openhands_client.py` | `download_trajectory`, `bounded_zip_member` | global download helper | Phase 4 kept |
| `web_server.py` | two output routes | different routes | Phase 4 kept |
| `studio/app.js` / `app.css` / `index.html` | terminal-output dialog | its own dialog | Phase 4 kept; label corrected |
| `fake_runtime.py` | trajectory route + `trajectory_raw` | small change | Phase 4 kept |
| `terminal_output.py` | — | the whole module | **not adopted** (global route); its recomputation logic adopted into `nexus_outputs.py` |

## 5. The pool-vs-PR-#41 blocker — VERIFIED, BLOCKED (owner decision)

`origin/main` now carries PR #41 (`2021458`): `tests/test_connection_concurrency.py`
plus docs whose headline is **"there is no connection pool anywhere in this
codebase"**. This branch (Phase 3, `c3127e8`) **replaced the single connection
with `psycopg_pool`** and removed `store._conn` / `store._lock`. Re-checked on
this host:

- `engineering/performance-evolution` `cloud_store.py` uses `psycopg_pool.ConnectionPool`, `_ensure_pool`;
- `origin/main` `cloud_store.py` uses a single `self._conn` and `_ensure_pool` does not exist;
- `origin/main` has `tests/test_connection_concurrency.py`; this branch does not.

This is a **semantic** conflict: `main` asserts one connection, the branch
asserts a pool. No automated resolution is correct; the owner must choose.
**This work does not merge `origin/main` and does not touch that test.** The
parallel session reached the same conclusion independently.

## 6. Verification after the union

- `ruff check .` → clean.
- `node tests/studio_render_test.js` → passed.
- `tests/test_artifacts.py` → 19 passed (15 + 4 source-verification cases).
- `test_artifacts + test_openhands_output + test_cli_mcp + test_route_auth` → 55 passed.
- Source-verification tests are **load-bearing** (removing the name check fails
  two of them).

Full-suite numbers are recorded in the Phase-5 final report.
