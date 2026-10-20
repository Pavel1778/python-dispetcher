-- Direct Cloud transport: durable routing from a conversation to its sandbox.
--
-- The Cloud V1 app-conversation endpoint (``POST /api/v1/app-conversations``)
-- provisions a sandbox and then asks that sandbox's agent-server to create the
-- conversation. That inner step can fail with a 500 for reasons outside this
-- application's control, which left every dispatch unable to start.
--
-- The direct transport provisions the sandbox itself
-- (``POST /api/v1/sandboxes``), which returns the sandbox's ``session_api_key``
-- and its exposed ``AGENT_SERVER`` URL, and then talks to that agent-server
-- directly. A conversation therefore lives on a specific sandbox, and every
-- later read (status, final response, events) must go back to that same sandbox.
--
-- ``conversation_routes`` records that binding. It is keyed by the conversation
-- id because Sr conversations live on ``workflows`` and Jr conversations on
-- ``tasks``; a single mapping table serves both.

CREATE TABLE IF NOT EXISTS conversation_routes (
    external_conversation_id text PRIMARY KEY,
    sandbox_id               text NOT NULL,
    agent_server_url         text NOT NULL,
    session_api_key          text NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now()
);

-- Sandboxes this application has started, so a grouped strategy can reuse a
-- running one instead of paying to provision another.
CREATE TABLE IF NOT EXISTS cloud_sandboxes (
    sandbox_id       text PRIMARY KEY,
    agent_server_url text NOT NULL,
    session_api_key  text NOT NULL,
    status           text NOT NULL DEFAULT 'RUNNING',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cloud_sandboxes_status
    ON cloud_sandboxes(status, updated_at DESC);

-- The direct transport provisions a sandbox before the Sr conversation exists,
-- so the workflow remembers its sandbox the moment it is known. A crash before
-- the conversation id is bound can then still find (and reuse or delete) the
-- sandbox instead of leaking it.
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS sr_sandbox_id text;
