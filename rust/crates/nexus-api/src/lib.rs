//! `nexus-api` - the NEXUS HTTP API in Rust (Axum).
//!
//! Phase 2's API-compatibility layer. It serves the routes whose contract is
//! already fixed by `docs/API.md` and the Python server, and returns the **same
//! JSON** for the same inputs. The parity test
//! (`tests/test_rust_api_parity.py`) starts this server and the Python server
//! on the same database and compares their answers field for field.
//!
//! What is served here, and nothing more:
//! * `GET /healthz` - `{"status":"ok"}`, exactly the Python constant;
//! * `GET /readyz` - liveness plus a real database round-trip;
//! * `GET /api/v1/projects` - the caller's projects;
//! * `GET /api/v1/projects/{id}` - one project;
//! * `GET /api/v1/projects/{id}/runs` - a project's active runs.
//!
//! Authentication (session/CSRF and bearer) and the rest of the surface are the
//! Python server's until they are migrated route by route; this layer does not
//! pretend to serve them. The routes here are the read paths, which is where a
//! parity comparison is cheap and unambiguous.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use nexus_store::Store;
use serde_json::{json, Value};
use uuid::Uuid;

/// The shared state: a store and the principal the caller acts as.
///
/// Real authentication is the Python server's; this layer takes the principal
/// from a header (`X-Nexus-Principal`) so the parity test can drive both servers
/// as the same user. A production wiring would put the session/bearer check in
/// front of this exactly as the Python server does.
#[derive(Clone)]
pub struct AppState {
    pub store: Store,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/api/v1/projects", get(list_projects))
        .route("/api/v1/projects/{id}", get(get_project))
        .route("/api/v1/projects/{id}/runs", get(list_runs))
        .with_state(state)
}

/// `HEALTHZ`: a one-line liveness probe with no configuration, exactly as the
/// Python server answers it.
async fn healthz() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

/// Readiness: liveness plus a real database round-trip, the same contract as
/// Python's `/readyz`.
async fn readyz(State(state): State<AppState>) -> Response {
    match sqlx_ping(&state.store).await {
        Ok(()) => (
            StatusCode::OK,
            Json(json!({"status": "ready", "database": true})),
        )
            .into_response(),
        Err(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"status": "not_ready", "database": false})),
        )
            .into_response(),
    }
}

async fn sqlx_ping(store: &Store) -> Result<(), sqlx::Error> {
    sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(store.pool())
        .await?;
    Ok(())
}

/// A principal header the parity test sets; the Python server derives the same
/// value from its session/bearer. Kept explicit so no route here silently
/// serves a cross-project read.
fn principal(headers: &axum::http::HeaderMap) -> Option<String> {
    headers
        .get("x-nexus-principal")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

async fn list_projects(State(state): State<AppState>, headers: axum::http::HeaderMap) -> Response {
    let Some(who) = principal(&headers) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    match state.store.list_projects(&who).await {
        Ok(projects) => (StatusCode::OK, Json(json!({"projects": projects}))).into_response(),
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "database"})),
        )
            .into_response(),
    }
}

async fn get_project(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Path(id): Path<Uuid>,
) -> Response {
    let Some(who) = principal(&headers) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    match state.store.project_for_member(&who, id).await {
        Ok(Some(project)) => (StatusCode::OK, Json(json!({"project": project}))).into_response(),
        Ok(None) => (StatusCode::NOT_FOUND, Json(json!({"error": "not found"}))).into_response(),
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "database"})),
        )
            .into_response(),
    }
}

async fn list_runs(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Path(id): Path<Uuid>,
) -> Response {
    let Some(who) = principal(&headers) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    match state.store.list_runs_for_member(&who, id).await {
        Ok(Some(runs)) => (StatusCode::OK, Json(json!({"runs": runs}))).into_response(),
        Ok(None) => (StatusCode::NOT_FOUND, Json(json!({"error": "not found"}))).into_response(),
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "database"})),
        )
            .into_response(),
    }
}
