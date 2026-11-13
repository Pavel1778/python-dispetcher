-- The OpenHands conversations of work that is over are deleted.
--
-- Every attempt of every task starts a conversation in the OpenHands account
-- it runs on, and until now nothing ever removed one: a run stopped its
-- conversations and left them where they were. An account that had run this
-- application for a month held every conversation of that month, and a
-- sandbox for most of them.
--
-- ``nexus_conversation_cleanups`` is the list of conversations this
-- application has to delete, one row per conversation. A row is written by
-- the worker once the run the conversation belongs to has been over for the
-- retention period (``NEXUS_OPENHANDS_CLEANUP_HOURS``, a day unless set), and
-- is delivered - with retries, by whichever worker is running - until the
-- service confirms that the conversation is gone. The older Sr/Jr workflows
-- (the chat's "Код" mode) are covered the same way: a row with ``workflow_id``
-- is the Sr conversation of a workflow that is completed or cancelled, or the
-- conversation of one of its tasks. ``state``:
--   requested - the deletion is owed, or was tried and is not yet confirmed
--   deleted   - the service deleted it and no longer reports it
--   missing   - it was not there any more when the deletion came for it
--   abandoned - it could not be deleted after every attempt; says why, kept
--
-- A row is the record that the deletion happened, so it is never removed:
-- ``ON DELETE SET NULL`` keeps it when its run, project or connection goes.
-- ``executor_label`` is the connection's name as it was (empty for the
-- server's own key): a row that names a connection and has lost it cannot be
-- delivered with another key, and says so.
--
-- Additive: one new table, no existing row is rewritten.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_conversation_cleanups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id text NOT NULL,
    run_id uuid REFERENCES nexus_runs(id) ON DELETE SET NULL,
    task_id uuid REFERENCES nexus_run_tasks(id) ON DELETE SET NULL,
    workflow_id uuid REFERENCES workflows(id) ON DELETE SET NULL,
    project_id uuid REFERENCES nexus_projects(id) ON DELETE SET NULL,
    openhands_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    executor_label text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'deleted', 'missing', 'abandoned')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_expires timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_nexus_conversation_cleanups_due
    ON nexus_conversation_cleanups(next_attempt_at) WHERE state = 'requested';
CREATE INDEX IF NOT EXISTS idx_nexus_conversation_cleanups_run
    ON nexus_conversation_cleanups(run_id);
CREATE INDEX IF NOT EXISTS idx_nexus_conversation_cleanups_workflow
    ON nexus_conversation_cleanups(workflow_id) WHERE workflow_id IS NOT NULL;

-- Browser access through PostgREST is refused, as everywhere in this schema.
ALTER TABLE nexus_conversation_cleanups ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_conversation_cleanups FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_conversation_cleanups FROM %I', r);
    END LOOP;
END $$;

COMMIT;
