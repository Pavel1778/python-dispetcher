-- Index the branch that claim_task interrogates on every claim attempt.
--
-- claim_task's candidate selection applies ``_BRANCH_FREE`` per candidate
-- task: three correlated subqueries. Two probe ``nexus_run_tasks`` by
-- ``(repository, branch)`` (one filters live states, one probes the
-- ``abandoned`` json array) and one probes ``nexus_executor_stops`` by
-- ``(repository, branch, state)``. Without an index each probe is a full
-- sequential scan of the task history — measured at ~11.5 ms per probe on a
-- 60,000-task table (EXPLAIN (ANALYZE, BUFFERS), PG 16, see
-- docs/PERFORMANCE.md for the method). Every worker pays this per claim, so
-- claim latency (and the lock hold time around it) grew linearly with task
-- history.
--
-- Additive only: two new indexes, no columns, no data. Idempotent.
-- ``repository = ''`` rows (no linked repository) are covered as well: the
-- empty string is a perfectly good index key and the claim path probes it.

CREATE INDEX IF NOT EXISTS idx_nexus_run_tasks_repo_branch
    ON nexus_run_tasks (repository, branch);

CREATE INDEX IF NOT EXISTS idx_nexus_executor_stops_repo_branch
    ON nexus_executor_stops (repository, branch, state);
