((t.state='pending' AND (t.lease_expires IS NULL OR t.lease_expires < now()))
 OR (t.state='waiting' AND (t.retry_at IS NULL OR t.retry_at <= now())))
AND NOT EXISTS (SELECT 1 FROM nexus_dispatch_intents d WHERE d.task_id=t.id
                AND d.state IN ('sending','uncertain','accepted','identified'))
