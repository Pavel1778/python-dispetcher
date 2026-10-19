-- Sr/Jr dispatcher schema for Supabase PostgreSQL.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql
-- Idempotent: safe to re-run.
--
-- The state machine (allowed transitions) is enforced in application code
-- (orchestration/control_plane.py::_ALLOWED) and mirrored here as a CHECK so a
-- bad writer cannot persist an impossible state.

BEGIN;

CREATE TABLE IF NOT EXISTS workflows (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    goal        text NOT NULL,
    state       text NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued','running','verifying','completed',
                                 'blocked','failed','cancelled')),
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS repositories (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider         text NOT NULL DEFAULT 'github'
                     CHECK (provider IN ('github','gitlab','bitbucket',
                                         'bitbucket_data_center','forgejo',
                                         'azure_devops')),
    full_name        text NOT NULL UNIQUE,      -- "owner/name"
    default_branch   text NOT NULL DEFAULT 'main',
    allowed_branches text[] NOT NULL DEFAULT ARRAY[]::text[],
    enabled          boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id      uuid REFERENCES workflows(id) ON DELETE CASCADE,
    parent_task_id   uuid REFERENCES tasks(id) ON DELETE SET NULL,
    goal             text NOT NULL,
    request          text NOT NULL DEFAULT '',
    acceptance       text NOT NULL DEFAULT '',
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued','assigned','running','verifying',
                                      'completed','blocked','failed','cancelled',
                                      'recovering','dead_letter')),
    priority         integer NOT NULL DEFAULT 0,
    dependencies     jsonb NOT NULL DEFAULT '[]'::jsonb,
    owner            text,
    lease_expires    timestamptz,
    attempt          integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    idempotency_key  text UNIQUE,
    requires_approval boolean NOT NULL DEFAULT false,
    approved         boolean,
    evidence         jsonb NOT NULL DEFAULT '[]'::jsonb,
    result           text,
    repository_id    uuid REFERENCES repositories(id) ON DELETE SET NULL,
    selected_repository text,                    -- "owner/name", denormalized
    selected_branch  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tasks_state      ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_owner      ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_lease      ON tasks(lease_expires);
CREATE INDEX IF NOT EXISTS idx_tasks_workflow   ON tasks(workflow_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent     ON tasks(parent_task_id);
-- Queue scan: pick the highest-priority ready task quickly.
CREATE INDEX IF NOT EXISTS idx_tasks_queue      ON tasks(priority DESC, created_at) WHERE state = 'queued';

CREATE TABLE IF NOT EXISTS conversations (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                  uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    external_conversation_id text UNIQUE,       -- app_conversation_id from Cloud
    external_start_task_id   text,              -- start task id, until READY
    status                   text NOT NULL DEFAULT 'starting',
    url                      text,
    sandbox_id               text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

-- One conversation per task keeps dispatch idempotent across restarts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_task ON conversations(task_id);
CREATE INDEX IF NOT EXISTS idx_conversations_ext       ON conversations(external_conversation_id);

CREATE TABLE IF NOT EXISTS task_events (
    seq         bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL,
    task_id     uuid REFERENCES tasks(id) ON DELETE CASCADE,
    workflow_id uuid REFERENCES workflows(id) ON DELETE CASCADE,
    session_id  text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_events_task     ON task_events(task_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_workflow ON task_events(workflow_id, seq DESC);

-- ------------------------------------------------------------------ RLS
-- Administrative writes go through the server component using the service
-- role, which bypasses RLS. Browser clients get read-only access.
ALTER TABLE workflows     ENABLE ROW LEVEL SECURITY;
ALTER TABLE repositories  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks         ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_events   ENABLE ROW LEVEL SECURITY;

-- Policies target the Supabase `authenticated` role. They are created only
-- when that role exists, so this migration also runs on a plain PostgreSQL.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        DROP POLICY IF EXISTS read_workflows ON workflows;
        CREATE POLICY read_workflows ON workflows FOR SELECT TO authenticated USING (true);
        DROP POLICY IF EXISTS read_repositories ON repositories;
        CREATE POLICY read_repositories ON repositories FOR SELECT TO authenticated USING (enabled);
        DROP POLICY IF EXISTS read_tasks ON tasks;
        CREATE POLICY read_tasks ON tasks FOR SELECT TO authenticated USING (true);
        DROP POLICY IF EXISTS read_conversations ON conversations;
        CREATE POLICY read_conversations ON conversations FOR SELECT TO authenticated USING (true);
        DROP POLICY IF EXISTS read_task_events ON task_events;
        CREATE POLICY read_task_events ON task_events FOR SELECT TO authenticated USING (true);
    END IF;
END $$;

COMMIT;
