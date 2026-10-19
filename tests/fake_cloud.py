"""A real local HTTP server that speaks the OpenHands Cloud V1 surface.

Tests drive the dispatcher over real sockets against this server. The Cloud API
surface, status codes and JSON match the live schema (see CLOUD-INTEGRATION-MAP
and the live OpenAPI document); only the LLM behind a conversation is faked.

This is not a mock of ``CloudClient``: the client under test makes real HTTP
requests, and the server enforces the same shapes the real one does, including
the two-phase start (start task -> READY -> app_conversation_id).
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCloud:
    """Mutable state backing the fake server."""

    def __init__(self):
        self.start_tasks: dict[str, dict] = {}
        self.conversations: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.messages: dict[str, list[str]] = {}
        self.client_tools: dict[str, list[dict]] = {}
        # The start-request fields the server actually received, per conversation.
        self.start_bodies: dict[str, dict] = {}
        # Account-level configuration the fake reports. Not secret values.
        self.settings: dict = {
            "llm_api_key_set": True,
            "active_agent_profile_id": None,
            "agent_settings": {"mcp_config": {"playwright": {"enabled": True},
                                              "fetch": {"enabled": True}}},
            "registered_marketplaces": [{"name": "m1"}],
            "inherited_marketplaces": [],
            "disabled_skills": [],
            "provider_tokens_set": {"github": None},
        }
        self.agent_profiles: dict = {"profiles": [], "active_agent_profile_id": None}
        # Skills the fake reports for any conversation (name -> present).
        self.skills: list[str] = ["work_hosts"]
        # Controls: how many polls a start task needs before READY.
        self.start_polls_required = 0
        self.start_fail = False
        #: When > 0, the next N conversation starts fail with this status code
        #: (e.g. 429). Lets a test drive the workflow-level rate-limit path.
        self.start_status = 0
        self.start_status_times = 0
        self.calls: list[tuple[str, str]] = []

    def make_conversation(self, *, title, repo=None, branch=None,
                          execution_status="running", tags=None) -> str:
        cid = str(uuid.uuid4())
        self.conversations[cid] = {
            "id": cid,
            "title": title,
            "sandbox_id": "sbx-" + cid[:8],
            "execution_status": execution_status,
            "sandbox_status": "RUNNING",
            "selected_repository": repo,
            "selected_branch": branch,
            "conversation_url": f"https://cloud.example/api/conversations/{cid}",
            "sub_conversation_ids": [],
            "tags": dict(tags or {}),
        }
        self.events[cid] = []
        self.messages[cid] = []
        return cid

    def add_assistant_message(self, cid: str, text: str) -> None:
        self.events[cid].append(
            {"kind": "MessageEvent",
             "llm_message": {"role": "assistant",
                             "content": [{"type": "text", "text": text}]}}
        )

    def add_client_tool_action(self, cid: str, name: str, args: dict,
                               event_id: str | None = None) -> str:
        """Append an ActionEvent as the live server emits for a client tool call.

        The live server sets ``tool_name`` and an ``action`` payload that carries
        the arguments plus a ``kind`` of ``ClientAction_<name>``; the id is
        stable across polls, which is what makes execution exactly-once.
        """
        eid = event_id or str(uuid.uuid4())
        self.events[cid].append(
            {
                "id": eid,
                "kind": "ActionEvent",
                "tool_name": name,
                "action": {**args, "kind": f"ClientAction_{name}"},
                "source": "agent",
            }
        )
        return eid


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeCloud/1.0"

    def log_message(self, *a):  # silence
        pass

    @property
    def cloud(self) -> FakeCloud:
        return self.server.cloud  # type: ignore[attr-defined]

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self._auth_ok():
            return self._json(401, {"detail": "unauthorized"})
        path = self.path.split("?")[0]
        self.cloud.calls.append(("GET", path))

        if path == "/api/v1/app-conversations/count":
            return self._json(200, {"count": len(self.cloud.conversations)})

        if path == "/api/v1/app-conversations":
            ids = self._query("ids")
            out = [self.cloud.conversations.get(i) for i in ids.split(",") if i]
            return self._json(200, out)

        if path == "/api/v1/app-conversations/search":
            items = list(self.cloud.conversations.values())
            # tags__contains is a repeatable key=value filter; all must match.
            for raw in self._query_all("tags__contains"):
                key, _, value = raw.partition("=")
                items = [c for c in items if (c.get("tags") or {}).get(key) == value]
            return self._json(200, {"items": items, "next_page_id": None})

        if path == "/api/v1/settings":
            return self._json(200, self.cloud.settings)

        if path == "/api/agent-profiles":
            return self._json(200, self.cloud.agent_profiles)

        if (path.startswith("/api/v1/app-conversations/")
                and path.endswith("/skills")):
            cid = path.split("/")[4]
            if cid not in self.cloud.conversations:
                return self._json(404, {"detail": "not found"})
            return self._json(200, {"skills": [{"name": n} for n in self.cloud.skills]})

        if path == "/api/v1/app-conversations/start-tasks":
            ids = [i for i in self._query("ids").split(",") if i]
            out = []
            for i in ids:
                st = self.cloud.start_tasks.get(i)
                if st is not None:
                    st["_polls"] = st.get("_polls", 0) + 1
                    if st["_polls"] > self.cloud.start_polls_required:
                        st["status"] = "ERROR" if self.cloud.start_fail else "READY"
                out.append(st)
            return self._json(200, out)

        if path.startswith("/api/v1/conversation/") and path.endswith("/events/search"):
            cid = path.split("/")[4]
            events = self.cloud.events.get(cid, [])
            page = int(self._query("page_id") or 0)
            limit = int(self._query("limit") or 100)
            start = page * limit
            chunk = events[start:start + limit]
            nxt = str(page + 1) if start + limit < len(events) else None
            return self._json(200, {"items": chunk, "next_page_id": nxt})
        return self._json(404, {"detail": "not found"})

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        if not self._auth_ok():
            return self._json(401, {"detail": "unauthorized"})
        path = self.path.split("?")[0]
        self.cloud.calls.append(("POST", path))

        if path == "/api/v1/app-conversations":
            if self.cloud.start_status_times > 0:
                code = self.cloud.start_status
                self.cloud.start_status_times -= 1
                return self._json(code, {"detail": "capacity full"})
            body = self._read_body()
            msg = body.get("initial_message") or {}
            text = ""
            for c in (msg.get("content") or []):
                text += c.get("text", "")
            cid = self.cloud.make_conversation(
                title=body.get("title") or "conversation",
                repo=body.get("selected_repository"),
                branch=body.get("selected_branch"),
                tags=body.get("tags"),
            )
            self.cloud.client_tools[cid] = body.get("client_tools") or []
            self.cloud.start_bodies[cid] = body
            self.cloud.events[cid].append(
                {"kind": "MessageEvent",
                 "llm_message": {"role": "user",
                                 "content": [{"type": "text", "text": text}]}}
            )
            st_id = str(uuid.uuid4())
            self.cloud.start_tasks[st_id] = {
                "id": st_id,
                "status": "WORKING",
                "app_conversation_id": cid,
                "sandbox_id": self.cloud.conversations[cid]["sandbox_id"],
                "agent_server_url": None,
                "detail": None,
                "_polls": 0,
            }
            return self._json(200, self.cloud.start_tasks[st_id])

        if path.endswith("/send-message"):
            cid = path.split("/")[4]
            body = self._read_body()
            text = "".join(c.get("text", "") for c in (body.get("content") or []))
            self.cloud.messages.setdefault(cid, []).append(text)
            return self._json(200, {"success": True, "sandbox_status": "RUNNING",
                                    "message": None})

        return self._json(404, {"detail": "not found"})

    # --------------------------------------------------------------- helpers
    def _query(self, key: str) -> str:
        if "?" not in self.path:
            return ""
        qs = self.path.split("?", 1)[1]
        for pair in qs.split("&"):
            k, _, v = pair.partition("=")
            if k == key:
                return urllib.parse.unquote(v)
        return ""

    def _query_all(self, key: str) -> list[str]:
        if "?" not in self.path:
            return []
        out = []
        for pair in self.path.split("?", 1)[1].split("&"):
            k, _, v = pair.partition("=")
            if k == key:
                out.append(urllib.parse.unquote(v))
        return out


class FakeCloudServer:
    """Context manager starting the fake on an ephemeral port."""

    def __init__(self, cloud: FakeCloud | None = None):
        self.cloud = cloud or FakeCloud()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.cloud = self.cloud  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeCloudServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
