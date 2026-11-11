-- NEXUS large task documents ("specifications").
--
-- Until now the text of an autonomous goal lived only in ``nexus_goals.text``,
-- which is capped at 20 000 characters, and nothing of it reached the agent
-- except what a planner had summarised into a task title and description.
--
-- A specification is the task exactly as it was submitted - any size up to the
-- server's cap, no trimming, no normalisation - stored once, hashed, indexed
-- and split into stages. The goal keeps a short label and points at it; every
-- run task points at the part of it that the task covers; the executor hands
-- the agent the whole original (inside the first message when it is small, as a
-- verified file when it is not).
--
--   nexus_specs               the original text, its fingerprint, planning state
--   nexus_spec_sections       the outline: one row per heading-delimited section
--   nexus_spec_requirements   every addressable piece of text (list item,
--                             paragraph, table row, code block) with a stable id
--   nexus_spec_links          explicit references between parts ("см. раздел 3.2")
--   nexus_spec_stages         contiguous parts sized to a planner's context
--                             window; also the durable work queue of planning
--
-- Additive: no existing column or constraint changes meaning. ``nexus_goals.text``
-- keeps its 20 000 character limit; for a longer task it holds a label and the
-- text is in ``nexus_specs``.
BEGIN;

CREATE TABLE IF NOT EXISTS nexus_specs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    -- The chat the text was sent in, when it came from one. With ``sha256`` it
    -- makes a repeated send of the same text find the same row.
    conversation_id uuid REFERENCES nexus_conversations(id) ON DELETE SET NULL,
    content text NOT NULL,
    -- SHA-256 of the UTF-8 bytes of ``content``. The database checks it on
    -- every write, so a row cannot hold text that differs from its fingerprint.
    sha256 text NOT NULL,
    char_count integer NOT NULL,
    byte_count integer NOT NULL,
    line_count integer NOT NULL CHECK (line_count > 0),
    token_estimate integer NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
    section_count integer NOT NULL DEFAULT 0 CHECK (section_count >= 0),
    requirement_count integer NOT NULL DEFAULT 0 CHECK (requirement_count >= 0),
    link_count integer NOT NULL DEFAULT 0 CHECK (link_count >= 0),
    source text NOT NULL DEFAULT 'chat' CHECK (source IN ('chat', 'api')),
    -- indexed: stored and indexed, no plan asked for yet
    -- planning: stages exist and are being planned (see nexus_spec_stages)
    -- planned: every stage has a plan and the run was created
    -- failed: planning stopped; ``last_error`` says why. A repeated send retries.
    state text NOT NULL DEFAULT 'indexed'
        CHECK (state IN ('indexed', 'planning', 'planned', 'failed')),
    -- Tokens of specification text per stage that the current stages were cut for.
    stage_budget integer CHECK (stage_budget IS NULL OR stage_budget > 0),
    -- What the run is to be started with once the plan is complete: repository,
    -- branch and budgets. Kept here because planning may finish in another
    -- process than the one that received the request.
    plan_options jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id uuid REFERENCES nexus_runs(id) ON DELETE SET NULL,
    last_error text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT nexus_specs_fingerprint CHECK (
        sha256 = encode(sha256(convert_to(content, 'UTF8')), 'hex')),
    CONSTRAINT nexus_specs_sizes CHECK (
        char_count = char_length(content) AND char_count > 0
        AND byte_count = octet_length(convert_to(content, 'UTF8')))
);
CREATE INDEX IF NOT EXISTS idx_nexus_specs_project
    ON nexus_specs(project_id, created_at DESC, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_specs_conversation
    ON nexus_specs(conversation_id, sha256) WHERE conversation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nexus_specs_planning
    ON nexus_specs(created_at) WHERE state = 'planning';

-- Offsets are in characters (code points) of ``content``, half-open
-- [start_char, end_char). Lines are 1-based and inclusive, and a line ends at
-- "\n" only, so they are the numbers ``sed -n`` and ``cat -n`` show.
CREATE TABLE IF NOT EXISTS nexus_spec_sections (
    spec_id uuid NOT NULL REFERENCES nexus_specs(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    key text NOT NULL,
    level integer NOT NULL CHECK (level >= 0),
    number text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    parent integer,
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    token_estimate integer NOT NULL DEFAULT 0,
    PRIMARY KEY (spec_id, ordinal),
    CHECK (start_char >= 0 AND end_char > start_char AND end_line >= start_line)
);

CREATE TABLE IF NOT EXISTS nexus_spec_requirements (
    spec_id uuid NOT NULL REFERENCES nexus_specs(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    key text NOT NULL,
    section integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('item', 'paragraph', 'row', 'code')),
    -- Reads as an obligation ("должен", "must", an imperative, a list item).
    normative boolean NOT NULL DEFAULT false,
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    excerpt text NOT NULL DEFAULT '',
    PRIMARY KEY (spec_id, ordinal),
    CHECK (start_char >= 0 AND end_char > start_char AND end_line >= start_line)
);
CREATE INDEX IF NOT EXISTS idx_nexus_spec_requirements_section
    ON nexus_spec_requirements(spec_id, section, ordinal);

CREATE TABLE IF NOT EXISTS nexus_spec_links (
    spec_id uuid NOT NULL REFERENCES nexus_specs(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    -- The requirement the reference is written in, and the section it names.
    requirement integer NOT NULL,
    target_section integer NOT NULL,
    reference text NOT NULL DEFAULT '',
    PRIMARY KEY (spec_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_nexus_spec_links_target
    ON nexus_spec_links(spec_id, target_section);

CREATE TABLE IF NOT EXISTS nexus_spec_stages (
    spec_id uuid NOT NULL REFERENCES nexus_specs(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    title text NOT NULL DEFAULT '',
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    -- Offsets in the UTF-8 file the agent is given, for text with no line breaks.
    start_byte integer NOT NULL,
    end_byte integer NOT NULL,
    token_estimate integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'planning', 'planned', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_owner text,
    lease_expires timestamptz,
    provider text,
    -- The validated tasks for this stage, as they will be stored on the run.
    plan jsonb,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (spec_id, ordinal),
    CHECK (start_char >= 0 AND end_char > start_char AND end_line >= start_line
           AND start_byte >= 0 AND end_byte > start_byte)
);

-- A goal whose text is kept as a specification, and the part of the
-- specification a run task covers:
--   {"spec_id", "stage", "stages", "lines": [a, b], "bytes": [s, e],
--    "sections": [first, last], "requirements": ["R-0012", ...]}
ALTER TABLE nexus_goals ADD COLUMN IF NOT EXISTS spec_id uuid
    REFERENCES nexus_specs(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_goals_spec
    ON nexus_goals(spec_id) WHERE spec_id IS NOT NULL;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS spec_ref jsonb;

ALTER TABLE nexus_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_spec_sections ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_spec_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_spec_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_spec_stages ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_specs, nexus_spec_sections, nexus_spec_requirements,
    nexus_spec_links, nexus_spec_stages FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_specs, nexus_spec_sections, '
                       'nexus_spec_requirements, nexus_spec_links, '
                       'nexus_spec_stages FROM %I', r);
    END LOOP;
END $$;
COMMIT;
