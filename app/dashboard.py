"""Operator dashboard: server-rendered HTML UI plus its auth endpoints.

Deliberately dependency-free (stdlib ``html`` + FastAPI responses), so the
service keeps the same small dependency set on Render Free. There is no template
engine and no JavaScript framework: every page is HTML rendered on the server
from the real store, so what the UI shows is what the database holds.

Security model
--------------
* The whole UI is **disabled unless both** ``DASHBOARD_USERNAME`` and
  ``DASHBOARD_PASSWORD_HASH`` are set. A half-configured deployment fails closed.
* Passwords are stored only as a scrypt hash; the plaintext never reaches this
  process (see ``app.auth``).
* Sessions are stateless HMAC-signed tokens in an ``HttpOnly`` cookie. Every
  private page and API route resolves the session through ``require_session``.
* State-changing requests are ``POST`` and require a CSRF token derived from the
  session (double-submit, no extra server state).
* Errors are shown as plain Russian messages; upstream exception text is scrubbed
  through the same redaction used for logs.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import SessionSigner, verify_password
from .config import Settings
from .logging_config import get_logger, safe_extra, scrub_text
from .status import TaskRole, TaskStatus, WorkflowStatus

logger = get_logger(__name__)

SESSION_COOKIE = "dispatcher_session"
CSRF_COOKIE = "dispatcher_csrf"

# Generic credential failure message. Never says which half was wrong.
_BAD_CREDENTIALS = "Неверный логин или пароль."


@dataclass
class DashboardDeps:
    """Everything the dashboard needs, injected so tests can substitute fakes."""

    settings: Settings
    store: Any
    dispatcher: Any
    orchestrator: Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _e(value: Any) -> str:
    """HTML-escape any value for safe interpolation."""
    return html.escape("" if value is None else str(value))


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# Status colours are the only "fake-free" visual signal: they come straight from
# the stored status string, never from a hard-coded mock.
_STATUS_CLASS = {
    TaskStatus.COMPLETED.value: "ok",
    TaskStatus.FAILED.value: "bad",
    TaskStatus.BLOCKED.value: "warn",
    TaskStatus.CANCELLED.value: "muted",
    TaskStatus.RUNNING.value: "run",
    TaskStatus.STARTING.value: "run",
    TaskStatus.VERIFYING.value: "run",
    TaskStatus.QUEUED.value: "muted",
}


def _status_badge(status: str) -> str:
    cls = _STATUS_CLASS.get(status, "muted")
    return f'<span class="badge {cls}">{_e(status)}</span>'


def _page(title: str, body: str, *, nav: str = "") -> str:
    """Wrap ``body`` in the shared responsive shell."""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{
  --bg:#0f1117; --panel:#171a23; --panel2:#1e222d; --fg:#e7e9ee; --muted:#8b93a7;
  --accent:#5b8cff; --ok:#2ecc71; --bad:#ff5c5c; --warn:#f5a524; --run:#4aa8ff;
  --border:#272b36;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif; }}
header {{ background:var(--panel); border-bottom:1px solid var(--border);
  padding:14px 18px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
header h1 {{ font-size:16px; margin:0; font-weight:600; letter-spacing:.2px; }}
header .sp {{ flex:1; }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
main {{ max-width:1080px; margin:0 auto; padding:18px; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
  padding:18px; margin-bottom:16px; }}
h2 {{ font-size:15px; margin:0 0 12px; font-weight:600; }}
label {{ display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }}
input, textarea, select {{ width:100%; padding:10px 12px; border-radius:8px;
  border:1px solid var(--border); background:var(--panel2); color:var(--fg);
  font:inherit; }}
textarea {{ min-height:120px; resize:vertical; }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:8px;
  padding:10px 16px; font:inherit; font-weight:600; cursor:pointer; }}
button:hover {{ filter:brightness(1.08); }}
button.ghost {{ background:transparent; border:1px solid var(--border); color:var(--fg); }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--border);
  vertical-align:top; font-size:14px; }}
th {{ color:var(--muted); font-weight:500; font-size:12px; text-transform:uppercase;
  letter-spacing:.4px; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px;
  font-weight:600; border:1px solid transparent; }}
.badge.ok {{ color:var(--ok); border-color:var(--ok); }}
.badge.bad {{ color:var(--bad); border-color:var(--bad); }}
.badge.warn {{ color:var(--warn); border-color:var(--warn); }}
.badge.run {{ color:var(--run); border-color:var(--run); }}
.badge.muted {{ color:var(--muted); border-color:var(--muted); }}
.muted {{ color:var(--muted); }}
.error {{ background:#2a1416; border:1px solid var(--bad); color:#ffb4b4;
  padding:10px 12px; border-radius:8px; margin-bottom:12px; }}
.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px;
  word-break:break-all; }}
.grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }}
.stat {{ background:var(--panel2); border:1px solid var(--border); border-radius:10px;
  padding:14px; }}
.stat .n {{ font-size:24px; font-weight:700; }}
.stat .l {{ color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.4px; }}
.empty {{ color:var(--muted); padding:14px 0; }}
.row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
@media (max-width:640px) {{
  main {{ padding:12px; }}
  th, td {{ padding:8px 6px; font-size:13px; }}
  table {{ display:block; overflow-x:auto; white-space:nowrap; }}
  header {{ padding:12px; }}
}}
</style>
</head>
<body>
<header>
  <h1>OpenHands Dispatcher</h1>
  <span class="sp"></span>
  {nav}
</header>
<main>{body}</main>
</body>
</html>"""


