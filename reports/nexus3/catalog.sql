-- Inventory only: no row contents, connection strings or credentials. Compare
-- on the same PostgreSQL major (pg_get_* text is not version independent).
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH tables AS (
    SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
), inventory AS (
    SELECT t.relname, jsonb_build_object(
        'rls', t.relrowsecurity, 'force_rls', t.relforcerowsecurity,
        'columns', (SELECT count(*) FROM pg_attribute a WHERE a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped),
        'indexes', (SELECT count(*) FROM pg_index i WHERE i.indrelid=t.oid),
        'foreign_keys', (SELECT count(*) FROM pg_constraint k WHERE k.conrelid=t.oid AND k.contype='f'),
        'shape_sha256', encode(sha256(convert_to(jsonb_build_array(
            (SELECT jsonb_agg(jsonb_build_array(a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                        pg_get_expr(d.adbin, d.adrelid)) ORDER BY a.attnum)
             FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=t.oid AND d.adnum=a.attnum
             WHERE a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped),
            (SELECT jsonb_agg(pg_get_constraintdef(k.oid) ORDER BY k.conname) FROM pg_constraint k WHERE k.conrelid=t.oid),
            (SELECT jsonb_agg(pg_get_indexdef(i.indexrelid) ORDER BY i.indexrelid::regclass::text)
             FROM pg_index i WHERE i.indrelid=t.oid)
        )::text, 'UTF8')), 'hex')
    ) AS shape FROM tables t
)
SELECT jsonb_pretty(jsonb_build_object(
    'postgres', current_setting('server_version'),
    'migrations', (SELECT jsonb_object_agg(filename, checksum) FROM public.schema_migrations),
    'tables', (SELECT jsonb_object_agg(relname, shape) FROM inventory)
));
COMMIT;
