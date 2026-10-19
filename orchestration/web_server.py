"""Web control panel for the Sr/Jr dispatcher (stdlib-only HTTP server).

The user types a large task on the page. The server creates a workflow, starts a
real Sr conversation on OpenHands Cloud (with the Sr client tools attached), and
a background loop drives both sides:

* :class:`~orchestration.sr_runtime.SrRuntime` handles the Sr's tool calls,
  which create and manage Jr tasks.
* :class:`~orchestration.cloud_dispatcher.CloudDispatcher` runs the Jr tasks as
  their own Cloud conversations.

Everything is durable in PostgreSQL, so a restart resumes the same workflows.
The API key is read from the environment and never sent to the browser.

Run::

    SUPABASE_DB_URL="postgresql://..." OPENHANDS_API_KEY="..." \
        python -m orchestration.web_server --port 8000

Endpoints:
    GET  /                      the page
    GET  /api/config            whether a Cloud key is configured
    GET  /api/repositories      the repository allowlist
    POST /api/repositories      add/update a repository
    POST /api/workflows         {goal, repository?, branch?} -> start Sr
    GET  /api/workflows         list workflows
    GET  /api/workflows/<id>    one workflow with its tasks and tool calls
    POST /api/workflows/<id>/message   send a message to the Sr conversation
    GET  /api/health            store + Cloud reachability
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .cloud_client import CloudClient, CloudError
from .cloud_dispatcher import CloudDispatcher
from .cloud_store import (
    InvalidTransition,
    NotFound,
    PostgresStore,
    StoreError,
)
from .sr_runtime import SrRuntime
from .sr_tools import SrTools

log = logging.getLogger("web_server")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sr/Jr dispatcher</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --fg:#e6e8eb; --mut:#9aa4b2;
          --acc:#4f8cff; --ok:#3fb950; --bad:#f85149; --warn:#d29922; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:20px 24px; border-bottom:1px solid #262b33;
           display:flex; align-items:baseline; gap:12px; }
  h1 { font-size:18px; margin:0; }
  .mut { color:var(--mut); font-size:13px; }
  main { max-width:1000px; margin:0 auto; padding:24px; }
  .card { background:var(--card); border:1px solid #262b33; border-radius:10px;
          padding:16px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:var(--mut); margin:8px 0 4px; }
  textarea,input,select { width:100%; background:#0d0f13; color:var(--fg);
          border:1px solid #303743; border-radius:8px; padding:10px;
          font:inherit; }
  textarea { min-height:90px; resize:vertical; }
  button { background:var(--acc); color:#fff; border:0; border-radius:8px;
           padding:10px 16px; font:inherit; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .row > * { flex:1 1 200px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #262b33; }
  th { color:var(--mut); font-weight:500; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px;
          font-size:12px; background:#222833; }
  .pill.completed { background:rgba(63,185,80,.15); color:var(--ok); }
  .pill.failed,.pill.dead_letter { background:rgba(248,81,73,.15); color:var(--bad); }
  .pill.running,.pill.verifying,.pill.assigned { background:rgba(79,140,255,.15); color:var(--acc); }
  .pill.blocked { background:rgba(210,153,34,.15); color:var(--warn); }
  pre { background:#0d0f13; border:1px solid #262b33; border-radius:8px;
        padding:10px; overflow:auto; max-height:320px; font-size:12px; }
  .err { color:var(--bad); }
  .ok { color:var(--ok); }
</style>
</head>
<body>
<header>
  <h1>Sr/Jr dispatcher</h1>
  <span class="mut" id="cfg"></span>
</header>
<main>
  <div class="card">
    <h3 style="margin:0 0 4px">New task</h3>
    <p class="mut" style="margin:0">The Sr agent plans and delegates to Jr agents on OpenHands Cloud.</p>
    <form id="form">
      <label for="goal">Task</label>
      <textarea id="goal" required placeholder="Describe the whole job..."></textarea>
      <div class="row">
        <div>
          <label for="repo">Repository (optional)</label>
          <select id="repo"><option value="">- none (analysis only) -</option></select>
        </div>
        <div>
          <label for="branch">Branch (optional)</label>
          <input id="branch" placeholder="main">
        </div>
      </div>
      <div style="margin-top:12px">
        <button type="submit" id="go">Start Sr</button>
        <span class="mut" id="msg" style="margin-left:10px"></span>
      </div>
    </form>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px">Workflows</h3>
    <table>
      <thead><tr><th>Goal</th><th>State</th><th>Sr</th><th>Tasks</th><th></th></tr></thead>
      <tbody id="wfs"><tr><td colspan="5" class="mut">loading...</td></tr></tbody>
    </table>
  </div>

  <div class="card" id="detail" style="display:none">
    <h3 id="dtitle" style="margin:0 0 8px"></h3>
    <div id="dbody"></div>
  </div>
</main>
<script>
const $ = (s) => document.querySelector(s);
let current = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const t = await r.text();
  let d; try { d = t ? JSON.parse(t) : {}; } catch { d = {raw:t}; }
  if (!r.ok) throw new Error(d.error || r.status);
  return d;
}

async function loadConfig() {
  const c = await api('/api/config');
  $('#cfg').textContent = c.cloud_key ? 'Cloud key configured' : 'NO CLOUD KEY - cannot start conversations';
  const r = await api('/api/repositories');
  const sel = $('#repo');
  for (const repo of r.repositories) {
    const o = document.createElement('option');
    o.value = repo.full_name;
    o.textContent = repo.full_name;
    sel.appendChild(o);
  }
}

async function loadWorkflows() {
  const { workflows } = await api('/api/workflows');
  const tb = $('#wfs');
  tb.innerHTML = '';
  if (!workflows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="mut">no workflows yet</td></tr>';
    return;
  }
  for (const w of workflows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(w.goal).slice(0,70)}</td>
      <td><span class="pill ${w.state}">${w.state}</span></td>
      <td class="mut">${w.sr_status || '-'}</td>
      <td>${w.task_count}</td>
      <td><button data-id="${w.id}" class="view">open</button></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('.view').forEach(b => b.onclick = () => openDetail(b.dataset.id));
}

function esc(s){ return String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function openDetail(id) {
  current = id;
  const d = await api('/api/workflows/' + id);
  $('#detail').style.display = 'block';
  $('#dtitle').textContent = d.workflow.goal;
  let html = `<p class="mut">Sr conversation: ${d.workflow.sr_conversation_id || 'starting...'}
    &middot; state ${d.workflow.state}</p>
    <div class="row"><input id="msgto" placeholder="Message to Sr...">
      <button id="sendmsg" style="flex:0 0 auto">Send</button></div>
    <h4>Tasks</h4><table><thead><tr><th>Goal</th><th>State</th><th>Attempt</th>
      <th>Repository</th><th>Result</th></tr></thead><tbody>`;
  for (const t of d.tasks) {
    html += `<tr><td>${esc(t.goal)}</td>
      <td><span class="pill ${t.state}">${t.state}</span></td>
      <td>${t.attempt}</td><td class="mut">${esc(t.repository||'-')}</td>
      <td class="mut">${esc((t.result||'').slice(0,120))}</td></tr>`;
  }
  html += '</tbody></table><h4>Sr tool calls</h4>';
  if (!d.tool_calls.length) html += '<p class="mut">none yet</p>';
  for (const c of d.tool_calls) {
    html += `<pre>${esc(c.name)} ${esc(JSON.stringify(c.args))}
  -> ${c.is_error ? 'ERROR' : 'ok'} ${esc(JSON.stringify(c.result))}</pre>`;
  }
  $('#dbody').innerHTML = html;
  $('#sendmsg').onclick = async () => {
    const text = $('#msgto').value.trim();
    if (!text) return;
    await api('/api/workflows/' + id + '/message', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text})
    });
    $('#msgto').value = '';
    openDetail(id);
  };
}

$('#form').onsubmit = async (e) => {
  e.preventDefault();
  $('#go').disabled = true; $('#msg').textContent = 'starting Sr...';
  try {
    const body = { goal: $('#goal').value.trim() };
    if ($('#repo').value) body.repository = $('#repo').value;
    if ($('#branch').value.trim()) body.branch = $('#branch').value.trim();
    const d = await api('/api/workflows', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    $('#msg').textContent = 'Sr started: ' + d.workflow_id;
    $('#goal').value = '';
    loadWorkflows();
  } catch (err) {
    $('#msg').innerHTML = '<span class="err">' + esc(err.message) + '</span>';
  } finally {
    $('#go').disabled = false;
  }
};

loadConfig().catch(e => $('#cfg').innerHTML = '<span class="err">'+esc(e.message)+'</span>');
loadWorkflows();
setInterval(loadWorkflows, 5000);
</script>
</body>
</html>
"""


