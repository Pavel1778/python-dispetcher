-- OpenHands connections: what a check found, and which of them a project uses.
--
-- ``nexus_openhands_accounts`` (0020, 0022) already holds one row per OpenHands
-- connection - a name, an API URL and an encrypted key - and a project could
-- bind exactly one of them (``nexus_projects.openhands_account_id``). Two
-- things were missing:
--
--   * what the last check of a connection found besides "the key works": the
--     version the service reports, and whether it has the API this code calls;
--   * a project's own list of executors: one or several connections, in order.
--     The first is the primary, the rest are backups in the order they are
--     tried. A project that has a list uses only the connections on it; a
--     project without one keeps the older rule (the owner's account pool, or
--     the server's own key when the owner has stored no connection).
--
-- Additive. ``nexus_projects.openhands_account_id`` stays and always names the
-- project's primary executor, so everything that reads it keeps working.
BEGIN;

-- What the service said about itself at the last check. ``compatibility`` is
-- ``unknown`` until a check could read the service's API description.
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS api_version text;
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS compatibility text
    NOT NULL DEFAULT 'unknown'
    CHECK (compatibility IN ('unknown', 'compatible', 'incompatible'));
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS compatibility_detail text;
-- When the connection was last asked whether it is reachable, by anyone - a
-- person's check or the loop's own look at a connection it had written off.
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS health_checked_at timestamptz;

-- Whose sandbox a recorded sandbox is: the connection that started it, or ''
-- for the server's own key. The direct transport reuses a running sandbox, and
-- it used to take the most recent one whoever had started it - with two
-- connections, one asked the service about the other's sandbox, was told it
-- does not exist, and wrote it off for its owner too.
ALTER TABLE cloud_sandboxes ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_cloud_sandboxes_owner
    ON cloud_sandboxes(owner, status, updated_at DESC);

-- How a project spreads work over its executors:
--   failover   - the first usable one in order; a backup only while those
--                before it cannot take work, and back again when they can
--   distribute - the usable one with the fewest tasks at work
ALTER TABLE nexus_projects ADD COLUMN IF NOT EXISTS executor_strategy text
    NOT NULL DEFAULT 'failover'
    CHECK (executor_strategy IN ('failover', 'distribute'));

CREATE TABLE IF NOT EXISTS nexus_project_executors (
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    account_id uuid NOT NULL REFERENCES nexus_openhands_accounts(id) ON DELETE CASCADE,
    -- 0 is the primary executor; backups follow in the order they are tried.
    position integer NOT NULL CHECK (position >= 0),
    -- Switched off for this project only; the connection itself is untouched.
    enabled boolean NOT NULL DEFAULT true,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, account_id),
    UNIQUE (project_id, position)
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_executors_account
    ON nexus_project_executors(account_id);

-- A project that had one connection bound has it as its primary executor.
INSERT INTO nexus_project_executors(project_id, account_id, position, created_by)
SELECT p.id, p.openhands_account_id, 0, 'migration:0029'
  FROM nexus_projects p
 WHERE p.openhands_account_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM nexus_project_executors e WHERE e.project_id = p.id);

ALTER TABLE nexus_project_executors ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_project_executors FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_project_executors FROM %I', r);
    END LOOP;
END $$;
COMMIT;
