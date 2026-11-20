((t.state='pending' AND (t.lease_expires IS NULL OR t.lease_expires < now()))
 OR (t.state='waiting' AND (t.retry_at IS NULL OR t.retry_at <= now())))
