//! A line-oriented diagnostic for the production transition decisions. No DB writes.
use nexus_domain::state::{
    can_transition_task, check_transition_run, check_transition_workflow, RunState, TaskState,
    WorkflowState,
};
use std::io::{self, BufRead};

fn main() {
    for line in io::stdin().lock().lines() {
        let line = line.expect("read decision");
        let fields: Vec<_> = line.split_whitespace().collect();
        let result = match fields.as_slice() {
            ["run", from, to] => RunState::parse(from)
                .zip(RunState::parse(to))
                .and_then(|(a, b)| check_transition_run(a, b).ok().map(|s| s.as_str())),
            ["workflow", from, to] => WorkflowState::parse(to).and_then(|b| {
                check_transition_workflow(WorkflowState::parse(from), b)
                    .ok()
                    .map(|s| s.as_str())
            }),
            ["task", from, to] => TaskState::parse(from)
                .zip(TaskState::parse(to))
                .and_then(|(a, b)| can_transition_task(a, b).then_some(b.as_str())),
            _ => panic!("expected machine current target"),
        };
        println!("{}", result.unwrap_or("refused"));
    }
}
