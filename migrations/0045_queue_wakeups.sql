-- PostgreSQL is the queue. Notifications are non-durable, type-only hints.
-- They are delivered only on commit and listeners must still poll for recovery.
CREATE OR REPLACE FUNCTION nexus_notify_queue_wakeup() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('nexus_queue_wakeup', TG_ARGV[0]);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS nexus_task_wakeup_insert ON tasks;
CREATE TRIGGER nexus_task_wakeup_insert AFTER INSERT ON tasks
FOR EACH ROW WHEN (NEW.state = 'queued')
EXECUTE FUNCTION nexus_notify_queue_wakeup('task');

DROP TRIGGER IF EXISTS nexus_task_wakeup_update ON tasks;
CREATE TRIGGER nexus_task_wakeup_update AFTER UPDATE OF state ON tasks
FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state IN ('queued', 'failed', 'recovering', 'completed'))
EXECUTE FUNCTION nexus_notify_queue_wakeup('task');

DROP TRIGGER IF EXISTS nexus_task_wakeup_approval ON tasks;
CREATE TRIGGER nexus_task_wakeup_approval AFTER UPDATE OF approved ON tasks
FOR EACH ROW WHEN (OLD.approved IS DISTINCT FROM NEW.approved AND NEW.approved IS TRUE AND NEW.state = 'queued')
EXECUTE FUNCTION nexus_notify_queue_wakeup('task');

DROP TRIGGER IF EXISTS nexus_run_task_wakeup_insert ON nexus_run_tasks;
CREATE TRIGGER nexus_run_task_wakeup_insert AFTER INSERT ON nexus_run_tasks
FOR EACH ROW WHEN (NEW.state = 'pending')
EXECUTE FUNCTION nexus_notify_queue_wakeup('lab');

DROP TRIGGER IF EXISTS nexus_run_wakeup_insert ON nexus_runs;
CREATE TRIGGER nexus_run_wakeup_insert AFTER INSERT ON nexus_runs
FOR EACH ROW WHEN (NEW.state = 'running')
EXECUTE FUNCTION nexus_notify_queue_wakeup('lab');

DROP TRIGGER IF EXISTS nexus_run_wakeup_update ON nexus_runs;
CREATE TRIGGER nexus_run_wakeup_update AFTER UPDATE OF state ON nexus_runs
FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
EXECUTE FUNCTION nexus_notify_queue_wakeup('lab');
