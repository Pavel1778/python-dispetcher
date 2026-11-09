-- D-2: project execution binding + Evidence Trust Engine storage.
--
-- Additive and backward compatible. Nothing here backfills legacy rows: a task,
-- workflow or evidence record with no project stays legacy/unscoped on purpose.
-- Deleting a project never deletes execution history (project_id is SET NULL,
-- not CASCADE, on tasks/workflows/evidence).
--
-- Numbering: 0012/0013 are reserved for the deferred File Lab (D-1) workstream.
-- This is 0014, the next free number after 0011. The migration runner applies
-- every ``*.sql`` in lexical order and does not require a contiguous sequence,
-- so a gap is safe.
BEGIN;

-- ---------------------------------------------------------------- project ↔ repo
-- Many-to-many: one repository can be shared by several projects through an
-- explicit link. This is the only thing that authorizes a project to run work
-- against a repository.
CREATE TABLE IF NOT EXISTS nexus_project_repositories (
    project_id    uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    repository_id uuid NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL DEFAULT 'system',
    PRIMARY KEY (project_id, repository_id)
);
CREATE INDEX IF NOT EXISTS idx_nexus_project_repositories_repo
    ON nexus_project_repositories(repository_id, project_id);

-- ------------------------------------------------- project context on execution
-- Nullable: legacy tasks/workflows stay unscoped. No automatic backfill.
ALTER TABLE tasks     ADD COLUMN IF NOT EXISTS project_id uuid
    REFERENCES nexus_projects(id) ON DELETE SET NULL;
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS project_id uuid
    REFERENCES nexus_projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_project     ON tasks(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflows_project ON workflows(project_id, created_at DESC);
-- Canonical repository link lookups (repository_id already has an FK to repositories).
CREATE INDEX IF NOT EXISTS idx_tasks_repository  ON tasks(repository_id);

-- --------------------------------------------------- canonical repository identity
-- ``tasks.repository_id`` is canonical; ``tasks.selected_repository`` is kept for
-- backward compatibility. This trigger makes them agree:
--   * repository_id set        -> selected_repository is derived from it;
--   * only selected_repository -> repository_id is resolved from the allowlist;
--   * both set and disagreeing -> the write is refused (never a random pick);
--   * neither can be resolved  -> repository_id stays NULL (analysis-only task).
-- It also refuses a cross-project reference: a task may only name a repository
-- that is explicitly linked to its project, and a workflow it belongs to must be
-- in the same project.
CREATE OR REPLACE FUNCTION nexus_tasks_check_binding() RETURNS trigger AS $$
DECLARE
    repo_full   text;
    wf_project  uuid;
BEGIN
    -- Resolve the canonical repository.
    IF NEW.repository_id IS NOT NULL THEN
        SELECT full_name INTO repo_full FROM repositories WHERE id = NEW.repository_id;
        IF repo_full IS NULL THEN
            RAISE EXCEPTION 'task repository_id % does not exist', NEW.repository_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF NEW.selected_repository IS NOT NULL
           AND NEW.selected_repository <> repo_full THEN
            RAISE EXCEPTION
                'task repository_id (%) and selected_repository (%) conflict',
                repo_full, NEW.selected_repository USING ERRCODE = 'check_violation';
        END IF;
        NEW.selected_repository := repo_full;
    ELSIF NEW.selected_repository IS NOT NULL THEN
        SELECT id INTO NEW.repository_id FROM repositories
         WHERE full_name = NEW.selected_repository;
    END IF;

    -- Workflow must be in the same project as the task.
    IF NEW.workflow_id IS NOT NULL THEN
        SELECT project_id INTO wf_project FROM workflows WHERE id = NEW.workflow_id;
        IF NEW.project_id IS NOT NULL AND wf_project IS NOT NULL
           AND wf_project <> NEW.project_id THEN
            RAISE EXCEPTION
                'task project % does not match workflow project %',
                NEW.project_id, wf_project USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- A project-scoped task may only use a repository linked to that project.
    IF NEW.project_id IS NOT NULL AND NEW.repository_id IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM nexus_project_repositories
                        WHERE project_id = NEW.project_id
                          AND repository_id = NEW.repository_id) THEN
            RAISE EXCEPTION
                'repository % is not linked to project %',
                NEW.repository_id, NEW.project_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_nexus_tasks_binding ON tasks;
