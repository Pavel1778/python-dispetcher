-- NEXUS GitHub multi-account.
--
-- A project binds to one account; the PAT is stored encrypted (Fernet, key from
-- NEXUS_SECRET_KEY) and is never returned by the API or written to a log. The
-- account's identity is verified against GitHub before it is trusted.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_github_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Owner principal (the NEXUS user who added the account).
    principal text NOT NULL,
    label text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
    -- The GitHub login verified via GET /user when the account was added.
    login text NOT NULL CHECK (length(btrim(login)) BETWEEN 1 AND 100),
    -- Fernet ciphertext of the PAT. Never plaintext; never returned by the API.
    token_encrypted bytea NOT NULL,
    scopes text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'invalid', 'revoked')),
    last_checked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (principal, label)
);
CREATE INDEX IF NOT EXISTS idx_nexus_github_accounts_principal
    ON nexus_github_accounts(principal, created_at DESC);

-- A project binds to exactly one account (nullable: a project may have none).
ALTER TABLE nexus_projects ADD COLUMN IF NOT EXISTS github_account_id uuid
    REFERENCES nexus_github_accounts(id) ON DELETE SET NULL;

ALTER TABLE nexus_github_accounts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_github_accounts FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_github_accounts FROM %I', r);
    END LOOP;
END $$;
COMMIT;
