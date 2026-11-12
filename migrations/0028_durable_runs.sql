-- Durable autonomous runs: work cycles, waiting, stop signals, reports.
--
-- Until now a run ended for good on the first thing that went wrong often
-- enough: three failed attempts made a task `failed`, a result that did not
-- verify left its dependents waiting for ever and the run `blocked`, and the
-- run's own deadline made it `failed` while agents were still at work. A person
-- had to notice and press "resume", and when the attempts were used up even
-- that was refused.
--
-- This migration gives the scheduler what it needs to keep going by itself and
-- to stay governable while it does:
--
--   * a task state ``waiting`` - the task will be tried again at ``retry_at``,
--     for the reason in ``wait_reason``. Attempts are counted inside a *cycle*;
--     when a cycle's attempts are used up the task waits (exponential backoff)
--     and starts the next cycle with its attempts restored;
--   * a run ``policy``: ``durable`` (keeps going until it succeeds or is
--     cancelled; its iteration budget is per cycle and renews after a pause) or
--     ``bounded`` (the behaviour before this migration, kept for a caller that
--     names explicit budgets: when they are spent the run stops);
--   * the conversation a run was started from, so that its report can be
--     delivered there, and ``nexus_run_reports`` - one report per run, written
--     in the transaction that completes the run and delivered exactly once;
--   * ``hidden_at`` - a cancelled run leaves the interface and stays in the
--     database with everything it recorded;
--   * ``nexus_executor_stops`` - the stop signal owed to a conversation that
--     must not go on (its run was cancelled, or it ran out of time). It is
--     delivered until the executor confirms, across restarts; a branch is not
--     given to another task while a stop for it is still unconfirmed;
--   * ``nexus_task_executions`` - which executor ran which attempt of which
--     task, on what, and how it ended. Append-only history; an account that is
--     later removed leaves its label here.
--
-- Additive. No row is deleted or rewritten except as stated under "existing
-- rows" below.
BEGIN;

-- --------------------------------------------------------------------- tasks
-- The state list gains ``waiting``. The constraint is found by what it checks,
-- not by an assumed name.
DO $$
DECLARE name text;
BEGIN
    FOR name IN
        SELECT c.conname FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'nexus_run_tasks' AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%completed_unverified%'
          AND pg_get_constraintdef(c.oid) NOT LIKE '%waiting%'
    LOOP
        EXECUTE format('ALTER TABLE nexus_run_tasks DROP CONSTRAINT %I', name);
    END LOOP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'nexus_run_tasks' AND c.conname = 'nexus_run_tasks_state_check'
    ) THEN
        ALTER TABLE nexus_run_tasks ADD CONSTRAINT nexus_run_tasks_state_check
            CHECK (state IN ('pending', 'waiting', 'dispatched', 'running', 'verifying',
                             'completed_verified', 'completed_unverified', 'partial',
                             'failed', 'blocked'));
    END IF;
END $$;

-- The work cycle a task is in (0 = the first), when a waiting task may be
-- taken again, and why it waits.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS cycle integer NOT NULL DEFAULT 0
    CHECK (cycle >= 0);
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS retry_at timestamptz;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS wait_reason text;
-- Starts the service refused in a row (429). The wait grows with it; a start
-- that is taken resets it.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS refusals integer NOT NULL DEFAULT 0
    CHECK (refusals >= 0);
-- "Do not start a new conversation: say this in the one that finished." Set
-- when a result did not verify and the agent can still fix it where it is.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS resume_conversation_id text;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS resume_note text;
-- When the current conversation was started. ``updated_at`` used to stand for
-- this; it cannot any more, because a running task is now written to (its
-- progress is recorded).
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS started_at timestamptz;
-- The head of the task's branch when the current attempt began.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS base_sha text;
-- Signs of life: a value that changes while the conversation makes progress,
-- when it last changed, and when it was last looked at.
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS progress_marker text;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS progress_at timestamptz;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS progress_checked_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_waiting
    ON nexus_run_tasks(run_id, retry_at) WHERE state = 'waiting';

-- ---------------------------------------------------------------------- runs
-- Whether this is the first time the columns below are added: what is done to
-- the rows that already exist is done once, not every time this file is run.
CREATE TEMP TABLE IF NOT EXISTS nexus_0028_first_run ON COMMIT DROP AS
    SELECT NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'nexus_runs'
          AND column_name = 'policy') AS first;

ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS policy text NOT NULL DEFAULT 'bounded'
    CHECK (policy IN ('durable', 'bounded'));
