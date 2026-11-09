-- Workspace exclusivity: a real cross-process lock, enforced by the database.
--
-- Before this, `claim_task` checked whether another non-terminal task named the
-- same repository+branch, but the check read other rows: two dispatchers could
-- claim two *different* queued rows for one workspace in the same instant, each
-- seeing no holder, and both would then edit one checkout (the loser's commits
-- vanish with no error).
--
-- A partial unique index makes that race impossible: the second
-- `UPDATE ... state='assigned'` violates the index and is refused. The index
-- only covers the states that mean "a conversation may be editing this checkout
-- right now", so a merely queued sibling holds nothing. Repository-less tasks
-- (selected_repository IS NULL) never contend: a NULL key is distinct in a
-- unique index, so any number of analysis-only tasks may run at once.
--
-- `blocked` is included on purpose, matching the previous check: a blocked task
-- may resume and must keep its workspace reserved until an operator resolves it.
BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_workspace_active
    ON tasks (selected_repository, COALESCE(selected_branch, ''))
    WHERE selected_repository IS NOT NULL
      AND state IN ('assigned', 'running', 'verifying', 'blocked', 'recovering');

COMMIT;
