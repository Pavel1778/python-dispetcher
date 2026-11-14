-- Project lifecycle: archive and soft delete (trash).
--
-- Before this, a project could be created, renamed and read, but never put
-- aside or retired: the only way to stop seeing one was to leave it in the
-- list. A project now has the same two reversible states a conversation has
-- (migration 0037 gave conversations ``pinned``/``deleted_at``):
--
--   archived_at - reversible. The project drops out of the default list but
--     is otherwise untouched: its conversations, runs and repository links
--     are not affected and it can still be opened and worked in.
--   deleted_at  - the trash. A deleted project refuses *new* work (a new
--     conversation or goal), and is blocked from deletion while it has an
--     active run, so a working agent is never cut off silently. Restoring
--     clears the marker; nothing is ever hard-deleted, so there is no cascade
--     to get wrong and no orphan record can be created by it.
--
-- Additive only: existing rows get NULL (live, not archived) and need no
-- backfill.
BEGIN;

ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- The default list (live projects only) and the archive/trash views.
CREATE INDEX IF NOT EXISTS idx_nexus_projects_lifecycle
    ON nexus_projects(deleted_at, archived_at);

COMMIT;
