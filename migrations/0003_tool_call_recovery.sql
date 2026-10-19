-- Durable recovery for Sr client-tool calls.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0003_tool_call_recovery.sql
-- Idempotent: safe to re-run.
--
-- ``sr_tool_calls`` was a one-shot claim: once a row existed the call was never
-- retried, so a crash between the claim and ``finish_tool_call`` lost the call
-- forever. These columns make the row a lease that can be recovered, and split
-- "executed" from "delivered" so a stored result is never stranded.
--
--   claimed_at   - when the row was last claimed; a stale claim is re-executed.
--   attempts     - how many times execution was attempted (bounded).
--   delivered_at - when the result was delivered back to Sr. A row can be
--                  finished but not delivered (crash before send-message); it is
--                  re-delivered, because losing the result stalls Sr forever.

BEGIN;

ALTER TABLE sr_tool_calls
    ADD COLUMN IF NOT EXISTS claimed_at   timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS attempts     integer     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS delivered_at timestamptz;

-- Recovery scans look only at unfinished or undelivered rows.
CREATE INDEX IF NOT EXISTS idx_sr_tool_calls_unfinished
    ON sr_tool_calls(claimed_at) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sr_tool_calls_undelivered
    ON sr_tool_calls(finished_at) WHERE delivered_at IS NULL;

COMMIT;
