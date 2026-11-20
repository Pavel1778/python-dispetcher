//! DAG validation: the shape and the safety of a plan, enforced by the
//! application, never by the model.
//!
//! This mirrors `orchestration/nexus_planner.validate_tasks`:
//!
//! * every task gets a stable key, acceptance criteria and a dependency list;
//! * dependencies must reference keys in the same plan; self- and duplicate
//!   dependencies and cycles are rejected (topological check);
//! * a task is only persisted if the whole plan validates, so a bad plan leaves
//!   no half-written run.
//!
//! The Python also derives a task *kind* from the model's word or the task's
//! own words. That derivation is language-sensitive (it reads Russian and
//! English stems); it is deliberately **not** migrated here, and the parity
//! test excludes it - see `docs/RUST_MIGRATION.md`. The validator here keeps
//! whatever `kind` the caller supplies, exactly as the Python's `keep` list
//! does for the fields it is told to carry.

use crate::error::DomainError;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet, VecDeque};

/// The maximum number of tasks in one plan (`validate_tasks(max_tasks=50)`).
pub const MAX_TASKS: usize = 50;

/// One planned task, normalised.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlannedTask {
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub acceptance: Vec<String>,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default = "default_kind")]
    pub kind: String,
}

fn default_kind() -> String {
    "implementation".to_string()
}

