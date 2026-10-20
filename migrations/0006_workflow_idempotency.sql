-- Idempotent workflow creation + atomic Sr start.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0006_workflow_idempotency.sql
-- Idempotent: safe to re-run.
--
-- Two defects made one user request able to create several Sr conversations.
--
-- 1. ``App.start_workflow`` minted a fresh workflow id on every call and then
--    started a new Sr conversation for it. A retried request (client timeout,
--    double submit, proxy retry) therefore produced a *second* workflow with a
--    *second* Sr and a *second* Jr -- observed live as three identical
--    workflows ~63s apart. ``tasks`` already had an ``idempotency_key``; the
--    workflow path had no equivalent, so this adds one with a UNIQUE
--    constraint, making the retry a no-op the database itself enforces.
--
-- 2. ``SrRuntime._retry_sr_starts`` selected pending workflows without locking
--    them, so two concurrent ticks could both start Sr for the same workflow.
--    ``tasks`` solved this with ``FOR UPDATE SKIP LOCKED`` in ``claim_task``;
--    ``sr_start_lease_expires`` gives the workflow path the same protection.
--
-- Nothing is deleted or rewritten: existing workflows keep a NULL
-- ``idempotency_key`` (multiple NULLs are allowed by a UNIQUE constraint), so
-- the migration is safe on live data.

BEGIN;

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS sr_start_lease_expires timestamptz,
    ADD COLUMN IF NOT EXISTS sr_start_owner text;

-- One workflow per logical request. Partial so the historical rows (NULL) are
-- unaffected and so only real keys are constrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflows_idempotency
    ON workflows(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- The retry scan filters on "no Sr yet"; a live start lease must be visible to
-- it, so index the two columns it reads together.
CREATE INDEX IF NOT EXISTS idx_workflows_sr_start_lease
    ON workflows(sr_start_lease_expires)
    WHERE sr_conversation_id IS NULL;

COMMIT;
