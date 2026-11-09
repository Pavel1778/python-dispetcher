-- NEXUS Project Context and unified Memory.
--
-- Additive. Nothing here dispatches work or attaches to legacy rows. Both
-- tables are project-scoped and carry provenance so an agent's claim is never
-- silently promoted to a verified fact.
--
--   nexus_project_context  - a versioned snapshot of what NEXUS observed about
--                            a project's repository at a specific revision.
--                            Refreshed only when the revision changes.
--   nexus_memory           - project-scoped (and optional global) facts with a
--                            provenance label, confidence and supersession.
BEGIN;

-- --------------------------------------------------------------- project context
CREATE TABLE IF NOT EXISTS nexus_project_context (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    -- The repository revision this snapshot describes. A refresh with the same
    -- revision is a no-op; a different revision supersedes the old snapshot.
    repository text NOT NULL DEFAULT '',
    revision text NOT NULL DEFAULT '',
    -- The observed summary, as a JSON object. Never a model's guess: every
    -- field is produced by reading the repository tree.
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'current'
        CHECK (state IN ('current', 'superseded')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, repository, revision)
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_context_project
    ON nexus_project_context(project_id, created_at DESC, id);

-- --------------------------------------------------------------- unified memory
CREATE TABLE IF NOT EXISTS nexus_memory (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Global memories (project_id NULL) hold operator preferences that apply to
    -- every project; project memories are isolated to one project.
    project_id uuid REFERENCES nexus_projects(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN (
        'fact', 'decision', 'constraint', 'completed_work', 'defect',
        'failed_approach', 'discovery', 'task', 'verification')),
    content text NOT NULL CHECK (length(btrim(content)) BETWEEN 1 AND 8000),
    -- Where the content came from. Only the two "verified" labels may satisfy a
    -- dependency; an agent's own report is never one of them.
    provenance text NOT NULL CHECK (provenance IN (
        'agent_reported', 'repository_observed', 'test_verified',
        'independently_verified')),
    -- 0..1, an honest confidence for a human, not a gate.
    confidence real NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    -- A stable key for de-duplication within a scope (e.g. a hash of the fact).
    dedupe_key text NOT NULL,
    source text NOT NULL DEFAULT '',
    superseded_by uuid REFERENCES nexus_memory(id) ON DELETE SET NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- One live row per (scope, dedupe_key); a repeat is an update, not a
    -- duplicate. A NULL project_id needs a partial unique index (below).
    UNIQUE (project_id, dedupe_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_memory_global_dedupe
    ON nexus_memory(dedupe_key) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_nexus_memory_project
    ON nexus_memory(project_id, created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_nexus_memory_kind
    ON nexus_memory(project_id, kind, created_at DESC);

-- RLS with no client policies, like every other nexus_* table.
ALTER TABLE nexus_project_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_memory ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_project_context, nexus_memory FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_project_context, nexus_memory FROM %I', r);
    END LOOP;
END $$;
COMMIT;
