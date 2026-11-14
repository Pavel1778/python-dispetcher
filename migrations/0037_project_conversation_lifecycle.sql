-- Project and conversation lifecycle: archive, soft delete, pin.
--
-- Phase E/F of the NEXUS project/conversation management work. Before this,
-- a project could be created, renamed and read, but never archived or
-- removed from the list - there was no way to put one aside or retire it.
-- A conversation could already be archived and restored (0020), but had no
-- "pin" (keep at the top of the list) and no distinct "delete" from
-- "archive": the only existing DELETE route is, and remains, an alias for
-- archiving (see its own test, test_conversations.py::
-- test_rename_archive_and_mode_change) - a separate, more final "trash"
-- state is added here instead of changing what that route already does.
--
-- Both ``nexus_projects`` and ``nexus_conversations`` get the same shape:
--   archived_at / archived - reversible, hides from the default list, does
--     not stop anything that is already running, and does not block new
--     activity (matches the conversation archive's existing behaviour).
--   deleted_at - a step further: "trash". A deleted project refuses new
--     conversations/goals; a deleted conversation refuses new messages. Both
--     are blocked while they still have an active run (``nexus_runs.state``
--     in running/paused/waiting_for_approval/blocked) - the active run must
--     be cancelled first, so a working agent's state is never silently cut.
--     Restoring clears ``deleted_at``; nothing is ever hard-deleted by this
--     migration or the code that uses it, so there is no cascade to get
--     wrong and no orphan record can be created by it.
--
-- Additive only: existing rows get archived_at/deleted_at = NULL (already
-- active, already not archived, already not pinned) and need no backfill.
BEGIN;

ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_nexus_projects_lifecycle
    ON nexus_projects(deleted_at, archived_at);

ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_lifecycle
    ON nexus_conversations(project_id, deleted_at, pinned, updated_at DESC);

COMMIT;
