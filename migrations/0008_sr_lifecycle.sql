-- Sr lifecycle: a durable "Sr started at" timestamp.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0007_sr_lifecycle.sql
-- Idempotent: safe to re-run.
--
-- The workflow verifier needs to tell a freshly started Sr (which legitimately
-- has no children yet) from an Sr that has been sitting idle for a long time
-- without delegating anything. The latter is wedged -- a sandbox that never came
-- up, a start that never became READY -- and must be blocked for a human instead
-- of leaving the workflow non-terminal forever.
--
-- ``updated_at`` cannot be used for this: it moves on every event, so it says
-- nothing about when Sr actually started.

BEGIN;

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS sr_started_at timestamptz;

COMMIT;
