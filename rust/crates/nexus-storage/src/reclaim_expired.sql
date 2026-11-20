-- $1 run, $2 project, $3 bounded batch size, $4 policy read from locked run.
-- All RHS expressions see the OLD row. The recovery marker must record the
-- lost attempt/cycle, not the reset attempt from the next durable cycle.
WITH expired AS (
    SELECT id FROM nexus_run_tasks
    WHERE run_id=$1 AND project_id=$2 AND state IN ('dispatched','running')
      AND conversation_id IS NULL AND lease_expires < now()
    ORDER BY lease_expires, id LIMIT $3 FOR UPDATE SKIP LOCKED
)
UPDATE nexus_run_tasks t SET
    lease_owner=NULL, lease_expires=NULL, lease_token=t.lease_token+1,
    state=CASE WHEN NOT $4 AND t.attempt>=t.max_attempts THEN 'failed' ELSE 'pending' END,
    attempt=CASE WHEN $4 AND t.attempt>=t.max_attempts THEN 0 ELSE t.attempt END,
    cycle=t.cycle + CASE WHEN $4 AND t.attempt>=t.max_attempts THEN 1 ELSE 0 END,
    tried_accounts=CASE WHEN $4 AND t.attempt>=t.max_attempts THEN '[]'::jsonb ELSE t.tried_accounts END,
    evidence=CASE WHEN NOT $4 AND t.attempt>=t.max_attempts THEN t.evidence
                  ELSE t.evidence || jsonb_build_array(jsonb_build_object(
                      'reclaimed', true, 'attempt', t.attempt, 'cycle', t.cycle)) END,
    last_error=CASE WHEN NOT $4 AND t.attempt>=t.max_attempts THEN 'lease expired after max attempts'
                    ELSE 'lease expired' END,
    updated_at=now()
FROM expired WHERE t.id=expired.id RETURNING t.id
