-- OpenHands account pool: a durable error history per account.
--
-- Additive. ``0026`` gave an account its limits, a masked key hint and the
-- connection state. The pool still kept only the *latest* failure in
-- ``last_error``; the operator surface the task requires ("ошибки" — the
-- account's errors) needs the history, not just the last line.
--
--   * ``error_count``                    - the running total of observed
--                                          failures, so a flapping account can be
--                                          ranked without reading the history.
--   * ``nexus_openhands_account_events`` - one row per observed failure or
--                                          recovery, so "ошибки" is a list.
--
-- ``kind`` is 'auth' (a rejected key), 'transport' (unreachable) or 'error';
-- 'recovered' marks the account producing a result again. ``detail`` is the
-- message, which is never a secret.
--
-- No backfill: an existing account keeps an ``error_count`` of 0 and an empty
-- history; the next real failure or success starts it.
BEGIN;

ALTER TABLE nexus_openhands_accounts
    ADD COLUMN IF NOT EXISTS error_count integer NOT NULL DEFAULT 0
        CHECK (error_count >= 0);

CREATE TABLE IF NOT EXISTS nexus_openhands_account_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES nexus_openhands_accounts(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('auth', 'transport', 'error', 'recovered')),
    detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_openhands_account_events_account
    ON nexus_openhands_account_events(account_id, created_at DESC, id);

ALTER TABLE nexus_openhands_account_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_openhands_account_events FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_openhands_account_events FROM %I', r);
    END LOOP;
END $$;
COMMIT;
