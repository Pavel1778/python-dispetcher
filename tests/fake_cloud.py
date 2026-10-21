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
        #: When > 0, a start task stays ``WORKING`` (and reports **no**
        #: ``app_conversation_id``) until this many start-task polls have
        #: happened. This is the documented two-phase contract: the id is only
        #: meaningful at ``READY``, so a caller that returns early binds a
        #: conversation that is not up yet.
        self.start_ready_after_polls = 0
        #: start-task id -> the conversation it will produce.
        self.start_task_cids: dict[str, str] = {}
        #: Conversations whose start task has not reached READY yet. The real
        #: search index does not list them, so a tag search cannot be used to
        #: adopt a conversation that is still starting.
        self.unready_conversations: set[str] = set()
        #: When > 0, the next N conversation starts fail with this status code
        #: (e.g. 429). Lets a test drive the workflow-level rate-limit path.
        self.start_status = 0
        self.start_status_times = 0
        #: When set, ``app-conversations/search`` fails with this status code.
        #: Lets a test drive the "orphan lookup is down" path.
        self.search_status = 0
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
            # A conversation that is still starting is not addressable: the id
            # is only meaningful at READY. Returning it here would let a caller
            # treat "started" as "usable", which is the bug under test.
            out = []
            for i in ids.split(","):
                if not i:
                    continue
                if i in self.cloud.unready_conversations:
                    out.append(None)
                else:
                    out.append(self.cloud.conversations.get(i))
            return self._json(200, out)

        if path == "/api/v1/app-conversations/search":
            if self.cloud.search_status:
                return self._json(self.cloud.search_status, {"detail": "unavailable"})
            items = [c for c in self.cloud.conversations.values()
                     if c["id"] not in self.cloud.unready_conversations]
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
                        if st["status"] == "READY":
                            # The id appears only once READY, as on the live API.
                            st["app_conversation_id"] = st.get("_cid")
                    # The real API only populates ``app_conversation_id`` once the
                    # start task is READY; before that it is absent. Mirror that
                    # so a caller cannot mistake "started" for "conversation up".
                    if (self.cloud.start_ready_after_polls
                            and st["_polls"] <= self.cloud.start_ready_after_polls
                            and not self.cloud.start_fail):
                        st = {**st, "app_conversation_id": None,
                              "status": "WORKING"}
                    elif st["_polls"] > self.cloud.start_ready_after_polls:
                        cid = self.cloud.start_task_cids.get(i)
                        if cid:
                            self.cloud.unready_conversations.discard(cid)
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
            if self.cloud.start_fail:
                # The start request was accepted but the start task terminally
                # failed: no usable conversation exists. This mirrors an ERROR
                # start task (a rejected sandbox, a bad repository), and is the
                # case where a caller must NOT record a conversation id.
                st_id = str(uuid.uuid4())
                self.cloud.start_tasks[st_id] = {
                    "id": st_id,
                    "status": "ERROR",
                    "app_conversation_id": None,
                    "sandbox_id": None,
                    "agent_server_url": None,
                    "detail": "sandbox provisioning failed",
                    "_polls": 0,
                }
                return self._json(200, self.cloud.start_tasks[st_id])
            cid = self.cloud.make_conversation(
                # The live server ignores ``title`` on create and derives its own
                # from the first user message (``autotitle``). Mirroring that is
                # what lets a test catch a name that is never applied: a fake
                # that honoured ``title`` here made the defect invisible.
                title=f"Conversation {uuid.uuid4().hex}",
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
            # ``app_conversation_id`` is present from the first response, but it
            # is only *meaningful* at READY (see the GET handler): the id exists
            # while the sandbox is still provisioning. A caller that binds it
            # before READY binds a conversation that is not up yet.
            self.cloud.start_tasks[st_id] = {
                "id": st_id,
                "status": "WORKING",
                "app_conversation_id": cid,
                "_cid": cid,
                "sandbox_id": self.cloud.conversations[cid]["sandbox_id"],
                "agent_server_url": None,
                "detail": None,
                "_polls": 0,
            }
            self.cloud.start_task_cids[st_id] = cid
            if self.cloud.start_ready_after_polls:
                self.cloud.unready_conversations.add(cid)
            return self._json(200, self.cloud.start_tasks[st_id])

        if path.endswith("/send-message"):
            cid = path.split("/")[4]
            body = self._read_body()
            text = "".join(c.get("text", "") for c in (body.get("content") or []))
            self.cloud.messages.setdefault(cid, []).append(text)
            return self._json(200, {"success": True, "sandbox_status": "RUNNING",
                                    "message": None})

        return self._json(404, {"detail": "not found"})

    # ----------------------------------------------------------------- PATCH
    def do_PATCH(self):
        if not self._auth_ok():
            return self._json(401, {"detail": "unauthorized"})
        path = self.path.split("?")[0]
        self.cloud.calls.append(("PATCH", path))
        if (path.startswith("/api/v1/app-conversations/")
                and path.count("/") == 4):
            cid = path.split("/")[4]
            conv = self.cloud.conversations.get(cid)
            if conv is None:
                return self._json(404, {"detail": "not found"})
            body = self._read_body()
            # The live server applies only the fields that are sent.
            if body.get("title") is not None:
                conv["title"] = body["title"]
            return self._json(200, conv)
        return self._json(404, {"detail": "not found"})

    # ---------------------------------------------------------------- DELETE
    def do_DELETE(self):
        if not self._auth_ok():
            return self._json(401, {"detail": "unauthorized"})
        path = self.path.split("?")[0]
        self.cloud.calls.append(("DELETE", path))
        if (path.startswith("/api/v1/app-conversations/")
                and path.count("/") == 4):
            cid = path.split("/")[4]
            if cid not in self.cloud.conversations:
                return self._json(404, {"detail": "not found"})
            del self.cloud.conversations[cid]
            self.cloud.events.pop(cid, None)
            return self._json(200, {"success": True})
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
