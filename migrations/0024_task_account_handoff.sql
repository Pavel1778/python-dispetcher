-- Account-aware run tasks: which OpenHands account ran a task, and the
-- hand-off record when an executor is switched.
--
-- Additive. A run task now records the account it was dispatched to, so a
-- failover can tell which account to exclude and a poll can reattach to the
-- right sandbox. ``nexus_task_handoffs`` is the durable history of a switch:
-- why the old executor was abandoned, which account took over, and the context
-- that was passed across.
BEGIN;

ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS openhands_account_id uuid
        REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL;
-- The accounts already tried for this task, so a failover never loops back to a
-- failed account. A jsonb array of account ids, newest last.
ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS tried_accounts jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_account
    ON nexus_run_tasks(openhands_account_id) WHERE openhands_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS nexus_task_handoffs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    task_id uuid NOT NULL REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    from_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    to_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    reason text NOT NULL DEFAULT '',
    -- The context passed to the new executor (the same shape the hand-off
    -- message is built from). Never a secret.
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_task_handoffs_task
    ON nexus_task_handoffs(task_id, created_at DESC);

ALTER TABLE nexus_task_handoffs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_task_handoffs FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_task_handoffs FROM %I', r);
    END LOOP;
END $$;
COMMIT;
