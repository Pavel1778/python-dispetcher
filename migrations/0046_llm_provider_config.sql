-- Persistent LLM provider configuration, separate from the OpenHands API URL.
--
-- Three values the mission forbids conflating:
--   base_url      (existing) : the OpenHands *API* endpoint NEXUS talks to.
--   llm_base_url  (new)      : the LLM *provider* endpoint a sandbox's model
--                              reaches (an OpenAI-compatible URL, or NULL for
--                              the provider's default). Never a secret.
--   default_model (existing) : the model identifier.
--
-- Non-secret configuration is stored plainly so rotating the API key (which
-- lives encrypted in api_key_encrypted) never erases the model or the URL.
--
-- A reconciliation record per account tracks the last time NEXUS compared the
-- desired configuration with what OpenHands reports, and what it did. It is a
-- side record: a failed reconciliation must never fail a dispatch.
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS llm_base_url text,
    ADD COLUMN IF NOT EXISTS llm_provider text,
    ADD COLUMN IF NOT EXISTS config_synced_at timestamptz,
    ADD COLUMN IF NOT EXISTS config_sync_state text
        CHECK (config_sync_state IS NULL
               OR config_sync_state IN ('synced', 'drift', 'pending', 'blocked')),
    ADD COLUMN IF NOT EXISTS config_sync_error text;

-- A provider Base URL is a URL, not free text: reject a value that is not an
-- http(s) origin. NULL is "use the provider default" and is allowed.
ALTER TABLE nexus_openhands_accounts
    DROP CONSTRAINT IF EXISTS nexus_openhands_llm_base_url_scheme;
ALTER TABLE nexus_openhands_accounts
    ADD CONSTRAINT nexus_openhands_llm_base_url_scheme
    CHECK (llm_base_url IS NULL OR llm_base_url ~ '^https?://[^[:space:]]+$');

CREATE INDEX IF NOT EXISTS idx_nexus_openhands_sync_due
    ON nexus_openhands_accounts(config_synced_at)
    WHERE state = 'active' AND enabled;