-- The run's own cycle: a durable run that used its iteration budget pauses
-- until ``resume_at`` and continues with the budget restored.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS cycle integer NOT NULL DEFAULT 0
    CHECK (cycle >= 0);
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS resume_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS wait_reason text;
-- Every task start the run has made, across cycles (``iteration`` counts the
-- current cycle's).
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS total_iterations integer NOT NULL DEFAULT 0
    CHECK (total_iterations >= 0);
-- The chat the run was started from. Its report is delivered there.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS conversation_id uuid
    REFERENCES nexus_conversations(id) ON DELETE SET NULL;
-- Visibility, apart from history: a cancelled run is not listed, and nothing
-- about it is removed.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS cancelled_by text;
-- How many times in a row the final confirmation could not be made (GitHub
-- was unreachable); the pause before the next try grows with it.
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS confirm_attempts integer NOT NULL DEFAULT 0
    CHECK (confirm_attempts >= 0);

CREATE INDEX IF NOT EXISTS idx_nexus_runs_visible
    ON nexus_runs(project_id, created_at DESC, id) WHERE hidden_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nexus_runs_conversation
    ON nexus_runs(conversation_id) WHERE conversation_id IS NOT NULL;

-- ------------------------------------------------------------ existing rows
DO $$
BEGIN
    IF (SELECT first FROM nexus_0028_first_run) THEN
        -- A run that is still alive keeps going under the new rules: it was
        -- started from the chat, where nobody named a budget. Finished runs
        -- are left as they are.
        UPDATE nexus_runs SET policy = 'durable'
         WHERE state IN ('running', 'paused', 'waiting_for_approval', 'blocked');
        UPDATE nexus_runs SET total_iterations = iteration WHERE iteration > 0;
        -- The chat a run came from, where that is still known: the
        -- specification it was planned from, else the conversation that
        -- points at it.
        UPDATE nexus_runs r SET conversation_id = s.conversation_id
          FROM nexus_specs s
         WHERE s.run_id = r.id AND s.conversation_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM nexus_conversations c WHERE c.id = s.conversation_id);
        UPDATE nexus_runs r SET conversation_id = c.id
          FROM nexus_conversations c
         WHERE c.run_id = r.id AND r.conversation_id IS NULL;
        -- Runs cancelled before this migration leave the interface as well.
        UPDATE nexus_runs SET hidden_at = updated_at, cancelled_at = updated_at
         WHERE state = 'cancelled';
    END IF;
END $$;

-- ------------------------------------------------------------------- reports
-- One row per run, created in the transaction that completes it. ``state``:
--   pending       - written, not yet in the chat
--   delivered     - the message exists (``message_id``); never sent twice
--   undeliverable - there is no chat to deliver to (the run was not started
--                   from one, or the conversation is gone); the report stays
--                   readable through the API
CREATE TABLE IF NOT EXISTS nexus_run_reports (
    run_id uuid PRIMARY KEY REFERENCES nexus_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES nexus_conversations(id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'undeliverable')),
    -- What the report says, as text (Markdown) and as data.
    body text NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- ``success`` or ``success_with_notes``; a report is only ever written for
    -- a run whose every task verified.
    outcome text NOT NULL DEFAULT 'success',
    message_id uuid REFERENCES nexus_conversation_messages(id) ON DELETE SET NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_nexus_run_reports_pending
    ON nexus_run_reports(next_attempt_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_nexus_run_reports_conversation
    ON nexus_run_reports(conversation_id);

-- --------------------------------------------------------------- stop signals
-- A conversation that has to stop. ``state``:
--   requested - the signal is owed, or was sent and is not yet confirmed
--   confirmed - the executor reported the conversation stopped (or gone)
--   abandoned - it could not be confirmed after every attempt; said so, kept
CREATE TABLE IF NOT EXISTS nexus_executor_stops (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    task_id uuid NOT NULL REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    conversation_id text NOT NULL,
    openhands_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    repository text NOT NULL DEFAULT '',
    branch text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'confirmed', 'abandoned')),
    -- Whether the signal itself has been accepted by the executor at least once.
    signalled boolean NOT NULL DEFAULT false,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_expires timestamptz,
    last_error text,
    -- What the conversation said it was when the stop was confirmed.
    observed text,
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    UNIQUE (conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_nexus_executor_stops_due
    ON nexus_executor_stops(next_attempt_at) WHERE state = 'requested';
CREATE INDEX IF NOT EXISTS idx_nexus_executor_stops_branch
    ON nexus_executor_stops(repository, branch) WHERE state = 'requested';
CREATE INDEX IF NOT EXISTS idx_nexus_executor_stops_run ON nexus_executor_stops(run_id);

-- ----------------------------------------------------------- execution history
-- One row per conversation a task was given: a start, a continuation in the
-- same conversation, a hand-off to another executor, an adopted orphan.
CREATE TABLE IF NOT EXISTS nexus_task_executions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES nexus_runs(id) ON DELETE CASCADE,
    task_id uuid NOT NULL REFERENCES nexus_run_tasks(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    task_key text NOT NULL,
    cycle integer NOT NULL DEFAULT 0,
    attempt integer NOT NULL DEFAULT 0,
    kind text NOT NULL DEFAULT 'start'
        CHECK (kind IN ('start', 'continue', 'handoff', 'adopted')),
    openhands_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    -- The executor as it was named then; kept when the account is removed.
    -- Empty for the server's own OPENHANDS_API_KEY.
    executor_label text NOT NULL DEFAULT '',
    conversation_id text,
    repository text NOT NULL DEFAULT '',
    branch text NOT NULL DEFAULT '',
    base_sha text,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    -- How it ended: a verifier's verdict, or stopped / timed_out / stalled /
    -- vanished / superseded.
    outcome text,
    reason text,
    head_sha text
);
CREATE INDEX IF NOT EXISTS idx_nexus_task_executions_task
    ON nexus_task_executions(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_nexus_task_executions_run
    ON nexus_task_executions(run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_nexus_task_executions_account
    ON nexus_task_executions(openhands_account_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_nexus_task_executions_open
    ON nexus_task_executions(task_id) WHERE ended_at IS NULL;

-- Browser access through PostgREST is refused, as everywhere in this schema.
ALTER TABLE nexus_run_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_executor_stops ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_task_executions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_run_reports, nexus_executor_stops, nexus_task_executions FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_run_reports, nexus_executor_stops, '
                       'nexus_task_executions FROM %I', r);
    END LOOP;
END $$;
COMMIT;
