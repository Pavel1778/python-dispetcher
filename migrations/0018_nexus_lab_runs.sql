-- NEXUS autonomous lab: goal lifecycle, runs, executable tasks, verification.
--
-- Additive. The legacy `workflows`/`tasks` tables are untouched: an autonomous
-- run is its own object so the two execution models cannot corrupt each other.
--
-- A **run** is one attempt to take a goal to completion. It carries the loop
-- budget (iterations, deadline) and the replanning state. A **run task** is one
-- unit of work with an explicit dependency list and a verification record.
-- Only a task whose dependencies are all `completed_verified` may be dispatched.
BEGIN;

-- ------------------------------------------------------------- goal lifecycle
-- Extend the goal state machine. The original 0009 CHECK pinned state='draft';
-- drop it and add the real lifecycle. `nexus_goals.state` is the goal's state;
-- a run is a separate row so a goal can be re-run.
ALTER TABLE nexus_goals DROP CONSTRAINT IF EXISTS nexus_goals_state_check;
ALTER TABLE nexus_goals ADD CONSTRAINT nexus_goals_state_check CHECK (state IN (
    'draft', 'analyzing', 'planning', 'ready', 'running', 'paused', 'verifying',
    'replanning', 'waiting_for_approval', 'blocked', 'completed', 'failed', 'cancelled'));
ALTER TABLE nexus_goals ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE nexus_goals ADD COLUMN IF NOT EXISTS active_run_id uuid;

-- ---------------------------------------------------------------------- runs
CREATE TABLE IF NOT EXISTS nexus_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    goal_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'running' CHECK (state IN (
        'running', 'paused', 'waiting_for_approval', 'blocked', 'completed',
        'failed', 'cancelled')),
    iteration integer NOT NULL DEFAULT 0 CHECK (iteration >= 0),
    max_iterations integer NOT NULL DEFAULT 50 CHECK (max_iterations > 0),
    max_attempts_per_task integer NOT NULL DEFAULT 3 CHECK (max_attempts_per_task > 0),
    deadline_at timestamptz,
    last_error text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_runs_project ON nexus_runs(project_id, created_at DESC, id);
-- At most one non-terminal run per goal: a second start is refused, not queued.
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_runs_active_goal
    ON nexus_runs(goal_id)
    WHERE state IN ('running', 'paused', 'waiting_for_approval', 'blocked');

-- --------------------------------------------------------------- run tasks
CREATE TABLE IF NOT EXISTS nexus_run_tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    task_key text NOT NULL CHECK (length(btrim(task_key)) BETWEEN 1 AND 64),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    acceptance jsonb NOT NULL DEFAULT '[]'::jsonb,
    dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
    executor text NOT NULL DEFAULT 'openhands',
    timeout_seconds integer NOT NULL DEFAULT 1800 CHECK (timeout_seconds > 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'dispatched', 'running', 'verifying',
        'completed_verified', 'completed_unverified', 'partial', 'failed',
        'blocked')),
    -- The OpenHands conversation bound to this task, so a restart reattaches
    -- instead of dispatching a second one.
    conversation_id text,
    repository text NOT NULL DEFAULT '',
    branch text NOT NULL DEFAULT '',
    verification jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    lease_expires timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, task_key)
);
CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_run ON nexus_run_tasks(run_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_state ON nexus_run_tasks(run_id, state);

-- --------------------------------------------------------------- notifications
CREATE TABLE IF NOT EXISTS nexus_notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid REFERENCES nexus_projects(id) ON DELETE CASCADE,
    kind text NOT NULL,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    body text NOT NULL DEFAULT '' CHECK (length(body) <= 4000),
    resource_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_nexus_notifications_project
    ON nexus_notifications(project_id, created_at DESC, id);

ALTER TABLE nexus_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_run_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_notifications ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_runs, nexus_run_tasks, nexus_notifications FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_runs, nexus_run_tasks, nexus_notifications FROM %I', r);
    END LOOP;
END $$;
COMMIT;
