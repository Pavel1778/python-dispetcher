-- Full lifecycle for projects and conversations: archive, trash, restore, purge.
--
-- Until now a project could only be renamed and a conversation only archived
-- (its ``archived`` flag, which ``DELETE`` set). There was no way to put a
-- project out of the way, no way to delete one, and no way to remove a
-- conversation at all. This adds the two columns that make those operations
-- safe and reversible, without touching any existing row.
--
-- **Soft delete first.** ``deleted_at`` is the trash: the row stays, its data
-- stays, and ``restore`` brings it back. A soft-deleted project or
-- conversation is hidden from every list and every read by default. A purge
-- (``purge_project`` / ``purge_conversation``) is the separate, explicit,
-- irreversible step; the services refuse it while anything is still running.
--
-- ``archived_at`` is the archive: "out of the active list, kept in full". It is
-- distinct from the trash because restoring from one must not restore from the
-- other, and because a conversation already had a boolean ``archived`` - this
-- column is not used for conversations (they keep ``archived``); it is here for
-- projects, which had no archive of their own.
--
-- Additive: three columns with no default value (NULL = live), two indexes.
BEGIN;

ALTER TABLE nexus_projects ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE nexus_projects ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE nexus_conversations ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- The lists read "everything not archived and not deleted", newest first.
CREATE INDEX IF NOT EXISTS idx_nexus_projects_live
    ON nexus_projects(created_at DESC, id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_live
    ON nexus_conversations(project_id, updated_at DESC, id) WHERE deleted_at IS NULL;

COMMIT;
