-- A task's time limit: two hours instead of thirty minutes.
--
-- ``nexus_run_tasks.timeout_seconds`` is how long a task's conversation may run
-- before the scheduler gives the task up ("timed out ...; the conversation was
-- left running"). Its default, 1800, was never anyone's choice for a
-- particular task - nothing in the application set another value - and it was
-- not enough. On the first evening the scheduler ran on the live service, the
-- first task of two runs was still at work after thirty minutes. Each was
-- recorded as failed while its agent went on working, and the run stopped.
--
-- Giving up early is the expensive mistake: the work under way is thrown
-- away, and "resume" starts a second agent on the same branch beside the
-- first. A conversation that hangs is still caught, later; the run's own
-- deadline bounds the whole.
--
-- Every task that may still run and carries the old default is given the new
-- one: tasks waiting or at work now are not cut short, and a task that already
-- timed out gets the longer limit when its run is resumed. Only a task that
-- was verified keeps its row as it was - it never runs again.
-- ``updated_at`` is left alone on purpose: for a running task it is the
-- moment the conversation started, and the limit is counted from it.
BEGIN;

ALTER TABLE nexus_run_tasks ALTER COLUMN timeout_seconds SET DEFAULT 7200;

UPDATE nexus_run_tasks
   SET timeout_seconds = 7200
 WHERE timeout_seconds = 1800
   AND state <> 'completed_verified';

COMMIT;
