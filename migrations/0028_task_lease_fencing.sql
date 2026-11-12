-- Fencing tokens and ownership for run tasks: a stale worker cannot write.
--
-- The lab store already claimed a task atomically (``FOR UPDATE SKIP LOCKED``
-- plus a lease) and refused a second claim of the same task. What it did *not*
-- do was remember *who* claimed a task, or stop the previous holder from
-- writing once the task had been taken away from it. ``claim_task`` took a
-- ``worker_id`` and never stored it. That left three real races:
--
--   * a worker whose lease expired (or whose task was switched / reclaimed /
--     requeued) could still ``mark_running`` and bind a conversation id onto a
--     task another worker had already taken, so the next poll followed the
--     wrong conversation and the task looked started when it was not;
--   * the same worker could settle a re-dispatched task with an old verdict,
--     so a fresh attempt was recorded as the result of a dead one;
--   * nothing said which worker was answerable for a task, so a stuck task
--     could not be attributed.
--
-- Two additive columns fix this:
--
--   * ``lease_owner`` - the worker that holds the claim, or NULL when the task
--     is not claimed. Informational and for the diagnostics; never a secret.
--   * ``lease_token`` - a per-task counter, bumped every time the task is taken
--     away from its holder (reclaim, switch, resume-requeue, cancel/fail) and
--     on every fresh claim. A worker carries the token it was granted; every
--     mutation it makes is fenced on ``lease_token = <its token>``, so a write
--     from a holder that has lost the task matches no row and is a no-op.
--
-- This is the standard fencing-token pattern: the token is monotonic per task,
-- the compare happens in the same UPDATE as the write (in the database, not in
-- the caller), and an out-of-date token can never win. Existing rows default to
-- 0, which is a valid token; no backfill is needed.
BEGIN;

ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS lease_owner text;
ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS lease_token bigint NOT NULL DEFAULT 0
        CHECK (lease_token >= 0);

-- Which worker is answerable for a task, cheaply: the diagnostics and the
-- operator surface read this.
CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_lease_owner
    ON nexus_run_tasks(lease_owner) WHERE lease_owner IS NOT NULL;

COMMIT;
