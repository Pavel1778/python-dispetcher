-- Private worker state, not part of the task or conversation API. Nothing in
-- the journal is a credential, a prompt, or a copy of the provider request.
CREATE TABLE IF NOT EXISTS nexus_dispatch_intents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id uuid NOT NULL REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    lease_token bigint NOT NULL,
    repository text NOT NULL,
    branch text NOT NULL,
    state text NOT NULL DEFAULT 'prepared' CHECK (state IN
        ('prepared','sending','uncertain','accepted','identified','bound','rejected','stop_owed')),
    context jsonb NOT NULL DEFAULT '{}',
    start_id text,
    conversation_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, lease_token)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_recovery ON nexus_dispatch_intents(run_id,state)
    WHERE state IN ('prepared','sending','uncertain','accepted','identified');
CREATE INDEX IF NOT EXISTS idx_dispatch_branch ON nexus_dispatch_intents(repository,branch)
    WHERE state IN ('sending','uncertain','accepted','identified');

-- Only when signing is configured: the verdict and obligation to sign commit
-- together. The UUID is also the evidence row key if delivery must be replayed.
CREATE TABLE IF NOT EXISTS nexus_task_attestations_due (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    task_id uuid NOT NULL REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    lease_token bigint NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE(task_id, lease_token)
);
CREATE INDEX IF NOT EXISTS idx_task_attestations_run ON nexus_task_attestations_due(run_id);
ALTER TABLE nexus_dispatch_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_task_attestations_due ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS nexus_run_attestations_due (
    run_id uuid PRIMARY KEY REFERENCES nexus_runs(id) ON DELETE CASCADE
);
ALTER TABLE nexus_run_attestations_due ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_dispatch_intents, nexus_task_attestations_due, nexus_run_attestations_due FROM PUBLIC;
DO $$ DECLARE role_name text; BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_dispatch_intents, nexus_task_attestations_due, '
                       'nexus_run_attestations_due FROM %I', role_name);
    END LOOP;
END $$;
