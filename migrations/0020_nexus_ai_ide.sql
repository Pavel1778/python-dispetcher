-- NEXUS AI IDE: OpenHands accounts, project binding, conversations and agents.
--
-- Additive and backward compatible. It reuses the existing project, repository,
-- account and orchestration models instead of duplicating them:
--   * GitHub accounts already live in ``nexus_github_accounts`` (0019).
--   * Repositories already live in the global ``repositories`` allowlist and are
--     linked to a project through ``nexus_project_repositories`` (0014).
--   * A task/run keeps its own record; a conversation here is the chat surface.
--
-- Nothing backfills legacy rows: a project with no binding stays unbound and a
-- conversation is only ever created by an explicit action.
BEGIN;

-- ---------------------------------------------------------- OpenHands accounts
-- One row per OpenHands account/profile. The API key is encrypted with the same
-- server-side key as a GitHub PAT (``NEXUS_SECRET_KEY``) and is never returned.
-- ``is_default`` is enforced to at most one row per principal by a partial
-- unique index, so "the default account" is unambiguous.
CREATE TABLE IF NOT EXISTS nexus_openhands_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    principal text NOT NULL,
    label text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
    -- The Cloud base URL this account talks to (a self-hosted or a Cloud URL).
    base_url text NOT NULL DEFAULT 'https://app.all-hands.dev',
    -- The transport for this account: "direct" provisions a sandbox and drives
    -- its agent-server; "app" uses the app-conversation endpoint. Mirrors
    -- OPENHANDS_TRANSPORT, per account.
    transport text NOT NULL DEFAULT 'direct' CHECK (transport IN ('direct', 'app')),
    -- Optional default model for a sandbox that has no key of its own.
    default_model text,
    -- Fernet ciphertext of the API key. Never plaintext; never returned.
    api_key_encrypted bytea NOT NULL,
    -- The verified identity (Cloud login/e-mail) when the account was checked.
    identity text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'invalid', 'revoked')),
    is_default boolean NOT NULL DEFAULT false,
    -- Discovered capabilities (models, MCP servers, marketplaces), as reported
    -- by the account. Never a secret; a cache of a read-only probe.
    capabilities jsonb,
    last_checked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (principal, label)
);
CREATE INDEX IF NOT EXISTS idx_nexus_openhands_accounts_principal
    ON nexus_openhands_accounts(principal, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_openhands_default
    ON nexus_openhands_accounts(principal) WHERE is_default;

-- ------------------------------------------------------- project binding
-- The project's execution binding. ``repository_id`` is the canonical link to
-- the allowlist row (never a typed owner/name); ``selected_branch`` is the
-- branch work runs on. Changing either is an explicit action, never a silent
-- switch, and the trigger below keeps the derived full name consistent.
ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS repository_id uuid REFERENCES repositories(id) ON DELETE SET NULL;
ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS selected_branch text;
ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS openhands_account_id uuid
        REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL;

-- ------------------------------------------------------------- conversations
-- A persistent conversation is scoped to one project (and therefore to that
-- project's bound repository/branch). ``mode`` is the chat mode; ``state``
-- tracks the assistant turn so a reload restores an in-flight answer.
CREATE TABLE IF NOT EXISTS nexus_conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    title text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    mode text NOT NULL DEFAULT 'ask' CHECK (mode IN ('ask', 'code', 'plan', 'autonomous')),
    state text NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'thinking', 'error')),
    archived boolean NOT NULL DEFAULT false,
    -- Optional link to the autonomous run this conversation initiated, if any.
    run_id uuid REFERENCES nexus_runs(id) ON DELETE SET NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_project
    ON nexus_conversations(project_id, updated_at DESC, id);
-- Search by title within a project (a simple substring search, not full text).
CREATE INDEX IF NOT EXISTS idx_nexus_conversations_title
    ON nexus_conversations(project_id, lower(title));

-- ---------------------------------------------------------------- messages
-- ``role`` distinguishes the human, the assistant and a tool event so the UI can
-- render them differently. ``content`` is the text; ``meta`` carries tool names,
-- run/task ids, verification results — never a secret.
CREATE TABLE IF NOT EXISTS nexus_conversation_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES nexus_conversations(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content text NOT NULL DEFAULT '',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_conversation_messages_conversation
    ON nexus_conversation_messages(conversation_id, created_at, id);

-- ------------------------------------------------------------- attachments
-- An attachment is a reference to a stored file (the File Lab / opaque store),
-- never a second copy of the bytes. ``file_id`` is nullable because an image may
-- be kept as its own small blob later; for now every attachment names a file.
CREATE TABLE IF NOT EXISTS nexus_conversation_attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES nexus_conversations(id) ON DELETE CASCADE,
    message_id uuid REFERENCES nexus_conversation_messages(id) ON DELETE CASCADE,
    file_id uuid REFERENCES nexus_files(id) ON DELETE SET NULL,
    kind text NOT NULL DEFAULT 'file' CHECK (kind IN ('file', 'image', 'archive', 'voice')),
    filename text NOT NULL DEFAULT '' CHECK (length(filename) <= 255),
    content_type text NOT NULL DEFAULT '',
    size bigint NOT NULL DEFAULT 0 CHECK (size >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_conversation_attachments_conversation
    ON nexus_conversation_attachments(conversation_id, created_at);

-- ------------------------------------------------------------ agent sessions
-- A NEXUS agent session is distinct from an OpenHands conversation. It records
-- which OpenHands account/conversation executes a task, so the two are never
-- confused and the mapping survives a restart.
CREATE TABLE IF NOT EXISTS nexus_agent_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES nexus_conversations(id) ON DELETE SET NULL,
    openhands_account_id uuid REFERENCES nexus_openhands_accounts(id) ON DELETE SET NULL,
    -- The real OpenHands conversation id, once one exists.
    external_conversation_id text,
    -- The NEXUS task/run this session executes, when it is not a chat turn.
    run_id uuid REFERENCES nexus_runs(id) ON DELETE SET NULL,
    run_task_id uuid REFERENCES nexus_run_tasks(id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'starting'
        CHECK (state IN ('starting', 'running', 'waiting', 'blocked', 'finished', 'error')),
    current_activity text NOT NULL DEFAULT '',
    repository text NOT NULL DEFAULT '',
    branch text NOT NULL DEFAULT '',
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_agent_sessions_project
    ON nexus_agent_sessions(project_id, created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_nexus_agent_sessions_conversation
    ON nexus_agent_sessions(conversation_id, created_at DESC);

-- Project isolation is enforced by the API; browser access through PostgREST is
-- refused outright, as everywhere else in this schema.
ALTER TABLE nexus_openhands_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_conversation_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_conversation_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_agent_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_openhands_accounts, nexus_conversations,
    nexus_conversation_messages, nexus_conversation_attachments,
    nexus_agent_sessions FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_openhands_accounts, nexus_conversations, '
                       'nexus_conversation_messages, nexus_conversation_attachments, '
                       'nexus_agent_sessions FROM %I', r);
    END LOOP;
END $$;
COMMIT;
