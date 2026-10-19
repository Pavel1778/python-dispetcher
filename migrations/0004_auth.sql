-- Authentication: server-side sessions and login rate limiting.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f migrations/0004_auth.sql
-- Idempotent: safe to re-run.
--
-- Sessions are stored server-side rather than in a signed cookie so that logout
-- and "revoke everything" are immediate and reliable: deleting the row ends the
-- session. Only the HMAC of the session token is stored, so a database leak does
-- not yield usable cookies (the HMAC key is AUTH_SESSION_SECRET, held only in the
-- environment).

BEGIN;

CREATE TABLE IF NOT EXISTS sessions (
    -- HMAC-SHA256(AUTH_SESSION_SECRET, token). The raw token never reaches the DB.
    token_hash  text PRIMARY KEY,
    username    text NOT NULL,
    -- Double-submit CSRF token; compared against the X-CSRF-Token header.
    csrf_token  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- Failed-login counters, keyed by a hash of username and client IP. Storing a
-- hash keeps the table free of readable usernames and IP addresses.
CREATE TABLE IF NOT EXISTS login_attempts (
    key          text PRIMARY KEY,
    failures     integer NOT NULL DEFAULT 0,
    window_start timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz
);

ALTER TABLE sessions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;

COMMIT;
