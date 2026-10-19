-- Durable Sr-start retry.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0005_sr_start_retry.sql
-- Idempotent: safe to re-run.
--
-- Starting the Sr conversation is a Cloud call that can fail transiently (429 on
-- a busy account, a 5xx, a network drop). Today a failure while starting Sr
-- leaves the workflow in 'queued' with no Sr conversation, and nothing retries
-- it: the background loop only services a workflow that already has an
-- sr_conversation_id, so the workflow stays queued forever.
--
-- These columns make the retry durable across a process restart, so a workflow
-- created while the account was rate limited is picked up again once the limit
-- clears instead of needing an operator to delete and recreate it.

BEGIN;

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS sr_attempt        integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sr_last_error     text,
    ADD COLUMN IF NOT EXISTS sr_next_attempt_at timestamptz;

-- A workflow with no Sr that has given up: the loop scans these.
CREATE INDEX IF NOT EXISTS idx_workflows_sr_pending
    ON workflows(sr_next_attempt_at)
    WHERE sr_conversation_id IS NULL;

COMMIT;
