-- Lock down the tables that hold a sandbox ``session_api_key``.
--
-- ``conversation_routes`` and ``cloud_sandboxes`` (migration 0007) store a live
-- sandbox session key in a plaintext column. Unlike every ``nexus_*`` table they
-- had no RLS and no explicit grant revocation, so a Supabase ``anon`` /
-- ``authenticated`` role could read them through PostgREST and lift a key.
--
-- This migration closes that: RLS is enabled with no client policy (so only the
-- server's own service/database role, which bypasses RLS, can read), and the
-- default PUBLIC / anon / authenticated grants are revoked. The application is
-- unaffected: it connects as the database owner.
--
-- It is a schema/permission change only; no row is touched.
BEGIN;

ALTER TABLE conversation_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloud_sandboxes       ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON conversation_routes, cloud_sandboxes FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON conversation_routes, cloud_sandboxes FROM %I', r);
    END LOOP;
END $$;

COMMIT;
