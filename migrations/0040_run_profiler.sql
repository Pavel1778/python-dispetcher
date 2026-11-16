-- Phase 5: durable stage timestamps for the run-time profiler.
--
-- A run's total wall-clock time is the sum of a few stages: waiting to be
-- claimed, dispatching, OpenHands starting the conversation, the agent
-- working, verification, and time between tries. Before this, only
-- ``created_at``/``updated_at`` existed, so "why did this run take 24 hours"
-- could not be answered with a number.
--
-- Every column is nullable and additive: a run that predates this keeps its
-- ``created_at`` and reads as "not measured", never as a fabricated zero. Each
-- is written once (``COALESCE(col, now())``) at the natural lifecycle point, so
-- a re-claim or a retry never moves the milestone.

ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS queued_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS dispatch_started_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS executor_started_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS conversation_ready_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS prompt_sent_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS first_agent_event_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS last_progress_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS verification_started_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS verification_completed_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS completed_at timestamptz;

-- A run that is already running when this migration applies starts its clock
-- now, so a deployment is not left with every in-flight run unmeasured.
UPDATE nexus_runs SET queued_at = created_at WHERE queued_at IS NULL;
