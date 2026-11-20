NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep)
    WHERE NOT EXISTS (SELECT 1 FROM nexus_run_tasks dep
                      WHERE dep.run_id=t.run_id AND dep.task_key=d.dep
                        AND dep.state IN ('completed_verified','completed_no_changes'))
)
