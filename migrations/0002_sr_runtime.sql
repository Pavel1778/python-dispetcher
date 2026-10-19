-- Sr runtime + client-tool bridge.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0002_sr_runtime.sql
-- Idempotent: safe to re-run.
--
-- OpenHands Cloud V1 conversations are created with ``client_tools`` specs. When
-- the Sr agent calls one, an ``ActionEvent`` appears in the conversation's event
-- stream and the agent receives only an acknowledgment observation. The bridge
-- polls that stream, executes the real operation, and delivers the result back
-- to Sr as a message.
--
-- The Sr conversation is tracked per workflow so a restart can find it and so a
-- workflow has exactly one Sr.

BEGIN;

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS sr_conversation_id text,
    ADD COLUMN IF NOT EXISTS sr_start_task_id   text,
    ADD COLUMN IF NOT EXISTS sr_status          text;

CREATE INDEX IF NOT EXISTS idx_workflows_sr ON workflows(sr_conversation_id);

-- One row per handled Sr client-tool call. ``event_id`` is the Cloud event id,
-- which is stable across polls, so a second poll cannot execute a call twice.
CREATE TABLE IF NOT EXISTS sr_tool_calls (
    event_id    text PRIMARY KEY,
    workflow_id uuid REFERENCES workflows(id) ON DELETE CASCADE,
    task_id     uuid REFERENCES tasks(id) ON DELETE SET NULL,
    name        text NOT NULL,
    args        jsonb NOT NULL DEFAULT '{}'::jsonb,
    result      jsonb,
    is_error    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_sr_tool_calls_wf ON sr_tool_calls(workflow_id, created_at DESC);

ALTER TABLE sr_tool_calls ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        DROP POLICY IF EXISTS read_sr_tool_calls ON sr_tool_calls;
        CREATE POLICY read_sr_tool_calls ON sr_tool_calls FOR SELECT TO authenticated USING (true);
    END IF;
END $$;

COMMIT;
