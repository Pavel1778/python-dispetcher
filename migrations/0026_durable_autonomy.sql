-- Durable autonomy: retry cycles, automatic reports, cancel/hide, multi-executor
-- and multi-repository projects.
--
-- Additive and backward compatible. Nothing backfills a legacy row: a run
-- planned before this migration has ``retry_attempt=0`` and ``max_retry_cycles``
-- at its default, a project with no executor assignment keeps running under the
-- process-wide key exactly as before, and a repository linked before this
-- migration has no owning GitHub account (it is read with the server token).
--
-- The design keeps the existing entities and extends them; there is no second
-- project/account/repository model here.

BEGIN;

-- ------------------------------------------------------- run retry cycles
-- A run no longer stops for good when its tasks have used their attempts. It
-- enters ``retrying`` with an exponential backoff; after the wait the tasks are
-- given a new cycle of attempts (``nexus_run_tasks.cycle``), until
-- ``max_retry_cycles`` is spent. A cancelled run never retries. A run that has
-- spent its cycles (or a cause no retry can fix) is ``blocked`` for a person.
ALTER TABLE nexus_runs DROP CONSTRAINT IF EXISTS nexus_runs_state_check;
ALTER TABLE nexus_runs ADD CONSTRAINT nexus_runs_state_check CHECK (state IN (
    'running', 'paused', 'waiting_for_approval', 'blocked', 'retrying',
    'completed', 'failed', 'cancelled'));

ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_attempt integer NOT NULL DEFAULT 0;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS max_retry_cycles integer NOT NULL DEFAULT 5;
--: When the next retry cycle may start. NULL means "no retry scheduled".
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_at timestamptz;
--: Why the run is waiting to retry (the reason the last cycle stopped).
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_reason text;
--: Starts spent across every cycle. ``iteration`` is the budget of the current
--: cycle (a new cycle gets a fresh one); this is the lifetime count, so the
--: total work a run may do is bounded by max_iterations * max_retry_cycles.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS total_iterations integer NOT NULL DEFAULT 0;

-- -------------------------------------------------- frontend visibility
-- ``hidden_at`` separates "the operator stopped this" from "keep the history".
-- A cancelled run is hidden from the active lists but its rows are untouched.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
--: When the operator asked to cancel, so the executor can be signalled once.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz;
--: The chat the run was started from; the report is delivered there.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS source_conversation_id uuid
    REFERENCES nexus_conversations(id) ON DELETE SET NULL;

-- The retry/visibility predicates are read on every tick; index them.
CREATE INDEX IF NOT EXISTS idx_nexus_runs_retry
    ON nexus_runs(state, retry_at) WHERE state = 'retrying';
CREATE INDEX IF NOT EXISTS idx_nexus_runs_visible
    ON nexus_runs(project_id, created_at DESC) WHERE hidden_at IS NULL;

-- A task that is given a new cycle of attempts: history stays in ``evidence``,
-- the attempt counter restarts, and the cycle number rises so the operator can
-- see how many times it was retried. ``max_attempts`` is per cycle.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS cycle integer NOT NULL DEFAULT 0;

-- ------------------------------------------------- automatic run reports
-- One report per run (the UNIQUE on run_id is the idempotency key: a second
-- finish cannot write a second report). Delivery is durable: a report left
-- ``pending`` after a restart is delivered by the next pass; a delivery that
-- fails is retried with a backoff, and a report is never sent twice because the
-- status only moves forward from ``pending``.
CREATE TABLE IF NOT EXISTS nexus_run_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL UNIQUE REFERENCES nexus_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    -- The conversation to deliver to (the run's source chat).
    conversation_id uuid REFERENCES nexus_conversations(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    content text NOT NULL DEFAULT '' CHECK (length(content) <= 60000),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_nexus_run_reports_pending
    ON nexus_run_reports(status, next_attempt_at) WHERE status = 'pending';

-- ------------------------------------- project OpenHands executor assignment
-- A project may name one primary executor and any number of fallbacks, in
-- order. This is the project-scoped allowlist: with assignments present, the
-- pool for that project is restricted to them. With none, the previous
-- behaviour is kept (the project's single bound account, else the pool).
CREATE TABLE IF NOT EXISTS nexus_project_openhands (
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    account_id uuid NOT NULL REFERENCES nexus_openhands_accounts(id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'fallback' CHECK (role IN ('primary', 'fallback')),
    position integer NOT NULL DEFAULT 0 CHECK (position >= 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'system',
    PRIMARY KEY (project_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_openhands_project
    ON nexus_project_openhands(project_id, role, position);
-- At most one primary executor per project.
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_project_openhands_primary
    ON nexus_project_openhands(project_id) WHERE role = 'primary';

-- The account's API version, discovered on verify, so the UI can show
-- compatibility. A read-only cache of a probe; never a secret.
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS api_version text;

-- ------------------------------------------- multi-repository projects
-- A project already links many repositories (``nexus_project_repositories``,
-- 0014). Two things are added: which connected GitHub account owns each link,
-- and which repository is the project's primary one.
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS github_account_id uuid
        REFERENCES nexus_github_accounts(id) ON DELETE SET NULL;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS is_primary boolean NOT NULL DEFAULT false;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS label text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_nexus_project_repositories_account
    ON nexus_project_repositories(project_id, github_account_id);
-- At most one primary repository per project.
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_project_repositories_primary
    ON nexus_project_repositories(project_id) WHERE is_primary;

-- Project isolation, as everywhere else: no browser access through PostgREST.
ALTER TABLE nexus_run_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_project_openhands ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_run_reports, nexus_project_openhands FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_run_reports, nexus_project_openhands FROM %I', r);
    END LOOP;
END $$;

COMMIT;
