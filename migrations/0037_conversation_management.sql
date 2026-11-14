-- NEXUS: conversation management (pins, soft delete, generations).
--
-- Additive. A conversation gains a pin (with the time it was pinned, so the
-- pinned section keeps a stable manual order) and a soft-delete stamp (so a
-- delete is reversible for a retention window before it becomes final). A
-- generation row is one AI answer in progress: it is what "stop generation"
-- acts on, and what a late token of a cancelled answer is checked against.

ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS pinned_at timestamptz;
-- When the conversation was deleted. NULL means it is live. A row past its
-- retention window is purged by housekeeping, not by this column.
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- Pinned conversations first, newest pin first; then the rest by activity.
-- The list query orders by (pinned, pinned_at, updated_at), so this index
-- serves it without a sort.
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_pinned
    ON nexus_conversations(project_id, pinned DESC, pinned_at DESC NULLS LAST,
                           updated_at DESC, id);
-- The live list (deleted_at IS NULL) is the common one.
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_live
    ON nexus_conversations(project_id, deleted_at, updated_at DESC, id);

-- One AI answer in progress (or finished). ``state`` is the generation's own
-- state, separate from the conversation's: a cancelled generation leaves the
-- conversation idle and usable. The partial text a cancelled answer produced
-- is kept on the message row it was saved as (meta.cancelled), not here.
CREATE TABLE IF NOT EXISTS nexus_generations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES nexus_conversations(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'running'
        CHECK (state IN ('running', 'cancelled', 'finished', 'failed')),
    mode text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_nexus_generations_conversation
    ON nexus_generations(conversation_id, created_at DESC, id);
-- At most one running generation per conversation: a second answer for the
-- same chat is refused, which is what makes "stop" unambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_generations_running
    ON nexus_generations(conversation_id) WHERE state = 'running';

ALTER TABLE nexus_generations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_generations FROM PUBLIC;
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles
             WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format('REVOKE ALL ON nexus_generations FROM %I', r);
    END LOOP;
END $$;
