-- OpenHands account pool: enable/disable, priority, health and selection mode.
--
-- Additive. ``0020_nexus_ai_ide.sql`` created ``nexus_openhands_accounts`` (one
-- row per account, key encrypted) and let a project bind one. This adds what a
-- *pool* needs to route work across several accounts without touching the
-- existing columns or their meaning:
--
--   * ``enabled``        - an account an operator switched off is never selected
--                          (distinct from ``state='revoked'``, which also clears
--                          the default; disabling is reversible and keeps the key)
--   * ``priority``       - the failover order; lower is tried first
--   * ``health``         - the last observed health, never a secret
--   * ``active_tasks``   - how many conversations the account is running, for
--                          LOAD_BALANCING; maintained by the pool, not guessed
--   * ``last_error_at``  - when the account last failed a real call
--
--   * ``nexus_openhands_pool`` - one row per principal: the selection mode
--                          (MANUAL / AUTO / FAILOVER / LOAD_BALANCING) and the
--                          explicit fallback order an operator may pin.
--
-- No backfill: an account with no pool row behaves as ``enabled``, priority 100
-- and the ``auto`` mode, which is exactly the previous behaviour (use the
-- default account).
BEGIN;

ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100;
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS health text NOT NULL DEFAULT 'unknown'
        CHECK (health IN ('unknown', 'healthy', 'degraded', 'unavailable'));
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS active_tasks integer NOT NULL DEFAULT 0
        CHECK (active_tasks >= 0);
ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS last_error_at timestamptz;

-- The failover order, and the load-balancing tie-break, read this index.
CREATE INDEX IF NOT EXISTS idx_nexus_openhands_accounts_pool
    ON nexus_openhands_accounts(principal, priority, created_at, id)
    WHERE enabled AND state = 'active';

-- One pool configuration per principal. Absent means the defaults below.
CREATE TABLE IF NOT EXISTS nexus_openhands_pool (
    principal text PRIMARY KEY,
    -- manual         use the default account only
    -- auto           the default when healthy, else the highest-priority healthy
    -- failover       like auto, but the caller may exclude a failed account
    -- load_balancing the healthy account running the fewest active tasks
    selection_mode text NOT NULL DEFAULT 'auto'
        CHECK (selection_mode IN ('manual', 'auto', 'failover', 'load_balancing')),
    -- An operator-pinned order (a list of account ids). Empty means "by
    -- priority". Ids that no longer exist or are not usable are skipped.
    fallback_order jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE nexus_openhands_pool ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_openhands_pool FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_openhands_pool FROM %I', r);
    END LOOP;
END $$;
COMMIT;