class Dashboard:
    """Builds the dashboard router and owns session/CSRF verification."""

    def __init__(self, deps: DashboardDeps) -> None:
        self._deps = deps
        settings = deps.settings
        self._username = settings.dashboard_username or ""
        secret = settings.dashboard_session_secret or ""
        # A signer is only constructed when the dashboard is fully configured.
        self._signer: SessionSigner | None = None
        if settings.dashboard_configured and secret:
            self._signer = SessionSigner(
                secret, ttl_seconds=settings.dashboard_session_ttl_seconds
            )

    # ------------------------------------------------------------------
    # configuration / session helpers
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._signer is not None

    def _session_from(self, request: Request) -> Any:
        if self._signer is None:
            return None
        return self._signer.verify(request.cookies.get(SESSION_COOKIE))

    def _csrf_ok(self, request: Request, submitted: str | None) -> bool:
        if self._signer is None:
            return False
        token = request.cookies.get(SESSION_COOKIE)
        expected = self._signer.csrf_token(token)
        return bool(expected) and bool(submitted) and secrets.compare_digest(
            expected, submitted or ""
        )

    def _set_session_cookies(self, response: Response, username: str) -> None:
        assert self._signer is not None
        token = self._signer.mint(username)
        secure = self._deps.settings.dashboard_cookie_secure
        max_age = self._signer.ttl_seconds
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
        # The CSRF token is readable by the page so the form can submit it back.
        response.set_cookie(
            CSRF_COOKIE,
            self._signer.csrf_token(token),
            max_age=max_age,
            httponly=False,
            samesite="lax",
            secure=secure,
            path="/",
        )

    def _clear_session_cookies(self, response: Response) -> None:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            response.delete_cookie(name, path="/")

    # ------------------------------------------------------------------
    # router
    # ------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/", response_class=HTMLResponse)
        async def index(request: Request) -> Response:
            session = self._session_from(request)
            if session is None:
                return RedirectResponse("/login", status_code=303)
            return HTMLResponse(await self._render_dashboard(request, session.username))

        @router.get("/login", response_class=HTMLResponse)
        async def login_form(request: Request) -> Response:
            if not self.enabled:
                return HTMLResponse(self._render_disabled(), status_code=503)
            if self._session_from(request) is not None:
                return RedirectResponse("/", status_code=303)
            return HTMLResponse(self._render_login())

        @router.post("/login")
        async def login(
            request: Request,
            username: str = Form(default=""),
            password: str = Form(default=""),
        ) -> Response:
            if not self.enabled:
                return HTMLResponse(self._render_disabled(), status_code=503)
            stored = self._deps.settings.dashboard_password_hash or ""
            # Verify both halves unconditionally so a wrong username and a wrong
            # password take the same time and return the same message.
            user_ok = secrets.compare_digest(username, self._username)
            pass_ok = verify_password(password, stored)
            if not (user_ok and pass_ok):
                logger.warning("dashboard login failed")
                return HTMLResponse(
                    self._render_login(error=_BAD_CREDENTIALS), status_code=401
                )
            response = RedirectResponse("/", status_code=303)
            self._set_session_cookies(response, username)
            logger.info("dashboard login succeeded")
            return response

        @router.post("/logout")
        async def logout(request: Request, csrf_token: str = Form(default="")) -> Response:
            if not self._csrf_ok(request, csrf_token):
                return HTMLResponse(self._render_forbidden(), status_code=403)
            response = RedirectResponse("/login", status_code=303)
            self._clear_session_cookies(response)
            return response

        # --- JSON API used by the pages (all session-protected) ---

        @router.get("/api/config")
        async def api_config(request: Request) -> Response:
            if self._session_from(request) is None:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            s = self._deps.settings
            # Only booleans and non-secret identifiers. Never a key or a DSN.
            return JSONResponse(
                {
                    "service": s.service_name,
                    "dashboard_configured": s.dashboard_configured,
                    "database_configured": bool(s.resolved_database_url),
                    "api_key_configured": bool(s.resolved_api_key),
                    "mcp_enabled": s.mcp_enabled,
                    "mcp_tools_attachable": bool(
                        s.mcp_enabled
                        and (s.mcp_plugin_source or s.mcp_public_base_url)
                    ),
                    "background_loop": s.dispatcher_enable_background_loop,
                    "poll_interval_seconds": s.dispatcher_poll_interval_seconds,
                    "max_attempts": s.dispatcher_max_attempts,
                    "allowed_repositories": s.allowed_repositories,
                    "start_task_timeout_seconds": s.dispatcher_start_task_timeout_seconds,
                }
            )

        @router.get("/api/health")
        async def api_health(request: Request) -> Response:
            if self._session_from(request) is None:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            store = self._deps.store
            db_ok = False
            try:
                db_ok = bool(await store.healthcheck())
            except Exception:  # noqa: BLE001 - report as unhealthy, never raise
                db_ok = False
            dispatcher = self._deps.dispatcher
            return JSONResponse(
                {
                    "status": "ok" if db_ok else "degraded",
                    "database": db_ok,
                    "dispatcher_cycle_ok": bool(
                        dispatcher and dispatcher.last_cycle_succeeded
                    ),
                    "last_cycle_at": (
                        dispatcher.last_cycle_at.isoformat()
                        if dispatcher and dispatcher.last_cycle_at
                        else None
                    ),
                }
            )

        # --- task creation ---

        @router.post("/tasks")
        async def create_task(
            request: Request,
            csrf_token: str = Form(default=""),
            title: str = Form(default=""),
            prompt: str = Form(default=""),
            repository: str = Form(default=""),
            branch: str = Form(default=""),
        ) -> Response:
            session = self._session_from(request)
            if session is None:
                return RedirectResponse("/login", status_code=303)
            if not self._csrf_ok(request, csrf_token):
                return HTMLResponse(self._render_forbidden(), status_code=403)

            title = title.strip()
            prompt = prompt.strip()
            repository = repository.strip() or None
            branch = branch.strip() or None

            error = await self._submit_task(
                title=title,
                prompt=prompt,
                repository=repository,
                branch=branch,
                username=session.username,
            )
            if error:
                return HTMLResponse(
                    await self._render_dashboard(request, session.username, error=error),
                    status_code=400,
                )
            return RedirectResponse("/", status_code=303)

        return router

    async def _submit_task(
        self,
        *,
        title: str,
        prompt: str,
        repository: str | None,
        branch: str | None,
        username: str,
    ) -> str | None:
        """Create a workflow + Sr task. Returns an error message, or None."""
        settings = self._deps.settings
        store = self._deps.store
        if not title:
            return "Укажите название задачи."
        if not prompt:
            return "Укажите описание задачи."
        if len(title) > 200:
            return "Название не должно превышать 200 символов."
        if len(prompt) > 20_000:
            return "Описание не должно превышать 20000 символов."

        allowed = settings.allowed_repositories
        if allowed and repository and repository.lower().removesuffix(".git") not in {
            r.lower().removesuffix(".git") for r in allowed
        }:
            return "Этот репозиторий не разрешён настройкой JR_ALLOWED_REPOSITORIES."
        if repository is None:
            repository = settings.default_repository
        branch = branch or settings.default_branch

        key = f"ui-{secrets.token_hex(12)}"
        try:
            workflow = await store.create_workflow(
                name=title, idempotency_key=key, tags={"origin": "dashboard"}
            )
            task, _created = await store.create_task(
                workflow_id=workflow.id,
                role=TaskRole.SR.value,
                title=title,
                prompt=prompt,
                idempotency_key=f"{key}-sr",
                selected_repository=repository,
                selected_branch=branch,
                max_attempts=settings.dispatcher_max_attempts,
                tags={"origin": "dashboard", "created_by": username},
            )
            await store.update_workflow_status(
                workflow.id, WorkflowStatus.RUNNING.value, sr_task_id=task.id
            )
            await store.record_event(
                event_type="task_created_ui",
                workflow_id=workflow.id,
                task_id=task.id,
                message="Sr task created from the dashboard",
                payload={"created_by": username},
            )
        except Exception as exc:  # noqa: BLE001 - surface a safe message
            logger.error("dashboard task creation failed", **safe_extra(error=type(exc).__name__))
            return "Не удалось создать задачу: хранилище недоступно."
        logger.info("dashboard task created", **safe_extra(task_id=task.id))
        return None

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _nav(self, csrf: str = "") -> str:
        return (
            '<a href="/">Задачи</a> '
            '<a href="/docs">API</a> '
            '<form method="post" action="/logout" style="margin:0">'
            f'<input type="hidden" name="csrf_token" value="{_e(csrf)}">'
            '<button type="submit" class="ghost">Выйти</button></form>'
        )

    def _render_login(self, *, error: str | None = None) -> str:
        err = f'<div class="error">{_e(error)}</div>' if error else ""
        body = f"""
<div class="card" style="max-width:420px;margin:8vh auto 0">
  <h2>Вход в панель диспетчера</h2>
  {err}
  <form method="post" action="/login">
    <label for="username">Логин</label>
    <input id="username" name="username" autocomplete="username" required>
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <div style="margin-top:16px"><button type="submit">Войти</button></div>
  </form>
</div>"""
        return _page("Вход", body)

    def _render_disabled(self) -> str:
        body = """
<div class="card" style="max-width:560px;margin:8vh auto 0">
  <h2>Панель отключена</h2>
  <p class="muted">Не заданы <span class="mono">DASHBOARD_USERNAME</span> и
  <span class="mono">DASHBOARD_PASSWORD_HASH</span>, поэтому интерфейс
  недоступен. Это защита по умолчанию: незаполненная конфигурация не
  открывает панель без пароля.</p>
</div>"""
        return _page("Панель отключена", body)

    def _render_forbidden(self) -> str:
        body = """
<div class="card" style="max-width:560px;margin:8vh auto 0">
  <h2>Запрос отклонён</h2>
  <p class="muted">Проверка CSRF-токена не пройдена. Обновите страницу и
  повторите действие.</p>
  <p><a href="/">Вернуться к задачам</a></p>
</div>"""
        return _page("Запрос отклонён", body)

    async def _render_dashboard(
        self, request: Request, username: str, *, error: str | None = None
    ) -> str:
        store = self._deps.store
        settings = self._deps.settings
        csrf = ""
        if self._signer is not None:
            csrf = self._signer.csrf_token(request.cookies.get(SESSION_COOKIE))

        workflows = await store.list_workflows()
        # Newest first; the store returns insertion order.
        workflows = list(reversed(workflows))[:50]

        rows: list[str] = []
        for wf in workflows:
            tasks = await store.list_tasks_for_workflow(wf.id)
            sr = next((t for t in tasks if t.role == TaskRole.SR.value), None)
            jr = [t for t in tasks if t.role == TaskRole.JR.value]
            done = sum(1 for t in jr if t.status == TaskStatus.COMPLETED.value)
            rows.append(
                f"""<tr>
  <td><a href="/workflows/{_e(wf.id)}">{_e(wf.name)}</a>
      <div class="muted mono">{_e(wf.id)}</div></td>
  <td>{_status_badge(wf.status)}</td>
  <td>{_e(sr.status) if sr else '<span class="muted">-</span>'}</td>
  <td>{done}/{len(jr)}</td>
  <td class="muted">{_fmt_dt(wf.created_at)}</td>
</tr>"""
            )
        table = (
            "<table><thead><tr><th>Задача</th><th>Статус</th><th>Sr</th>"
            "<th>Jr</th><th>Создано</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            if rows
            else '<div class="empty">Задач пока нет. Создайте первую ниже.</div>'
        )

        db_ok = False
        try:
            db_ok = bool(await store.healthcheck())
        except Exception:  # noqa: BLE001
            db_ok = False
        dispatcher = self._deps.dispatcher
        cycle_ok = bool(dispatcher and dispatcher.last_cycle_succeeded)
        err = f'<div class="error">{_e(error)}</div>' if error else ""

        body = f"""
{err}
<div class="grid">
  <div class="stat"><div class="n">{len(workflows)}</div>
    <div class="l">Задач (последние 50)</div></div>
  <div class="stat"><div class="n">{"✓" if db_ok else "✕"}</div>
    <div class="l">База данных</div></div>
  <div class="stat"><div class="n">{"✓" if cycle_ok else "…"}</div>
    <div class="l">Цикл диспетчера</div></div>
  <div class="stat"><div class="n">{"✓" if settings.resolved_api_key else "✕"}</div>
    <div class="l">Ключ OpenHands</div></div>
</div>

<div class="card">
  <h2>Новая задача</h2>
  <form method="post" action="/tasks">
    <input type="hidden" name="csrf_token" value="{_e(csrf)}">
    <label for="title">Название</label>
    <input id="title" name="title" required maxlength="200"
           placeholder="Например: описать архитектуру проекта">
    <label for="prompt">Что должен сделать Sr</label>
    <textarea id="prompt" name="prompt" required
      placeholder="Опишите задачу для планирующего агента"></textarea>
    <div class="grid">
      <div>
        <label for="repository">Репозиторий (необязательно)</label>
        <input id="repository" name="repository" placeholder="owner/repo">
      </div>
      <div>
        <label for="branch">Ветка (необязательно)</label>
        <input id="branch" name="branch" placeholder="main">
      </div>
    </div>
    <div style="margin-top:16px"><button type="submit">Создать задачу</button></div>
  </form>
</div>

<div class="card">
  <h2>Задачи</h2>
  {table}
</div>

<p class="muted">Вы вошли как <b>{_e(username)}</b>.</p>"""
        return _page("Панель диспетчера", body, nav=self._nav(csrf))

    def workflow_router(self) -> APIRouter:
        """The per-workflow detail page (kept separate for readability)."""
        router = APIRouter()

        @router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
        async def workflow_detail(request: Request, workflow_id: str) -> Response:
            session = self._session_from(request)
            if session is None:
                return RedirectResponse("/login", status_code=303)
            store = self._deps.store
            csrf = ""
            if self._signer is not None:
                csrf = self._signer.csrf_token(request.cookies.get(SESSION_COOKIE))
            wf = await store.get_workflow(workflow_id)
            if wf is None:
                return HTMLResponse(
                    _page(
                        "Не найдено",
                        '<div class="card"><h2>Задача не найдена</h2>'
                        '<p><a href="/">К списку задач</a></p></div>',
                        nav=self._nav(csrf),
                    ),
                    status_code=404,
                )
            tasks = await store.list_tasks_for_workflow(wf.id)
            events = await store.list_events(workflow_id=wf.id, limit=100)

            rows = []
            for t in tasks:
                summary = t.result_summary or t.last_error or ""
                if summary:
                    summary = scrub_text(
                        summary, self._deps.settings.redaction_secrets()
                    )
                prompt = scrub_text(
                    t.prompt or "", self._deps.settings.redaction_secrets()
                )
                rows.append(
                    f"""<tr>
  <td>{_e(t.role)}</td>
  <td>{_status_badge(t.status)}</td>
  <td class="mono">{_e(t.id)}</td>
  <td>{t.attempts}/{t.max_attempts}</td>
  <td class="mono">{_e(t.app_conversation_id) or '<span class="muted">-</span>'}</td>
  <td>{_e(prompt)[:400]}</td>
  <td>{_e(summary)[:400]}</td>
</tr>"""
                )
            tasks_table = (
                "<table><thead><tr><th>Роль</th><th>Статус</th><th>ID</th>"
                "<th>Попытки</th><th>Беседа</th><th>Задача</th><th>Результат / ошибка</th>"
                "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
                if rows
                else '<div class="empty">Задач нет.</div>'
            )

            ev_rows = []
            for ev in reversed(events):
                msg = scrub_text(
                    ev.message or "", self._deps.settings.redaction_secrets()
                )
                ev_rows.append(
                    f"""<tr><td class="muted">{_fmt_dt(ev.created_at)}</td>
  <td class="mono">{_e(ev.event_type)}</td><td>{_e(msg)}</td></tr>"""
                )
            ev_table = (
                "<table><thead><tr><th>Время</th><th>Событие</th><th>Сообщение</th>"
                "</tr></thead><tbody>" + "".join(ev_rows) + "</tbody></table>"
                if ev_rows
                else '<div class="empty">Событий нет.</div>'
            )

            body = f"""
<p><a href="/">← К списку задач</a></p>
<div class="card">
  <h2>{_e(wf.name)}</h2>
  <div class="row"><span class="muted">Статус:</span> {_status_badge(wf.status)}</div>
  <div class="row muted mono">{_e(wf.id)}</div>
  <div class="row muted">Создано: {_fmt_dt(wf.created_at)}</div>
  {f'<div class="row muted">Итог: {_e(scrub_text(wf.result_summary, self._deps.settings.redaction_secrets()))}</div>' if wf.result_summary else ''}
</div>
<div class="card"><h2>Задачи workflow</h2>{tasks_table}</div>
<div class="card"><h2>Журнал событий</h2>{ev_table}</div>"""
            return HTMLResponse(_page(wf.name, body, nav=self._nav(csrf)))

        return router


def build_dashboard(deps: DashboardDeps) -> tuple[APIRouter, APIRouter]:
    """Return ``(dashboard_router, workflow_router)`` ready to include."""
    dashboard = Dashboard(deps)
    return dashboard.router(), dashboard.workflow_router()
