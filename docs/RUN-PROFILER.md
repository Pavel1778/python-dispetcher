# Run-time profile: where a run's wall-clock time goes

A run's total time is the sum of a few stages. Before this, only `created_at`
and `updated_at` existed, so "why did this run take hours" had no number. The
profile answers it from **durable** milestones on the run row.

## The milestones (migration `0040_run_profiler.sql`)

`nexus_runs` gains one nullable timestamp per milestone, in the order they
happen:

| column | stamped when |
|---|---|
| `queued_at` | the run is created (`start_run`) |
| `claimed_at` | a worker claims its first task (`claim_task`) |
| `dispatch_started_at` | a conversation is asked for (`LabScheduler._start`) |
| `conversation_ready_at` | the conversation is bound (`_bind`) |
| `prompt_sent_at` | the first message is on its way (`_bind`) |
| `first_agent_event_at` | a poll first sees the agent at work (`_collect`) |
| `last_progress_at` | reserved for the no-progress detector |
| `verification_started_at` / `verification_completed_at` | around the verifier |
| `completed_at` | the run reaches a terminal state |

Each is written **once**: `record_run_milestone` uses
`SET col = COALESCE(col, now())`, so a retry, a re-claim or a second worker
never moves a milestone. A name outside the known set is refused, so the writer
is not a way to change an arbitrary column.

## The stages (`orchestration/nexus_profiler.py`)

`profile_run(row, now=…)` returns, in seconds:

- `queue_wait`, `dispatch`, `openhands_start`, `prompt`, `first_event`,
  `execution`, `verification`, `finish`;
- `idle_seconds` = wall-clock minus the measured stages, floored at zero;
- `bottleneck` = the largest measured stage (or `idle`);
- `total_seconds`, `started_at`, `ended_at`.

`idle_ratio(profile)` is the idle fraction.

**Honesty rules.** A stage whose start or end is missing is `None`, never `0`
("not measured" is not "took no time"). The module is pure (no I/O, no clock
except the `now` a caller passes), so the API, the studio and the tests compute
the same numbers.

## Surface

- `GET /api/v1/projects/{id}/runs/{run_id}` returns a `profile` object
  (`{...stages, idle_seconds, idle_ratio, bottleneck}`).
- The studio's run card has a **«Профиль времени»** section listing the measured
  stages and the dominant one, and the idle share in the summary line.
- `LabStore.run_profile(run_id)` is the store-side read.

## What was measured on real data (Stage D)

The local OpenHands agent-server held 37 real conversations (chat transcripts,
not autonomous runs). The longest spanned **86 362 s (24 h)** with ~850 actions;
in the longest one **88 % of the time was in gaps longer than 60 s**, the two
largest being 8 h and 3.7 h between turns. That is the shape the profiler makes
visible: a run's wall-clock time is dominated by time *between* measured
stages, which the profile reports as `idle`, not hidden inside a stage.

No production run history was available to profile: the Supabase project is
**INACTIVE** and the local `nexus_phase3` history is synthetic (test runs).
See the Phase-5 final report, "Historical run analysis".

## Tests

`tests/test_run_profiler.py` (8: the pure cases - every stage, a missing
milestone as `None`, idle, a live run to `now`, the bottleneck, the milestone
set - and one integration case through the store) and the run-detail HTTP case
in `tests/test_lab_http.py`.
