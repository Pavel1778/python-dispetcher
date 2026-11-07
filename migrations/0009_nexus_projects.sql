-- Additive NEXUS project foundation. Legacy workflows/tasks remain unscoped and
-- are intentionally NOT attached to projects. Apply only after reviewing the
-- target database and migration history; the server may auto-apply migrations.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS nexus_project_members (
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    principal text NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, principal)
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_members_principal
    ON nexus_project_members(principal, project_id);

-- Goals are drafts only: creating one does not dispatch an agent or create a
-- legacy workflow. No LLM output is accepted as an execution plan here.
CREATE TABLE IF NOT EXISTS nexus_goals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 20000),
    state text NOT NULL DEFAULT 'draft' CHECK (state = 'draft'),
    created_by text NOT NULL,
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_nexus_goals_project ON nexus_goals(project_id, created_at DESC, id);

-- Project events are metadata, not an executor or QA audit trail.
CREATE TABLE IF NOT EXISTS nexus_project_events (
    seq bigserial PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    actor text NOT NULL,
    kind text NOT NULL,
    resource_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_events_project
    ON nexus_project_events(project_id, seq DESC);

-- No browser access through PostgREST: API authorization happens in backend.
-- Explicitly revoke Supabase default grants as well as enabling RLS with no
-- client policies. Service/database owners must be trusted separately.
ALTER TABLE nexus_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_project_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_project_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_projects, nexus_project_members, nexus_goals, nexus_project_events FROM PUBLIC;
REVOKE ALL ON SEQUENCE nexus_project_events_seq_seq FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_projects, nexus_project_members, nexus_goals, nexus_project_events FROM %I', r);
        EXECUTE format('REVOKE ALL ON SEQUENCE nexus_project_events_seq_seq FROM %I', r);
    END LOOP;
END $$;
COMMIT;
