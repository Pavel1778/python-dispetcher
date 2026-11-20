(t.pending_start IS NULL AND NOT EXISTS (
    SELECT 1 FROM nexus_dispatch_intents d WHERE d.task_id=t.id AND d.lease_token=t.lease_token
      AND d.state IN ('prepared','sending','uncertain','accepted','identified')
))
