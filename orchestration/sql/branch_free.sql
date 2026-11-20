(t.repository = '' OR (
    NOT EXISTS (SELECT 1 FROM nexus_run_tasks o
                WHERE o.repository=t.repository AND o.branch=t.branch AND o.id<>t.id
                  AND o.state IN ('dispatched','running','verifying'))
    AND NOT EXISTS (SELECT 1 FROM nexus_run_tasks a
                    WHERE a.repository=t.repository AND a.branch=t.branch
                      AND jsonb_array_length(a.abandoned)>0)
    AND NOT EXISTS (SELECT 1 FROM nexus_executor_stops s
                    WHERE s.repository=t.repository AND s.branch=t.branch AND s.state='requested')
))