/// A task key: `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` (`nexus_planner._TASK_KEY`).
fn valid_task_key(key: &str) -> bool {
    let bytes = key.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return false;
    }
    let first = bytes[0] as char;
    if !first.is_ascii_alphanumeric() {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|&b| (b as char).is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Validate a plan's raw JSON task list and return it normalised, in input
/// order.
///
/// The input is the `{"tasks": [...]}` object (or a bare list) the model
/// returns; the output is the same normalisation the Python performs:
/// `title` trimmed and capped at 200 characters, `description` capped at 4000,
/// acceptance entries capped at 1000 (at most 20), dependencies capped at 20.
pub fn validate_tasks(payload: &serde_json::Value) -> Result<Vec<PlannedTask>, DomainError> {
    let tasks = match payload {
        serde_json::Value::Object(map) => map.get("tasks"),
        other => Some(other),
    };
    let list = match tasks {
        Some(serde_json::Value::Array(items)) => items,
        _ => {
            return Err(DomainError::PlanTaskCount {
                got: 0,
                max: MAX_TASKS,
            })
        }
    };
    if list.is_empty() || list.len() > MAX_TASKS {
        return Err(DomainError::PlanTaskCount {
            got: list.len(),
            max: MAX_TASKS,
        });
    }

    let mut normalized: Vec<PlannedTask> = Vec::with_capacity(list.len());
    let mut keys: HashSet<String> = HashSet::new();

    for (i, item) in list.iter().enumerate() {
        let obj = item
            .as_object()
            .ok_or(DomainError::TaskNotObject { index: i })?;

        let key = match obj.get("id").and_then(|v| v.as_str()) {
            Some(k) => k.to_string(),
            None => format!("task-{}", i + 1),
        };
        if !valid_task_key(&key) {
            return Err(DomainError::TaskInvalidId { index: i });
        }
        if !keys.insert(key.clone()) {
            return Err(DomainError::TaskDuplicateId(key));
        }

        let title = match obj.get("title").and_then(|v| v.as_str()) {
            Some(t) if !t.trim().is_empty() => t.trim().to_string(),
            _ => return Err(DomainError::TaskNoTitle(key)),
        };

        // Acceptance: a string becomes a one-element list; every element must be
        // a string.
        let acceptance: Vec<String> = match obj.get("acceptance") {
            None => Vec::new(),
            Some(serde_json::Value::String(s)) => vec![truncate(s, 1000)],
            Some(serde_json::Value::Array(items)) => {
                let mut out = Vec::new();
                for a in items {
                    match a.as_str() {
                        Some(s) => out.push(truncate(s, 1000)),
                        None => return Err(DomainError::TaskBadAcceptance(key)),
                    }
                }
                out.truncate(20);
                out
            }
            Some(_) => return Err(DomainError::TaskBadAcceptance(key)),
        };

        // Dependencies: a list of ids, no duplicates, no self-reference.
        let deps: Vec<String> = match obj.get("dependencies") {
            None => Vec::new(),
            Some(serde_json::Value::Array(items)) => {
                let mut out = Vec::new();
                for d in items {
                    match d.as_str() {
                        Some(s) => out.push(s.to_string()),
                        None => return Err(DomainError::TaskBadDependencies(key)),
                    }
                }
                out
            }
            Some(_) => return Err(DomainError::TaskBadDependencies(key)),
        };
        let dep_set: HashSet<&String> = deps.iter().collect();
        if dep_set.len() != deps.len() || deps.iter().any(|d| d == &key) {
            return Err(DomainError::TaskBadDependencyList(key));
        }
        let mut deps = deps;
        deps.truncate(20);

        let description = match obj.get("description").and_then(|v| v.as_str()) {
            Some(d) => truncate(d, 4000),
            None => String::new(),
        };

        let kind = match obj.get("kind").and_then(|v| v.as_str()) {
            Some(k) if !k.is_empty() => k.to_string(),
            _ => default_kind(),
        };

        normalized.push(PlannedTask {
            id: key,
            title: truncate(&title, 200),
            description,
            acceptance,
            dependencies: deps,
            kind,
        });
    }

    check_dag(&normalized)?;
    Ok(normalized)
}

/// The topological check: every dependency names a task in the plan, and there
/// is no cycle.
pub fn check_dag(tasks: &[PlannedTask]) -> Result<(), DomainError> {
    let keys: HashSet<&str> = tasks.iter().map(|t| t.id.as_str()).collect();
    let mut outgoing: HashMap<&str, Vec<&str>> = HashMap::new();
    let mut indegree: HashMap<&str, usize> = HashMap::new();
    for t in tasks {
        outgoing.entry(t.id.as_str()).or_default();
        indegree.entry(t.id.as_str()).or_insert(0);
    }
    for t in tasks {
        for dep in &t.dependencies {
            if !keys.contains(dep.as_str()) {
                return Err(DomainError::TaskUnknownDependency(
                    t.id.clone(),
                    dep.clone(),
                ));
            }
            outgoing
                .entry(dep.as_str())
                .or_default()
                .push(t.id.as_str());
            *indegree.entry(t.id.as_str()).or_insert(0) += 1;
        }
    }

    let mut ready: VecDeque<&str> = indegree
        .iter()
        .filter(|(_, &d)| d == 0)
        .map(|(&k, _)| k)
        .collect();
    // Deterministic order so the parity comparison is stable.
    let mut sorted: Vec<&str> = ready.drain(..).collect();
    sorted.sort_unstable();
    ready.extend(sorted);

    let mut seen = 0usize;
    while let Some(key) = ready.pop_front() {
        seen += 1;
        if let Some(nexts) = outgoing.get(key) {
            for nxt in nexts {
                if let Some(d) = indegree.get_mut(nxt) {
                    *d -= 1;
                    if *d == 0 {
                        ready.push_back(nxt);
                    }
                }
            }
        }
    }

    if seen != tasks.len() {
        return Err(DomainError::PlanCycle);
    }
    Ok(())
}

/// A stable version hash of a plan payload.
///
/// This mirrors `nexus_workflow.plan_hash_of`: the validated task list is
/// serialised with sorted keys and no insignificant whitespace, then SHA-256.
/// The exact bytes are what makes the approval binding meaningful - a change to
/// a title, a dependency or an acceptance criterion produces a different hash.
///
/// The Python computes the digest over `json.dumps(plan, sort_keys=True,
/// ensure_ascii=False, separators=(",", ":"), default=str)`. This function
/// reproduces that byte-for-byte for the plan shapes the system stores (see the
/// parity test); it is not a general-purpose JSON re-serialiser.
pub fn plan_hash_of(plan: &[PlannedTask]) -> String {
    let value = plan_value(plan);
    let canonical = canonical_json(&value);
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// A plan as the JSON value `plan_hash_of` hashes.
fn plan_value(plan: &[PlannedTask]) -> serde_json::Value {
    serde_json::Value::Array(
        plan.iter()
            .map(|t| {
                let mut m = serde_json::Map::new();
                m.insert("id".into(), t.id.clone().into());
                m.insert("title".into(), t.title.clone().into());
                m.insert("description".into(), t.description.clone().into());
                m.insert(
                    "acceptance".into(),
                    serde_json::Value::Array(
                        t.acceptance.iter().map(|a| a.clone().into()).collect(),
                    ),
                );
                m.insert(
                    "dependencies".into(),
                    serde_json::Value::Array(
                        t.dependencies.iter().map(|d| d.clone().into()).collect(),
                    ),
                );
                m.insert("kind".into(), t.kind.clone().into());
                serde_json::Value::Object(m)
            })
            .collect(),
    )
}

/// `json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.
///
/// Python's `json.dumps` does not escape non-ASCII when `ensure_ascii=False`,
/// and uses `", "`/`": "` by default but `,`/`:` with `separators=(",", ":")`.
/// serde_json's compact form matches the latter for the value shapes here.
fn canonical_json(value: &serde_json::Value) -> String {
    serde_json::to_string(&sort_value(value)).expect("a JSON value always serialises")
}

/// Recursively sort object keys, so the serialisation matches `sort_keys=True`.
fn sort_value(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let mut sorted = serde_json::Map::new();
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for k in keys {
                sorted.insert(k.clone(), sort_value(&map[k]));
            }
            serde_json::Value::Object(sorted)
        }
        serde_json::Value::Array(items) => {
            serde_json::Value::Array(items.iter().map(sort_value).collect())
        }
        other => other.clone(),
    }
}

fn truncate(s: &str, max: usize) -> String {
    // The Python slices by code point (`s[:n]`), not by byte; char_indices keeps
    // the same boundary so a multibyte character is never cut in half.
    let mut out = String::new();
    for (count, ch) in s.chars().enumerate() {
        if count >= max {
            break;
        }
        out.push(ch);
    }
    out
}
