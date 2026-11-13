-- A start that the executor's service has accepted is kept on the task until
-- its conversation exists.
--
-- Starting an OpenHands conversation has two phases: the service accepts the
-- start at once and names a *start task*; the conversation appears when the
-- sandbox is ready, about a minute later on the live service. The worker used
-- to hold that start task in memory only. A worker that was replaced inside
-- that minute - every deployment replaces it - took the start with it: the
-- conversation went to work with nobody following it, and the task stood
-- claimed until its lease ran out, fifteen minutes later, when the orphan was
-- found by its tags and adopted as a second attempt.
--
-- ``pending_start`` holds what is needed to finish such a start from another
-- process: the start task, the account that accepted it, the branch head the
-- attempt began from, how the task's text was delivered, and the fencing token
-- of the claim it belongs to. It is meaningful only while the task is
-- ``dispatched`` with no conversation and still carries that token; a new claim
-- clears it and binding the conversation clears it.
--
-- Additive: one nullable column, no row is rewritten.
BEGIN;

ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS pending_start jsonb;

COMMIT;
