-- What a task is *for*, and the honest "no changes" outcome.
--
-- Until now every accepted task was ``completed_verified``, and the rule that
-- earned it was "the head of the task's branch is a commit the agent named".
-- That rule is true of an agent that changed nothing and named the head it
-- found, so a task whose push was refused - or that did no work at all - could
-- be recorded as verified: the SHA of the current branch head does not, by
-- itself, prove that a new change appeared (see ``GitVerifier.verify``).
--
-- Two additions make the verdict honest without inventing a new failure for
-- work that is genuinely complete:
--
--   * ``nexus_run_tasks.kind`` says what the task is for. ``implementation``,
--     ``bugfix`` and ``documentation`` are expected to move the branch;
--     ``verification_only`` and ``research_audit`` are not. The planner sets
--     it; an old row defaults to ``implementation``.
--   * the task state ``completed_no_changes`` is the accepted outcome of a task
--     whose kind expects no change: the conversation finished, the branch is
--     where it was, and that is correct for this task. It is accepted like
--     ``completed_verified`` (it unblocks dependents and lets a run complete)
--     but it is a distinct state, so the report and the UI can say so instead
--     of claiming a change that never happened.
--
-- A change-requiring task whose branch did not move is *not* accepted: it stays
-- ``completed_unverified`` and is reworked. ``completed_no_changes`` is never
-- produced for it.
--
-- Additive: one column with a default and one widened CHECK. No row is rewritten
-- beyond the default and no data is lost.
BEGIN;

ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'implementation';
ALTER TABLE nexus_run_tasks DROP CONSTRAINT IF EXISTS nexus_run_tasks_kind_check;
ALTER TABLE nexus_run_tasks ADD CONSTRAINT nexus_run_tasks_kind_check CHECK (kind IN (
    'implementation', 'bugfix', 'documentation', 'verification_only', 'research_audit'));

-- Widen the state CHECK to accept ``completed_no_changes``. The old constraint
-- is found by what it checks, not by an assumed name, so this works whether it
-- is called ``nexus_run_tasks_state_check`` or something else.
DO $$
DECLARE name text;
BEGIN
    FOR name IN
        SELECT c.conname FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'nexus_run_tasks' AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%completed_unverified%'
          AND pg_get_constraintdef(c.oid) NOT LIKE '%completed_no_changes%'
    LOOP
        EXECUTE format('ALTER TABLE nexus_run_tasks DROP CONSTRAINT %I', name);
    END LOOP;
    ALTER TABLE nexus_run_tasks DROP CONSTRAINT IF EXISTS nexus_run_tasks_state_check;
    ALTER TABLE nexus_run_tasks ADD CONSTRAINT nexus_run_tasks_state_check
        CHECK (state IN ('pending', 'waiting', 'dispatched', 'running', 'verifying',
                         'completed_verified', 'completed_no_changes',
                         'completed_unverified', 'partial', 'failed', 'blocked'));
END $$;

COMMIT;
