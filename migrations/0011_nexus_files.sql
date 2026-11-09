-- Project-scoped uploads, development workspaces, guarded actions and voice
-- notes.
--
-- This file unifies the two independent "project file" designs that existed on
-- separate branches (main's opaque uploads and the File Lab branch). The table
-- is a superset: a row may carry main's columns (``name``/``content``/
-- ``created_by``) or the File Lab's (``filename``/``data``/``uploaded_by``/
-- ``content_type``/``is_archive``). Both services are supported; neither set is
-- mandatory, so either writer works.
--
-- Additive. Nothing here is attached to legacy workflows/tasks, and nothing
-- here starts an agent. Uploaded bytes live in PostgreSQL so that a restart
-- keeps them and so that no upload ever lands on the server's own filesystem.
-- Archives are extracted by backend code into `nexus_workspace_files` rows
-- after the checks in orchestration/archive_safety.py; no archive member name
-- is ever used as a filesystem path.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    -- main's opaque-upload columns
    name text,
    content bytea,
    created_by text,
    -- File Lab columns
    filename text,
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    data bytea,
    is_archive boolean NOT NULL DEFAULT false,
    uploaded_by text,
    -- shared
    size bigint NOT NULL CHECK (size >= 0),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- At least one payload column must be present and match the declared size.
    CONSTRAINT nexus_files_payload CHECK (
        (content IS NOT NULL AND octet_length(content) = size)
        OR (data IS NOT NULL AND octet_length(data) = size)
    ),
    CONSTRAINT nexus_files_owner CHECK (created_by IS NOT NULL OR uploaded_by IS NOT NULL)
);

-- A warm database created by the earlier (main) 0011 has ``name``/``content``/
-- ``created_by`` NOT NULL and a narrower ``size``; relax those so a File Lab row
-- can be written. On a fresh database these are already no-ops.
ALTER TABLE nexus_files ADD COLUMN IF NOT EXISTS filename text;
ALTER TABLE nexus_files ADD COLUMN IF NOT EXISTS content_type text NOT NULL DEFAULT 'application/octet-stream';
ALTER TABLE nexus_files ADD COLUMN IF NOT EXISTS data bytea;
ALTER TABLE nexus_files ADD COLUMN IF NOT EXISTS is_archive boolean NOT NULL DEFAULT false;
ALTER TABLE nexus_files ADD COLUMN IF NOT EXISTS uploaded_by text;
ALTER TABLE nexus_files ALTER COLUMN name DROP NOT NULL;
ALTER TABLE nexus_files ALTER COLUMN content DROP NOT NULL;
ALTER TABLE nexus_files ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE nexus_files ALTER COLUMN size TYPE bigint;
ALTER TABLE nexus_files DROP CONSTRAINT IF EXISTS nexus_files_size_check;

CREATE INDEX IF NOT EXISTS idx_nexus_files_project
    ON nexus_files(project_id, created_at DESC, id);

-- A development workspace: a durable, project-scoped file tree an agent or a
-- human can inspect. It is NOT a shell and it has no access to the host.
CREATE TABLE IF NOT EXISTS nexus_workspaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    source_file_id uuid REFERENCES nexus_files(id) ON DELETE SET NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_workspaces_project
    ON nexus_workspaces(project_id, created_at DESC, id);

-- One file inside a workspace. `original_data` is the content as imported, kept
-- so the UI can show a real diff after an edit; it is NULL for a file created
-- in place. `path` is a normalized relative path with no `..` and no leading
-- slash (enforced by the backend, and re-checked here as a belt-and-braces).
CREATE TABLE IF NOT EXISTS nexus_workspace_files (
    workspace_id uuid NOT NULL REFERENCES nexus_workspaces(id) ON DELETE CASCADE,
    path text NOT NULL CHECK (
        length(path) BETWEEN 1 AND 1024
        AND path !~ '(^/|(^|/)\.\.(/|$)|//)'
    ),
    is_dir boolean NOT NULL DEFAULT false,
    size bigint NOT NULL CHECK (size >= 0),
    data bytea,
    original_data bytea,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, path)
);

-- A guarded action that needs explicit human confirmation before it happens.
-- Only the hash of the confirmation token is stored, so a database leak does
-- not let anyone confirm someone else's pending action.
CREATE TABLE IF NOT EXISTS nexus_pending_actions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid REFERENCES nexus_projects(id) ON DELETE CASCADE,
    actor text NOT NULL,
    action text NOT NULL,
    payload jsonb NOT NULL,
    token_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','cancelled','expired')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    decided_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_nexus_pending_actions_pending
    ON nexus_pending_actions(actor, status, expires_at);

-- A recorded voice note. Bytes are stored like any other upload; the transcript
-- is NULL until a transcription provider is configured, so the UI can be honest
-- about what was and was not recognized.
CREATE TABLE IF NOT EXISTS nexus_voice_notes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    content_type text NOT NULL,
    size bigint NOT NULL CHECK (size >= 0),
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    data bytea NOT NULL,
    transcript text,
    transcript_engine text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_voice_notes_project
    ON nexus_voice_notes(project_id, created_at DESC, id);

ALTER TABLE nexus_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_workspace_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_pending_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_voice_notes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_files, nexus_workspaces, nexus_workspace_files,
    nexus_pending_actions, nexus_voice_notes FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_files, nexus_workspaces, '
            'nexus_workspace_files, nexus_pending_actions, nexus_voice_notes FROM %I', r);
    END LOOP;
END $$;
COMMIT;
