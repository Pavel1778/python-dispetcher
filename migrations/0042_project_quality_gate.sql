-- Per-project quality gate: whether the Self-QA checks run for a project.
--
-- The gate was a deployment-wide switch (NEXUS_SELFQA_GATE=1): either every
-- project's accepted commits were checked, or none was. Running a project's
-- checks clones its repository and runs commands inside it, which is a cost
-- and a trust decision about one project, so it belongs on the project.
--
-- The value is a mode, not a boolean, because the useful default is neither
-- "always" nor "never":
--
--   auto  run the checks only when the repository declares them itself
--         (`.nexus-qa.json`). A project that never opted in by declaring its
--         checks is not slowed down by a default lint+tests run it did not ask
--         for; a project that did declare them gets them checked. This is the
--         default, so declaring the file is all it takes to be gated.
--   on    always run: the declared checks, or the defaults when the repository
--         declares none. An explicit choice for a project whose checks NEXUS
--         should apply even without a declaration.
--   off   never run. An explicit opt-out, which also wins over `auto`.
--
-- Additive and idempotent: one column with a default and a CHECK. Existing
-- projects get `auto`, which is the behaviour the roadmap asks for; nothing is
-- backfilled with a value a row did not have.

ALTER TABLE nexus_projects
    ADD COLUMN IF NOT EXISTS quality_gate text NOT NULL DEFAULT 'auto';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'nexus_projects_quality_gate_check'
    ) THEN
        ALTER TABLE nexus_projects
            ADD CONSTRAINT nexus_projects_quality_gate_check
            CHECK (quality_gate IN ('auto', 'on', 'off'));
    END IF;
END $$;
