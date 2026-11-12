-- Autonomous lab worker registry and heartbeat.
--
-- The worker is a separate process (``python -m orchestration.lab_worker``). Its
-- liveness is not observable from a lease alone: a task with no lease is just
-- "nothing claimed right now", which is also true when the worker is dead. This
-- table records each worker that has run and when it last ticked, so the
-- diagnostics can say whether a worker is actually running instead of guessing.
--
-- Additive: a deployment that runs the loop inline (``NEXUS_LAB_INLINE=1``)
-- registers under a web-derived worker id, so the same heartbeat covers it.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_lab_workers (
    worker_id text PRIMARY KEY,
    -- 'worker' for the separate process, 'inline' for the web-server loop.
    kind text NOT NULL DEFAULT 'worker' CHECK (kind IN ('worker', 'inline')),
    state text NOT NULL DEFAULT 'running'
        CHECK (state IN ('running', 'stopping', 'stopped')),
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);
CREATE INDEX IF NOT EXISTS idx_nexus_lab_workers_heartbeat
    ON nexus_lab_workers(heartbeat_at DESC);

ALTER TABLE nexus_lab_workers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_lab_workers FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_lab_workers FROM %I', r);
    END LOOP;
END $$;
COMMIT;
