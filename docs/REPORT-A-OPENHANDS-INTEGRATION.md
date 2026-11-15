# REPORT A — OpenHands Integration (2026-11-15)

Scope: what NEXUS can and cannot do against OpenHands, with evidence.
Verdict words are used in the project's own sense: VERIFIED / PARTIALLY
VERIFIED / BLOCKED / NOT VERIFIED.

## Runtime and versions

| Component | Version | How established |
| --- | --- | --- |
| agent-server (frozen image `ghcr.io/openhands/agent-server`) | 1.60.1 | frozen runtime; **not modified, not decompiled** |
| openhands-tools wheel | 1.60.1 | exact wheel read locally this session lineage |
| openhands-sdk wheel | 1.60.1 | downloaded from pypi.org files host and read locally this session lineage (requires-python >= 3.12; source read only, never installed) |
| Cloud (app) REST API | v1.x surface | `docs/OPENHANDS_INVENTORY.md`, live-verified 2026-11-12; **not re-verified today** (no credentials in this sandbox) |
| Direct transport (agent-server socket) | 1.60.1 | exercised through `tests/fake_runtime.py` over real sockets |

The live Cloud was not called this session; everything below the transport
line is verified against real sockets, not against production.

## The response chain (dispatch → events → final answer)

VERIFIED against the fake servers in this repository and reproduced in the
full suite (1915 passed / 49 skipped / 0 failed, 38m34s, 2026-11-15):

- dispatch binds a conversation to a run task; a lost worker's conversation
  is adopted; a superseded conversation is stopped; claims are leases;
- polling reads `execution_status`; an empty/absent status is **not** a
  verdict (the conversation is asked again next tick);
- on `finished`, `agent_final_response` is read; content equality of the
  final response is asserted through the API in the current suite.

## Terminal-output recovery (PR #42, tested this branch)

**Honest scope, source-verified against the exact 1.60.1 artifacts:**

- `openhands.tools.terminal.terminal_session` truncates command output
  with `maybe_truncate(command_output, 30_000)` and passes **no**
  `save_dir` — the raw, untrimmed stdout/stderr is discarded before the
  event exists. `MAX_CMD_OUTPUT_SIZE = 30_000`.
- `TerminalObservation.to_llm_content` applies
  `maybe_truncate(ret, 30000, save_dir=…, tool_prefix="terminal")`, where
  `ret` is the *rendered* text (already clipped at the session layer).
  Only near the cap is `terminal_output_<sha256(ret)[:8]>.txt` written,
  and it contains the **clipped rendering**, not raw output.
- There is no external API for the raw stdout/stderr. NEXUS therefore
  does **not** and cannot recover it on this agent-server version; the
  UI and docs say so explicitly.

What is implemented and tested (`tests/test_terminal_output.py`, 45 tests,
all passing; included in the green suite):

- list: every terminal observation of the run's conversations (≤ 25
  conversations, ≤ 3×100 event pages, ≤ 50 refs), with a 1-byte probe
  reporting `saved_output: available|missing|unknown|unsupported`;
- fetch: `GET …/terminal-outputs/{cid}/{event_id}` — whole or refused;
  **413 only** when > 8 MiB (`MAX_OUTPUT_BYTES`);
- download: raw bytes as `text/plain` attachment, length-capped read;
- the file path is recomputed from the event (`terminal_output_<sha256(ret)[:8]>.txt`),
  never taken from it; the save directory is lexically validated absolute
  POSIX (no `..`, backslash, NUL; strict components; ≤ 512 chars);
- the path never leaves the server process; the audit line
  `terminal_output.read` is written on a successful read;
- the `app` transport reports `unsupported` (501), never claims files;
  foreign runs/projects/conversations/event-ids are refused (404).

## Runtime limits chosen (all enforced and tested)

Read ≤ 8 MiB; probe = 1 byte; 25 conversations / 40 probes / 50 refs /
3 pages × 100 events; no output written to PostgreSQL; text delivered to
the API caller; audit line best-effort.

## Known blockers / not done

1. **BLOCKED (by upstream design, not by NEXUS):** recovering the raw,
   untrimmed terminal output of a command. The session layer discards it;
   no external API and no file contains it. Evidence: source citations
   above, read from the 1.60.1 wheels.
2. The saved file exists only near the clip case. For typical commands
   far below the cap, no file is ever written — `saved_output: missing`
   is the expected answer, not a defect.
3. Live Cloud re-verification of the file routes: NOT VERIFIED today
   (no credentials in this sandbox). The direct/local agent-server path
   is verified over real sockets against the fake.
4. Browser/mobile rendering of the outputs panel: NOT VERIFIED
   (Playwright/Chromium absent; studio static UI tests and
   `node --check` pass).
5. `tools/benchmark.py` does not yet measure the terminal-outputs list
   path; no number is claimed for it.

## Verdict

PARTIAL–VERIFIED overall: green suite + fake-socket end-to-end coverage
on the direct transport; raw-output recovery BLOCKED by upstream design;
live Cloud NOT VERIFIED this session.
