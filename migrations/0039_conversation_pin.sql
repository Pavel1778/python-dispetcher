-- Pin a conversation to the top of the chat list.
--
-- Two lines of work implemented the project/conversation lifecycle
-- independently. This branch's is `0038_lifecycle.sql` (archive, trash,
-- restore, purge on both projects and conversations); the other
-- (`arena/ab0d7665`, PR #38) put the same `deleted_at` columns in its own
-- `0037`. The `deleted_at`/`archived_at` columns are therefore already here
-- and are NOT repeated: this file adds only the one column the other line
-- had that this one did not - `pinned`.
--
-- A fresh number is used on purpose (the two lines both claimed `0037`); the
-- runner applies `*.sql` in lexical order and keys on the filename, so a new
-- file is what keeps an already-migrated database from re-running anything.
--
-- Additive: existing rows get `pinned = false` (no row moves) and need no
-- backfill.
BEGIN;

ALTER TABLE nexus_conversations
    ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;

-- The chat list reads "pinned first, then newest", scoped to a project and
-- still excluding the trash.
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_pinned
    ON nexus_conversations(project_id, pinned DESC, updated_at DESC, id)
    WHERE deleted_at IS NULL;

COMMIT;
