-- Versioned project-scoped plans. This migration does not dispatch tasks or
-- convert legacy workflows. Backend validates the entire DAG before insertion.
BEGIN;
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_goals_id_project ON nexus_goals(id, project_id);

CREATE TABLE IF NOT EXISTS nexus_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    goal_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','superseded')),
    created_by text NOT NULL,
    idempotency_key text,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    approved_by text,
    FOREIGN KEY (goal_id, project_id) REFERENCES nexus_goals(id, project_id) ON DELETE CASCADE,
    UNIQUE (project_id, goal_id, version),
    UNIQUE (project_id, goal_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_plans_approved ON nexus_plans(goal_id) WHERE state='approved';
CREATE INDEX IF NOT EXISTS idx_nexus_plans_project_goal ON nexus_plans(project_id, goal_id, version DESC);

CREATE TABLE IF NOT EXISTS nexus_plan_tasks (
    plan_id uuid NOT NULL REFERENCES nexus_plans(id) ON DELETE CASCADE,
    task_key text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    title text NOT NULL,
    description text NOT NULL DEFAULT '',
    acceptance jsonb NOT NULL,
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    PRIMARY KEY (plan_id, task_key),
    UNIQUE (plan_id, position)
);
CREATE TABLE IF NOT EXISTS nexus_plan_dependencies (
    plan_id uuid NOT NULL,
    task_key text NOT NULL,
    depends_on text NOT NULL,
    PRIMARY KEY (plan_id, task_key, depends_on),
    FOREIGN KEY (plan_id, task_key) REFERENCES nexus_plan_tasks(plan_id, task_key) ON DELETE CASCADE,
    FOREIGN KEY (plan_id, depends_on) REFERENCES nexus_plan_tasks(plan_id, task_key) ON DELETE CASCADE,
    CHECK (task_key <> depends_on)
);
ALTER TABLE nexus_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_plan_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_plan_dependencies ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_plans, nexus_plan_tasks, nexus_plan_dependencies FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_plans, nexus_plan_tasks, nexus_plan_dependencies FROM %I', r);
    END LOOP;
END $$;
COMMIT;
