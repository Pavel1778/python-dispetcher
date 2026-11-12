-- Idempotent report delivery under several workers.
--
-- A run report must reach its chat exactly once. The delivery used to read the
-- pending rows and mark each ``sent`` only after posting, so two workers (the
-- web server's inline lab loop and a standalone ``lab_worker``, which the docs
-- say may run side by side) could read the same row and both post it: the chat
-- received the report twice.
--
-- The fix claims each report atomically (``FOR UPDATE SKIP LOCKED``) and moves
-- it to a transient ``sending`` state in the same transaction, so exactly one
-- worker posts it. A worker that dies while ``sending`` leaves the row to be
-- reclaimed after a short while.
--
-- Additive: only the set of legal ``status`` values grows. No row is rewritten.
BEGIN;

ALTER TABLE nexus_run_reports DROP CONSTRAINT IF EXISTS nexus_run_reports_status_check;
ALTER TABLE nexus_run_reports ADD CONSTRAINT nexus_run_reports_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed'));

-- The claim reads due rows by status and time; index the transient state too.
CREATE INDEX IF NOT EXISTS idx_nexus_run_reports_sending
    ON nexus_run_reports(updated_at) WHERE status = 'sending';

COMMIT;
