-- OpenHands account pool: limits, a masked key hint and the connection state.
--
-- Additive. ``0022_openhands_account_pool.sql`` gave an account the fields a
-- pool routes on (enabled, priority, health, active_tasks). This adds the three
-- things the pool still could not express:
--
--   * ``max_concurrent_tasks``      - how many conversations the account may run
--                                     at once. NULL means "no limit". A selection
--                                     skips an account already at its limit, so a
--                                     bounded account is not over-committed.
--   * ``daily_conversation_limit``  - the account's own daily ceiling, recorded
--                                     for the operator and the report. NULL means
--                                     "not known"; the pool never guesses it.
--   * ``api_key_hint``              - a masked fragment of the key (first and
--                                     last characters), so the interface and the
--                                     logs can *show which key is stored* without
--                                     ever holding the key. The full key stays in
--                                     ``api_key_encrypted`` and is never returned.
--   * ``connection_ok`` / ``last_connection_at`` - the result of the last
--                                     connectivity probe, which is distinct from
--                                     an authorization check (an endpoint can be
--                                     reachable while the key is rejected, and the
--                                     other way round).
--
-- No backfill: an account with no limit behaves exactly as before (unlimited),
-- and an account stored before this migration keeps an empty hint until its key
-- is next rotated.
BEGIN;

ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS max_concurrent_tasks integer
        CHECK (max_concurrent_tasks IS NULL OR max_concurrent_tasks > 0);
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS daily_conversation_limit integer
        CHECK (daily_conversation_limit IS NULL OR daily_conversation_limit > 0);
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS api_key_hint text NOT NULL DEFAULT '';
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS connection_ok boolean;
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS last_connection_at timestamptz;

COMMIT;
