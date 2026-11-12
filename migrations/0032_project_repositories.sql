-- Several repositories in one project, each with its own GitHub account.
--
-- A project could already link several repositories
-- (``nexus_project_repositories``, 0014), and a link is still the only thing
-- that authorizes work on a repository. What a link did not say:
--
--   * **which GitHub account reads and writes this repository**. A project had
--     one account for everything (``nexus_projects.github_account_id``), so
--     ``account-A/frontend`` and ``account-B/backend`` could not both be
--     reached. ``github_account_id`` on the link names the account for that
--     repository; without it the project's account is used, and without that
--     the server's own token - exactly as before;
--   * **which repository is the main one** (``is_primary``): the one a task
--     that names no repository works on. At most one per project;
--   * **what the repository is for** (``role_note``) and **which branch** its
--     tasks work on (``branch``; empty means the repository's default). Both
--     are given to the planner, so that it can assign a task to the right
--     repository, and to the agent, so that it knows what it is looking at;
--   * **whether the link is switched on** (``enabled``). A link that is off
--     stays in the list and authorizes nothing;
--   * **what the last access check found** (``access``, ``checked_at``): can
--     the account read it, can it push, is it private.
--
-- And a run's tasks may now belong to different repositories, so the report of
-- a run speaks of each (``nexus_run_tasks.repository`` and ``branch`` have been
-- per task since 0018; nothing changes there).
--
-- Additive. Existing links keep working unchanged: no account, not primary,
-- enabled.
BEGIN;

ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS github_account_id uuid
        REFERENCES nexus_github_accounts(id) ON DELETE SET NULL;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS is_primary boolean NOT NULL DEFAULT false;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS label text NOT NULL DEFAULT '';
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS branch text NOT NULL DEFAULT ''
        CHECK (length(branch) <= 250);
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS role_note text NOT NULL DEFAULT ''
        CHECK (length(role_note) <= 500);
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS access jsonb;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;
ALTER TABLE nexus_project_repositories
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_nexus_project_repositories_account
    ON nexus_project_repositories(project_id, github_account_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_project_repositories_primary
    ON nexus_project_repositories(project_id) WHERE is_primary;

-- The repository a project is bound to (``nexus_projects.repository_id``) is its
-- main one, on the bound branch. From here on the two are changed together.
UPDATE nexus_project_repositories pr
   SET is_primary = true,
       branch = CASE WHEN p.selected_branch IS NULL OR p.selected_branch = r.default_branch
                     THEN '' ELSE p.selected_branch END
  FROM nexus_projects p, repositories r
 WHERE p.id = pr.project_id AND r.id = pr.repository_id
   AND p.repository_id = pr.repository_id
   AND NOT EXISTS (SELECT 1 FROM nexus_project_repositories o
                   WHERE o.project_id = pr.project_id AND o.is_primary);
-- A project bound to nothing, with exactly one linked repository, has an
-- obvious main one as well (and is bound to it).
UPDATE nexus_project_repositories pr SET is_primary = true
 WHERE NOT EXISTS (SELECT 1 FROM nexus_project_repositories o
                   WHERE o.project_id = pr.project_id AND o.is_primary)
   AND (SELECT count(*) FROM nexus_project_repositories o
        WHERE o.project_id = pr.project_id) = 1;
UPDATE nexus_projects p
   SET repository_id = pr.repository_id,
       selected_branch = COALESCE(p.selected_branch, r.default_branch)
  FROM nexus_project_repositories pr, repositories r
 WHERE pr.project_id = p.id AND pr.is_primary AND r.id = pr.repository_id
   AND p.repository_id IS NULL;

COMMIT;
