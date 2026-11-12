-- Chat-first intent routing: the conversation's mode is a default, not a
-- decision about each message.
--
-- Additive and backward compatible. Two new modes:
--   ``auto``     - the default: the Intent Router decides per message whether a
--                  turn is a chat answer, an analysis, or an autonomous run.
--   ``analysis`` - the person chose "read the real state and answer".
-- An existing conversation keeps its mode; only the set of legal values grows,
-- so no row is rewritten and no data is lost. The default for a *new*
-- conversation becomes ``auto`` so a question asked in it does not start a run.
BEGIN;

-- Widen the CHECK to the modes the router and the studio now use. The old
-- constraint named four modes; add ``analysis`` and ``auto``.
ALTER TABLE nexus_conversations DROP CONSTRAINT IF EXISTS nexus_conversations_mode_check;
ALTER TABLE nexus_conversations ADD CONSTRAINT nexus_conversations_mode_check
    CHECK (mode IN ('ask', 'analysis', 'plan', 'code', 'autonomous', 'auto'));

-- A new conversation is ``auto``: its first message is classified, so a
-- question is answered instead of launching an autonomous run. Existing rows
-- keep whatever mode they had.
ALTER TABLE nexus_conversations ALTER COLUMN mode SET DEFAULT 'auto';

COMMIT;