CREATE TRIGGER trg_nexus_tasks_binding
    BEFORE INSERT OR UPDATE OF repository_id, selected_repository, project_id, workflow_id
    ON tasks
    FOR EACH ROW EXECUTE FUNCTION nexus_tasks_check_binding();

-- A workflow's project must not silently change while it owns tasks. This keeps
-- the "task and workflow share a project" invariant honest.
CREATE OR REPLACE FUNCTION nexus_workflows_check_project() RETURNS trigger AS $$
BEGIN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        IF EXISTS (SELECT 1 FROM tasks WHERE workflow_id = OLD.id
                    AND project_id IS NOT NULL
                    AND project_id IS DISTINCT FROM NEW.project_id) THEN
            RAISE EXCEPTION
                'workflow % has tasks in a different project', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_nexus_workflows_project ON workflows;
CREATE TRIGGER trg_nexus_workflows_project
    BEFORE UPDATE OF project_id ON workflows
    FOR EACH ROW EXECUTE FUNCTION nexus_workflows_check_project();

-- ------------------------------------------------------------- evidence store
-- Signed, append-only evidence records. ``project_id`` is NULL for legacy/unscoped
-- records; those are never given a fabricated project. The signed ``body`` carries
-- project_id for format_version 2, so editing the column alone breaks verification.
CREATE TABLE IF NOT EXISTS nexus_evidence (
    seq                 bigserial PRIMARY KEY,
    id                  uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    project_id          uuid REFERENCES nexus_projects(id) ON DELETE SET NULL,
    format_version      integer NOT NULL DEFAULT 2 CHECK (format_version IN (1, 2)),
    repository_id       uuid REFERENCES repositories(id) ON DELETE SET NULL,
    repository_full_name text,
    commit_sha          text,
    branch              text,
    subject_kind        text NOT NULL CHECK (subject_kind IN
                            ('task', 'workflow', 'project', 'manual')),
    subject_id          uuid,
    body                jsonb NOT NULL,
    -- The exact canonical bytes that were signed. Verification reads this
    -- verbatim, so a jsonb round-trip cannot mask a tamper, and every indexed
    -- column below is cross-checked against it.
    signed_payload      text NOT NULL,
    key_id              text NOT NULL,
    signature           text NOT NULL,
    verdict             text NOT NULL CHECK (verdict IN
                            ('VERIFIED', 'UNVERIFIED', 'FAILED', 'BLOCKED')),
    verification_detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    verified_at         timestamptz,
    prev_hash           text,
    entry_hash          text NOT NULL,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nexus_evidence_project
    ON nexus_evidence(project_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_nexus_evidence_subject
    ON nexus_evidence(subject_kind, subject_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_nexus_evidence_repo
    ON nexus_evidence(repository_id, seq DESC);
-- Chain lookups: the newest entry in a scope is the tail the next one links to.
CREATE INDEX IF NOT EXISTS idx_nexus_evidence_chain
    ON nexus_evidence(project_id, seq DESC, prev_hash);

ALTER TABLE nexus_project_repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus_evidence ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON nexus_project_repositories, nexus_evidence FROM PUBLIC;
REVOKE ALL ON SEQUENCE nexus_evidence_seq_seq FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON nexus_project_repositories, nexus_evidence FROM %I', r);
        EXECUTE format('REVOKE ALL ON SEQUENCE nexus_evidence_seq_seq FROM %I', r);
    END LOOP;
END $$;

COMMIT;
