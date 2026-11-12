-- Hold a branch while a switched executor's old conversation may still write.
--
-- ``0028_task_lease_fencing.sql`` made a stale worker unable to *record* work on
-- a task it had lost. This closes the other half: the old conversation itself.
--
-- A switch (an operator's, or a failover) starts a *new* conversation on another
-- account while the old one may still be running in its sandbox, editing the
-- same checkout. The switch stopped only unpolling it; ``stop_conversation`` was
-- called best-effort and its ``False`` return (the app transport cannot stop a
-- conversation, only pause a whole sandbox) was ignored. So the old writer kept
-- pushing to the branch beside the new one - two writers on one workspace, the
-- case the acceptance text forbids ("Не допускай параллельной записи в один
-- workspace без специальной координации").
--
-- ``abandoned`` is the list of conversations a task left behind
-- (``conversation_id``, ``account_id``, ``at``). The worker stops each for real
-- and drops it with ``mark_abandoned_stopped``; until then ``_BRANCH_FREE``
-- holds the task's repository+branch, so nobody starts a second writer there.
-- A repository-less task is never held.
--
-- Additive. An existing task has an empty list: nothing to stop, nothing held.
BEGIN;

ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS abandoned jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Finding the tasks of a run that still have something to stop must not scan
-- every task.
CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_abandoned
    ON nexus_run_tasks(run_id)
    WHERE abandoned <> '[]'::jsonb;

COMMIT;
