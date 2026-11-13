-- Chat-first workflow: a conversation negotiates a plan before any work runs.
--
-- Additive and backward compatible. Every existing conversation keeps its mode
-- and its history; it is only given a workflow state, and the state of a
-- conversation that never started a run is ``DISCOVERY`` - the state the
-- dialogue opens in.
--
-- The state machine is the durable half of the requirement "NEXUS discusses
-- first and runs only on an explicit command": EXECUTING is reachable only
-- from AWAITING_APPROVAL, and only an explicit launch command in that state
-- starts a run (``orchestration/nexus_workflow.py`` enforces the transition;
-- this table is what makes it survive a restart).
--
--   DISCOVERY        -> discussion; nothing is planned or run
--   CLARIFICATION    -> requirements are being pinned down
--   PLANNING         -> a plan is being produced
--   AWAITING_APPROVAL-> a plan exists and is shown; waiting for "Запускай"
--   EXECUTING        -> the approved run is at work
--   REPORTING        -> the run ended; its report is being delivered
--   COMPLETED/CANCELLED/FAILED -> terminal
--
-- ``plan_hash`` is the version of the plan on offer; ``plan_approved_hash``
-- records the version a person actually approved, so a replayed or concurrent
-- approval cannot start a second run and a plan that changed must be approved
-- again.
BEGIN;

ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS workflow_state text NOT NULL DEFAULT 'DISCOVERY'
        CHECK (workflow_state IN (
            'DISCOVERY', 'CLARIFICATION', 'PLANNING', 'AWAITING_APPROVAL',
            'EXECUTING', 'REPORTING', 'COMPLETED', 'CANCELLED', 'FAILED'));
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS workflow_goal_id uuid
        REFERENCES nexus_goals(id) ON DELETE SET NULL;
-- The specification this conversation's plan was built from (when the plan
-- came from a run of a long text). NULL for a plan built in the chat itself.
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS workflow_spec_id uuid
        REFERENCES nexus_specs(id) ON DELETE SET NULL;
-- The SHA-256 of the plan the conversation is currently offering (its version).
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS plan_hash text;
-- The plan version a person approved. Equal to ``plan_hash`` once approved; a
-- later replan changes ``plan_hash`` and this no longer matches, so the new
-- version must be approved on its own.
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS plan_approved_hash text;
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS plan_approved_at timestamptz;
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS plan_approved_by text;
-- The plan itself, as the validated task list the run will be built from. It is
-- what the person saw, kept verbatim: a run started from a confirmation uses
-- exactly this plan, so what was approved is what runs. JSONB so a version's
-- hash can be computed over it.
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS workflow_plan jsonb NOT NULL DEFAULT '[]'::jsonb;

-- The ''Manual'' mode: discuss, propose a plan, run only on an explicit
-- confirmation. ``auto`` stays the default and keeps running understandable
-- work by itself; this only widens the set of legal modes.
ALTER TABLE nexus_conversations DROP CONSTRAINT IF EXISTS nexus_conversations_mode_check;
ALTER TABLE nexus_conversations ADD CONSTRAINT nexus_conversations_mode_check
    CHECK (mode IN ('ask', 'analysis', 'plan', 'code', 'autonomous', 'auto', 'manual'));

-- A conversation whose workflow is at work on a run is found by its run id;
-- the workflow state is read for the chat surface on every turn, so it is
-- indexed with the project.
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_workflow
    ON nexus_conversations(project_id, workflow_state);

-- A specification that has been planned but is waiting for a person to approve
-- the plan gets its own state: it is no longer "planning" (so the background
-- loop does not finish it and start a run), and it is not yet "planned" (which
-- means the run exists). ``plan_options`` carries the hold flag; the plan
-- itself is in the stages, so the run is built from exactly the plan shown.
ALTER TABLE nexus_specs DROP CONSTRAINT IF EXISTS nexus_specs_state_check;
ALTER TABLE nexus_specs ADD CONSTRAINT nexus_specs_state_check
    CHECK (state IN ('indexed', 'planning', 'planned', 'awaiting_approval', 'failed'));

COMMIT;