class App:
    """Wires the store, Cloud client, Sr runtime and Jr dispatcher together."""

    def __init__(self, dsn: str, *, worker_id: str = "web", poll_interval: float = 5.0):
        self.store = PostgresStore(dsn)
        self.client = CloudClient()
        self.dispatcher = CloudDispatcher(
            self.store, self.client, worker_id=worker_id,
            start_poll_interval=2.0, start_poll_attempts=40,
        )
        self.runtime = SrRuntime(
            self.store, self.client, SrTools(self.store, self.dispatcher),
            worker_id=worker_id, start_poll_interval=2.0, start_poll_attempts=40,
        )
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self.last_tick: dict[str, Any] = {}

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        self._loop = threading.Thread(target=self._run, daemon=True)
        self._loop.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.join(timeout=5)
        self.store.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sr = self.runtime.tick().as_dict()
                jr = self.dispatcher.tick().as_dict()
                self.last_tick = {"at": time.time(), "sr": sr, "jr": jr}
            except CloudError as e:
                self.last_tick = {"at": time.time(), "cloud_error": str(e)}
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.exception("tick failed")
                self.last_tick = {"at": time.time(), "error": f"{type(e).__name__}: {e}"}
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------- actions
    def start_workflow(self, goal: str, repository: str | None = None,
                       branch: str | None = None) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal is required")
        if repository is not None and self.store.allowed_repository(repository) is None:
            raise ValueError(f"repository not allowed: {repository}")
        workflow_id = self.store.create_workflow(goal)
        try:
            conversation_id = self.runtime.ensure_sr(workflow_id)
        except CloudError as e:
            # Keep the workflow so the user can see it failed to start Sr.
            self.store.bind_sr(workflow_id, status=f"error: {e}")
            raise
        return {"workflow_id": workflow_id, "sr_conversation_id": conversation_id}

    def workflow_detail(self, workflow_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", workflow_id):
            raise NotFound(workflow_id)
        wf = self.store.sr_for(workflow_id)
        if wf is None:
            raise NotFound(workflow_id)
        workflows = {w["id"]: w for w in self.store.list_workflows()}
        tasks = self.store.list_tasks(workflow_id=workflow_id)
        return {
            "workflow": {
                "id": workflow_id,
                "goal": wf["goal"],
                "state": workflows.get(workflow_id, {}).get("state"),
                "sr_conversation_id": wf.get("sr_conversation_id"),
                "sr_status": wf.get("sr_status"),
            },
            "tasks": [
                {
                    "id": t.id, "goal": t.goal, "state": t.state.value,
                    "attempt": t.attempt, "repository": t.selected_repository,
                    "branch": t.selected_branch, "result": t.result,
                }
                for t in tasks
            ],
            "tool_calls": self.store.tool_calls(workflow_id=workflow_id),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "SrJrDispatcher/1.0"

    def log_message(self, *a: Any) -> None:  # keep the console quiet
        pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------- helpers
    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON")

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self._html(200, PAGE)
            if path == "/api/config":
                return self._json(200, {
                    "cloud_key": bool(os.environ.get("OPENHANDS_API_KEY")),
                    "cloud_url": self.app.client.base_url,
                })
            if path == "/api/repositories":
                return self._json(200, {
                    "repositories": self.app.store.list_repositories()
                })
            if path == "/api/workflows":
                out = []
                for w in self.app.store.list_workflows():
                    tasks = self.app.store.list_tasks(workflow_id=w["id"])
                    out.append({
                        "id": w["id"], "goal": w["goal"], "state": w["state"],
                        "sr_status": w.get("sr_status"),
                        "sr_conversation_id": w.get("sr_conversation_id"),
                        "task_count": len(tasks),
                    })
                return self._json(200, {"workflows": out})
            if path.startswith("/api/workflows/"):
                workflow_id = path.split("/")[3]
                return self._json(200, self.app.workflow_detail(workflow_id))
            if path == "/api/health":
                return self._json(200, {
                    "store": True,
                    "cloud": self.app.client.health(),
                    "last_tick": self.app.last_tick,
                })
            return self._json(404, {"error": "not found"})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", path)
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/repositories":
                name = (body.get("full_name") or "").strip()
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
                    return self._json(400, {"error": "full_name must be owner/name"})
                rid = self.app.store.add_repository(
                    name,
                    provider=body.get("provider") or "github",
                    default_branch=body.get("default_branch") or "main",
                    allowed_branches=body.get("allowed_branches") or [],
                )
                return self._json(200, {"repository_id": rid})
            if path == "/api/workflows":
                out = self.app.start_workflow(
                    body.get("goal") or "",
                    body.get("repository"),
                    body.get("branch"),
                )
                return self._json(200, out)
            if path.startswith("/api/workflows/") and path.endswith("/message"):
                workflow_id = path.split("/")[3]
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(400, {"error": "text is required"})
                sr = self.app.store.sr_for(workflow_id)
                if not sr or not sr.get("sr_conversation_id"):
                    return self._json(409, {"error": "Sr conversation not ready"})
                ok = self.app.client.send_message(sr["sr_conversation_id"], text)
                return self._json(200, {"sent": ok})
            return self._json(404, {"error": "not found"})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except CloudError as e:
            return self._json(502, {"error": f"cloud: {e}"})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", path)
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


class WebServer:
    """Context manager for the HTTP server and its background loop."""

    def __init__(self, app: App, host: str = "127.0.0.1", port: int = 8000):
        self.app = app
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.app = app  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.app.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self.app.stop()

    def __enter__(self) -> "WebServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sr/Jr dispatcher web server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("set SUPABASE_DB_URL")
        return 2
    try:
        app = App(dsn, poll_interval=args.poll_interval)
    except StoreError as e:
        print(f"store error: {e}")
        return 2
    server = WebServer(app, args.host, args.port)
    server.start()
    print(f"listening on http://{args.host}:{server.port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
