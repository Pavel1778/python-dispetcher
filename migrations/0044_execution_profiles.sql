-- Desired model configuration; NOT a second OpenHands account or secret store.
-- Additive. Old binaries can read their existing tables without rewriting this one.
CREATE TABLE IF NOT EXISTS nexus_execution_profiles (
    scope text PRIMARY KEY,
    project_id uuid REFERENCES nexus_projects(id) ON DELETE CASCADE,
    task_id uuid REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    CHECK (scope = CASE WHEN task_id IS NOT NULL AND project_id IS NOT NULL THEN 'task:' || task_id::text
                        WHEN task_id IS NULL AND project_id IS NOT NULL THEN 'project:' || project_id::text
                        WHEN task_id IS NULL THEN 'deployment' ELSE '' END),
    desired jsonb NOT NULL CHECK (jsonb_typeof(desired)='object'),
    revision bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_sync timestamptz, observation jsonb NOT NULL DEFAULT '{"state":"saved"}'
);
ALTER TABLE nexus_execution_profiles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_execution_profiles FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_execution_profiles FROM %I', r);
    END LOOP;
END $$;
