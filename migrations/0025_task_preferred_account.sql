-- Manual executor switch: an operator can name the account a task must run on.
--
-- Additive. When an operator switches a running task to another OpenHands
-- account, the target is recorded here and the pool selects it on the next
-- dispatch, unless it is not usable. The scheduler clears it once the task is
-- dispatched, so the preference applies to exactly one hand-off.
BEGIN;

ALTER TABLE nexus_run_tasks
    ADD COLUMN IF NOT EXISTS preferred_account_id uuid
        REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL;

COMMIT;
