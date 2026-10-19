"""Dashboard HTTP behaviour: auth gating, CSRF, and real store rendering."""

from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient

from app import main as app_main
from app.auth import hash_password
from app.config import Settings
from app.store.memory import InMemoryStateStore

USERNAME = "operator"
PASSWORD = "correct horse battery staple"
SESSION_SECRET = "test-session-secret"


def _settings(**overrides) -> Settings:
    base: dict = {
        "openhands_cloud_api_key": "sk-test",
        "dashboard_enabled": True,
        "dashboard_username": USERNAME,
        "dashboard_password_hash": hash_password(PASSWORD),
        "dashboard_session_secret": SESSION_SECRET,
        "dashboard_cookie_secure": False,
        "dispatcher_enable_background_loop": False,
        "mcp_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _client(settings: Settings) -> TestClient:
    store = InMemoryStateStore()
    app_main.runtime.settings = settings
    app_main.runtime.store = store
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None
    return TestClient(app_main.create_app())


def _reset_runtime() -> None:
    app_main.runtime.settings = None
    app_main.runtime.store = None
    app_main.runtime.client = None
    app_main.runtime.dispatcher = None
    app_main.runtime.orchestrator = None


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


def test_root_redirects_to_login_when_anonymous():
    try:
        with _client(_settings()) as c:
            resp = c.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
    finally:
        _reset_runtime()


def test_dashboard_disabled_without_credentials_fails_closed():
    """A half-configured dashboard must not expose an unauthenticated UI."""
    try:
        with _client(_settings(dashboard_username=None, dashboard_password_hash=None)) as c:
            assert c.get("/login", follow_redirects=False).status_code == 503
            assert c.get("/", follow_redirects=False).status_code == 303
            # Even a correct-looking login cannot succeed.
            resp = c.post("/login", data={"username": "x", "password": "y"})
            assert resp.status_code == 503
            # The API is closed too.
            assert c.get("/api/config").status_code == 401
    finally:
        _reset_runtime()


def test_api_endpoints_require_session():
    try:
        with _client(_settings()) as c:
            assert c.get("/api/config").status_code == 401
            assert c.get("/api/health").status_code == 401
    finally:
        _reset_runtime()


def test_login_rejects_wrong_password_and_username():
    try:
        with _client(_settings()) as c:
            r1 = c.post("/login", data={"username": USERNAME, "password": "wrong"})
            r2 = c.post("/login", data={"username": "nobody", "password": PASSWORD})
        assert r1.status_code == 401
        assert r2.status_code == 401
        # Identical message: no username/password oracle.
        assert "Неверный логин или пароль." in r1.text
        assert "Неверный логин или пароль." in r2.text
    finally:
        _reset_runtime()


def test_login_success_sets_httponly_session_cookie():
    try:
        with _client(_settings()) as c:
            resp = c.post(
                "/login",
                data={"username": USERNAME, "password": PASSWORD},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        set_cookie = resp.headers.get_list("set-cookie")
        session_cookie = next(h for h in set_cookie if h.startswith("dispatcher_session="))
        assert "HttpOnly" in session_cookie
        assert "SameSite=lax" in session_cookie
    finally:
        _reset_runtime()


def test_authenticated_dashboard_renders_real_store_data():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            resp = c.get("/")
        assert resp.status_code == 200
        assert "OpenHands Dispatcher" in resp.text
        # Empty store renders the honest empty state, not a mock row.
        assert "Задач пока нет" in resp.text
    finally:
        _reset_runtime()


def test_create_task_from_dashboard_persists_and_renders():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            page = c.get("/")
            csrf = _csrf_from(page.text)
            resp = c.post(
                "/tasks",
                data={
                    "csrf_token": csrf,
                    "title": "Описать архитектуру",
                    "prompt": "Сделай обзор проекта",
                    "repository": "",
                    "branch": "",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            after = c.get("/")
            assert "Описать архитектуру" in after.text
            assert "Сделай обзор проекта" not in after.text  # prompt not in list view
            # The task really exists in the store: the detail page (which reads
            # the store directly) shows it and its prompt.
            listing = c.get("/")
            wid = re.search(r"/workflows/([0-9a-f-]{36})", listing.text).group(1)
            detail = c.get(f"/workflows/{wid}")
            assert detail.status_code == 200
            assert "Сделай обзор проекта" in detail.text
    finally:
        _reset_runtime()


def test_create_task_requires_csrf_token():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            resp = c.post(
                "/tasks",
                data={"csrf_token": "bogus", "title": "t", "prompt": "p"},
            )
        assert resp.status_code == 403
    finally:
        _reset_runtime()


def test_create_task_rejects_repository_outside_allowlist():
    try:
        with _client(_settings(jr_allowed_repositories="owner/allowed")) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            csrf = _csrf_from(c.get("/").text)
            resp = c.post(
                "/tasks",
                data={
                    "csrf_token": csrf,
                    "title": "t",
                    "prompt": "p",
                    "repository": "evil/repo",
                },
            )
        assert resp.status_code == 400
        assert "не разрешён" in resp.text
    finally:
        _reset_runtime()


def test_create_task_validates_required_fields():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            csrf = _csrf_from(c.get("/").text)
            blank_title = c.post(
                "/tasks", data={"csrf_token": csrf, "title": "", "prompt": "p"}
            )
            blank_prompt = c.post(
                "/tasks", data={"csrf_token": csrf, "title": "t", "prompt": ""}
            )
        assert blank_title.status_code == 400
        assert "название" in blank_title.text.lower()
        assert blank_prompt.status_code == 400
        assert "описание" in blank_prompt.text.lower()
    finally:
        _reset_runtime()


def test_api_config_never_leaks_secret_values():
    secret_key = "sk-live-SUPER-SECRET-VALUE"
    try:
        with _client(
            _settings(
                openhands_cloud_api_key=secret_key,
                database_url="postgresql://user:DBPASSWORD@host:5432/db",
            )
        ) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            resp = c.get("/api/config")
        assert resp.status_code == 200
        body = resp.text
        assert secret_key not in body
        assert "DBPASSWORD" not in body
        assert SESSION_SECRET not in body
        data = resp.json()
        assert data["api_key_configured"] is True
        assert data["database_configured"] is True
    finally:
        _reset_runtime()


def test_logout_clears_session_and_requires_csrf():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            csrf = _csrf_from(c.get("/").text)

            # Without a valid CSRF token the logout is refused.
            bad = c.post("/logout", data={"csrf_token": "nope"}, follow_redirects=False)
            assert bad.status_code == 403

            good = c.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
            assert good.status_code == 303
            # Session is gone: the dashboard redirects to login again.
            assert c.get("/", follow_redirects=False).status_code == 303
    finally:
        _reset_runtime()


def test_workflow_detail_page_renders_events():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            csrf = _csrf_from(c.get("/").text)
            c.post(
                "/tasks",
                data={"csrf_token": csrf, "title": "Задача A", "prompt": "детали"},
            )
            store = app_main.runtime.store
            workflows = asyncio.get_event_loop().run_until_complete(
                store.list_workflows()
            )
            resp = c.get(f"/workflows/{workflows[0].id}")
        assert resp.status_code == 200
        assert "Задача A" in resp.text
        assert "task_created_ui" in resp.text
    finally:
        _reset_runtime()


def test_workflow_detail_unknown_id_returns_404():
    try:
        with _client(_settings()) as c:
            c.post("/login", data={"username": USERNAME, "password": PASSWORD})
            resp = c.get("/workflows/does-not-exist")
        assert resp.status_code == 404
    finally:
        _reset_runtime()
