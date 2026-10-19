"""A local stand-in for the OpenHands agent-server.

This is a real HTTP server speaking the confirmed v1.50.1 surface, not a mock
of the client. Tests drive the dispatcher through real sockets and real JSON,
so serialization, status codes and error paths are exercised for real.

The only thing it fakes is the LLM: a conversation "finishes" when the test
tells it to, which keeps the suite offline and deterministic.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class FakeConversation:
    id: str
    working_dir: str
    title: str | None = None
    status: str = "idle"
    final_response: str | None = None
    events: int = 0
    runs: int = 0


@dataclass
class FakeRuntime:
    """State and fault injection for the stand-in server."""

    conversations: dict[str, FakeConversation] = field(default_factory=dict)
    created_count: int = 0
    #: When > 0, every request fails with 503 this many times.
    fail_times: int = 0
    #: When True, the whole server refuses connections.
    down: bool = False
    #: Status code for the next create (e.g. 429).
    create_status: int = 0
    #: When set, ConversationErrorEvent search returns this classification.
    error_classification: dict[str, Any] | None = None
    require_key: bool = True
    expected_key: str = "test-key"
    seen_keys: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def finish(self, conversation_id: str, response: str | None) -> None:
        with self.lock:
            c = self.conversations[conversation_id]
            c.status = "finished"
            c.final_response = response

    def set_status(self, conversation_id: str, status: str) -> None:
        with self.lock:
            self.conversations[conversation_id].status = status

    def drop(self, conversation_id: str) -> None:
        with self.lock:
            self.conversations.pop(conversation_id, None)


class _Handler(BaseHTTPRequestHandler):
    runtime: FakeRuntime

    def log_message(self, *args: Any) -> None:  # silence test noise
        pass

    # ------------------------------------------------------------- helpers
    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def _authorised(self) -> bool:
        key = self.headers.get("X-Session-API-Key", "")
        self.runtime.seen_keys.append(key)
        if not self.runtime.require_key:
            return True
        return key == self.runtime.expected_key

    def _guard(self) -> bool:
        rt = self.runtime
        with rt.lock:
            if rt.fail_times > 0:
                rt.fail_times -= 1
                self._json(503, {"detail": "temporarily unavailable"})
                return False
        if not self._authorised():
            self._json(401, {"detail": "Missing or invalid X-Session-API-Key"})
            return False
        return True

    # --------------------------------------------------------------- routes
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if not self._guard():
            return
        parts = [p for p in path.split("/") if p]

        # /api/settings -> agent_settings required by the live create endpoint
        if parts == ["api", "settings"]:
            self._json(200, {"agent_settings": {"agent": "CodeActAgent"}})
            return

        # /api/conversations/{id}/events/search
        if len(parts) == 5 and parts[3] == "events" and parts[4] == "search":
            c = self.runtime.conversations.get(parts[2])
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            cls = self.runtime.error_classification
            items = []
            if cls is not None:
                items.append(
                    {"kind": "ConversationErrorEvent", "classification": cls}
                )
            self._json(200, {"items": items})
            return

        # /api/conversations/{id}/agent_final_response
        if len(parts) == 4 and parts[3] == "agent_final_response":
            c = self.runtime.conversations.get(parts[2])
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            self._json(200, {"response": c.final_response or ""})
            return

        # /api/conversations/{id}/events/count
        if len(parts) == 5 and parts[3] == "events" and parts[4] == "count":
            c = self.runtime.conversations.get(parts[2])
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            self._json(200, c.events)
            return

        # /api/conversations/{id}
        if len(parts) == 3 and parts[1] == "conversations":
            c = self.runtime.conversations.get(parts[2])
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            self._json(200, self._info(c))
            return

        self._json(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if not self._guard():
            return
        parts = [p for p in path.split("/") if p]
        body = self._read_body()

        # POST /api/conversations
        if len(parts) == 2 and parts[1] == "conversations":
            rt = self.runtime
            with rt.lock:
                if rt.create_status:
                    code, rt.create_status = rt.create_status, 0
                    self._json(code, {"detail": "capacity full"})
                    return
                cid = body.get("conversation_id")
                if not cid:
                    self._json(422, {"detail": "conversation_id required"})
                    return
                existing = rt.conversations.get(cid)
                if existing is not None:
                    # Confirmed v1.50.1 behaviour: 200 for an id that exists.
                    self._json(200, self._info(existing))
                    return
                c = FakeConversation(
                    id=cid,
                    working_dir=(body.get("workspace") or {}).get("working_dir", ""),
                    title=body.get("title"),
                )
                rt.conversations[cid] = c
                rt.created_count += 1
                self._json(201, self._info(c))
                return

        if len(parts) == 4 and parts[1] == "conversations":
            cid, action = parts[2], parts[3]
            c = self.runtime.conversations.get(cid)
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            if action == "run":
                with self.runtime.lock:
                    if c.status == "running":
                        self._json(409, {"detail": "Conversation already running"})
                        return
                    c.status = "running"
                    c.runs += 1
                self._json(200, {"success": True})
                return
            if action == "pause":
                with self.runtime.lock:
                    c.status = "paused"
                self._json(200, {"success": True})
                return

        # POST /api/conversations/{id}/events
        if len(parts) == 4 and parts[3] == "events":
            c = self.runtime.conversations.get(parts[2])
            if c is None:
                self._json(404, {"detail": "Conversation not found"})
                return
            with self.runtime.lock:
                c.events += 1
                if body.get("run"):
                    if c.status != "running":
                        c.status = "running"
                        c.runs += 1
            self._json(200, {"success": True})
            return

        self._json(404, {"detail": "Not Found"})

    @staticmethod
    def _info(c: FakeConversation) -> dict[str, Any]:
        return {
            "id": c.id,
            "execution_status": c.status,
            "title": c.title,
            "workspace": {"kind": "LocalWorkspace", "working_dir": c.working_dir},
        }


class FakeServer:
    """Context manager that runs the stand-in server on a real port."""

    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        handler = type("Handler", (_Handler,), {"runtime": self.runtime})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
