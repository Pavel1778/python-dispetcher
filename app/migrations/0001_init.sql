-- 0001_init.sql
-- Minimal durable schema for the OpenHands Cloud dispatcher.
-- Designed for PostgreSQL 13+ / Supabase. gen_random_uuid() is built in.

CREATE TABLE IF NOT EXISTS workflows (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL,
    status            TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    sr_task_id        UUID,
    plan_summary      TEXT,
    result_summary    TEXT,
    expected_jr_count INTEGER,
    tags              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows (status);

CREATE TABLE IF NOT EXISTS tasks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id         UUID NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    parent_task_id      UUID REFERENCES tasks (id) ON DELETE SET NULL,
    role                TEXT NOT NULL,
    status              TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL UNIQUE,
    title               TEXT,
    prompt              TEXT,
    start_task_id       TEXT,
    app_conversation_id TEXT,
    sandbox_id          TEXT,
    selected_repository TEXT,
    selected_branch     TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    last_error          TEXT,
    result_summary      TEXT,
    next_attempt_at     TIMESTAMPTZ,
    lease_owner         TEXT,
    lease_expires_at    TIMESTAMPTZ,
    tags                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks (workflow_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (status, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_tasks_conversation ON tasks (app_conversation_id);

CREATE TABLE IF NOT EXISTS task_events (
    id          BIGSERIAL PRIMARY KEY,
    workflow_id UUID REFERENCES workflows (id) ON DELETE CASCADE,
    task_id     UUID REFERENCES tasks (id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    message     TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_events_workflow ON task_events (workflow_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id, created_at);

-- Outbound notifications queue (Better Stack / webhooks wired later).
CREATE TABLE IF NOT EXISTS notifications (
    id          BIGSERIAL PRIMARY KEY,
    workflow_id UUID REFERENCES workflows (id) ON DELETE CASCADE,
    task_id     UUID REFERENCES tasks (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications (status, created_at);
