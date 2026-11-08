-- Project-scoped opaque uploads. Never execute, extract or expose to PostgREST.
BEGIN;
CREATE TABLE IF NOT EXISTS nexus_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 255),
    size integer NOT NULL CHECK (size BETWEEN 0 AND 5242880),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    content bytea NOT NULL CHECK (octet_length(content) = size),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_files_project ON nexus_files(project_id, created_at DESC, id);
ALTER TABLE nexus_files ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_files FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_files FROM %I', r);
    END LOOP;
END $$;
COMMIT;
