"""A local stand-in for the OpenHands agent-server.

This is a real HTTP server speaking the confirmed v1.50.1 surface, not a mock
of the client. Tests drive the dispatcher through real sockets and real JSON,
so serialization, status codes and error paths are exercised for real.

The only thing it fakes is the LLM: a conversation "finishes" when the test
tells it to, which keeps the suite offline and deterministic.
"""

from __future__ import annotations

import json
import posixpath
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


@dataclass
class FakeConversation:
    id: str
    working_dir: str
    title: str | None = None
    status: str = "idle"
    final_response: str | None = None
    events: int = 0
    runs: int = 0
    #: What the create request carried. The live server reads
    #: ``agent_context`` from here; recording it lets a test prove the
    #: dispatcher forwarded the verified state rather than an empty default.
    agent_context: dict[str, Any] = field(default_factory=dict)
    mcp_config: dict[str, Any] = field(default_factory=dict)
    #: The text of the first message, exactly as the create request carried it:
    #: what the agent is given to work from. ``None`` when there was none.
    initial_message: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    #: The sandbox's files at the moment this conversation was created. A file
    #: that arrives later was not there when the agent started.
    files_at_start: dict[str, bytes] = field(default_factory=dict)


def parse_upload(body: bytes, content_type: str) -> tuple[str, bytes] | None:
    """``(field name, data)`` of the one part of a ``multipart/form-data`` body.

    Written here, strictly, instead of borrowing a parser: the fake checks that
    what the client sends is a well-formed upload, and takes the bytes between
    the part's headers and the closing boundary exactly as they are.
    """
    marker = "boundary="
    if not content_type.startswith("multipart/form-data") or marker not in content_type:
        return None
    boundary = content_type.split(marker, 1)[1].split(";")[0].strip().strip('"').encode()
    opening, closing = b"--" + boundary + b"\r\n", b"\r\n--" + boundary + b"--\r\n"
    if not body.startswith(opening) or not body.endswith(closing):
        return None
    head, separator, data = body[len(opening):-len(closing)].partition(b"\r\n\r\n")
    if not separator:
        return None
    for line in head.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-disposition" and 'name="' in value:
            return value.split('name="', 1)[1].split('"', 1)[0], data
    return None


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
    #: The server's settings document. ``PATCH`` merges into this; ``GET``
    #: returns it. Mirrors the live store so the readiness barrier is exercised
    #: over real sockets rather than a mock.
    settings: dict[str, Any] = field(
        default_factory=lambda: {"agent_settings": {"agent": "CodeActAgent"}}
    )
    #: When > 0, the next PATCH returns 503 this many times.
    patch_fail_times: int = 0
    #: When True, PATCH /api/settings answers 404 (endpoint unsupported).
    patch_unsupported: bool = False
    #: The sandbox's file system: absolute path -> content. Written by
    #: ``POST /api/file/upload`` and read by ``GET /api/file/download``, the two
    #: routes of the live agent-server's file router.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Every upload received, in order: ``(path, bytes sent)``.
    uploads: list[tuple[str, int]] = field(default_factory=list)
    #: Directories that cannot be written to: an upload under one answers 500,
    #: as the live server does for a path it has no permission for.
    read_only_dirs: set[str] = field(default_factory=set)
    #: When > 0, the next N uploads answer this status.
    upload_status: int = 0
    upload_status_times: int = 0
    #: Applied to an upload before it is stored: a disk that keeps something
    #: other than what was sent (cut short, altered). ``None`` stores it as is.
    store_as: Callable[[str, bytes], bytes] | None = None
    #: When > 0, the next N downloads answer this status.
    download_status: int = 0
    download_status_times: int = 0
    #: The agent-server's tool catalog (``GET /api/tools/catalog``). Mirrors the
    #: live shape: a list of descriptors carrying ``user_selectable`` /
    #: ``in_default_set`` / ``usable``. ``tool_catalog_status`` forces a status
    #: (e.g. 404 for a server that predates the route).
    tool_catalog: dict[str, Any] = field(default_factory=lambda: {"tools": [
        {"name": "terminal", "user_selectable": True, "in_default_set": True, "usable": True},
        {"name": "file_editor", "user_selectable": True, "in_default_set": True, "usable": True},
        {"name": "task_tracker", "user_selectable": True, "in_default_set": True, "usable": True},
        {"name": "browser_tool_set", "user_selectable": True, "in_default_set": True,
         "usable": True},
        {"name": "switch_llm", "user_selectable": True, "in_default_set": True, "usable": True},
        {"name": "glob", "user_selectable": True, "in_default_set": False, "usable": True},
        {"name": "grep", "user_selectable": True, "in_default_set": False, "usable": True},
        {"name": "read_file", "user_selectable": False, "in_default_set": False, "usable": True},
        {"name": "write_file", "user_selectable": False, "in_default_set": False, "usable": True},
        {"name": "list_directory", "user_selectable": False, "in_default_set": False,
         "usable": True},
    ]})
    tool_catalog_status: int = 0
    #: The conversations deleted with ``DELETE /api/conversations/{id}``, in order.
    deleted: list[str] = field(default_factory=list)
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
            with self.runtime.lock:
                self._json(200, json.loads(json.dumps(self.runtime.settings)))
            return

        # /api/tools/catalog -> the agent-server's own tool set (and /api/tools/).
        if parts in (["api", "tools", "catalog"], ["api", "tools"]):
            with self.runtime.lock:
                if self.runtime.tool_catalog_status:
                    self._json(self.runtime.tool_catalog_status,
                               {"detail": "Not Found"})
                    return
                self._json(200, json.loads(json.dumps(self.runtime.tool_catalog)))
            return

        # /api/file/download?path=<absolute path>
        if parts == ["api", "file", "download"]:
            rt = self.runtime
            target = self._query_path()
            with rt.lock:
                if rt.download_status_times > 0:
                    rt.download_status_times -= 1
                    self._json(rt.download_status, {"detail": "Failed to download file"})
                    return
                data = rt.files.get(target or "")
            if not target or not target.startswith("/"):
                self._json(400, {"detail": "Path must be absolute"})
                return
            if data is None:
                self._json(404, {"detail": "File not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
        # POST /api/file/upload?path=<absolute path>   (multipart, field "file")
        if parts == ["api", "file", "upload"]:
            self._upload()
            return
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
                message = body.get("initial_message")
                if isinstance(message, dict):
                    c.initial_message = "".join(
                        part.get("text", "") for part in message.get("content") or []
                        if isinstance(part, dict))
                    if message.get("run"):
                        c.status = "running"
                        c.runs += 1
                c.tags = dict(body.get("tags") or {})
                c.files_at_start = dict(rt.files)
                # Mirror the live server: the conversation inherits exactly the
                # agent_context and mcp_config the request carried. Nothing is
                # copied from the stored settings — that is the whole defect the
                # readiness barrier exists to close.
                agent_settings = body.get("agent_settings") or {}
                if isinstance(agent_settings, dict):
                    ctx = agent_settings.get("agent_context")
                    if isinstance(ctx, dict):
                        c.agent_context = json.loads(json.dumps(ctx))
                    mcp = agent_settings.get("mcp_config")
                    if isinstance(mcp, dict):
                        c.mcp_config = json.loads(json.dumps(mcp))
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

    def do_PATCH(self) -> None:
        path = self.path.split("?")[0]
        if not self._guard():
            return
        parts = [p for p in path.split("/") if p]
        body = self._read_body()

        if parts == ["api", "settings"]:
            rt = self.runtime
            with rt.lock:
                if rt.patch_unsupported:
                    self._json(404, {"detail": "Not Found"})
                    return
                if rt.patch_fail_times > 0:
                    rt.patch_fail_times -= 1
                    self._json(503, {"detail": "temporarily unavailable"})
                    return
                diff = body.get("agent_settings_diff")
                if not isinstance(diff, dict):
                    # Confirmed live behaviour: the body must carry one of the
                    # *_diff keys; a bare field is rejected with 400.
                    self._json(400, {"detail": "no settings diff provided"})
                    return
                agent = rt.settings.setdefault("agent_settings", {})
                for key, value in diff.items():
                    if key in ("agent_context", "mcp_config") and isinstance(
                        value, dict
                    ):
                        # Field-by-field merge, exactly as the live endpoint does.
                        merged = dict(agent.get(key) or {})
                        for sub_key, sub_value in value.items():
                            if sub_value is None:
                                merged.pop(sub_key, None)
                            else:
                                merged[sub_key] = sub_value
                        agent[key] = merged
                    else:
                        agent[key] = value
                self._json(200, json.loads(json.dumps(rt.settings)))
            return

        self._json(404, {"detail": "Not Found"})

    def do_DELETE(self) -> None:
        path = self.path.split("?")[0]
        if not self._guard():
            return
        parts = [p for p in path.split("/") if p]
        # DELETE /api/conversations/{id}: the conversation and its events go;
        # the sandbox, its files and every other conversation stay.
        if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
            rt = self.runtime
            with rt.lock:
                if rt.conversations.pop(parts[2], None) is None:
                    self._json(404, {"detail": "Not Found"})
                    return
                rt.deleted.append(parts[2])
            self._json(200, {"success": True})
            return
        self._json(404, {"detail": "Not Found"})

    def _query_path(self) -> str | None:
        query = urllib.parse.urlparse(self.path).query
        return (urllib.parse.parse_qs(query).get("path") or [None])[0]

    def _upload(self) -> None:
        rt = self.runtime
        target = self._query_path()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        with rt.lock:
            if rt.upload_status_times > 0:
                rt.upload_status_times -= 1
                self._json(rt.upload_status, {"detail": "Failed to upload file"})
                return
        if not target:
            self._json(422, {"detail": "path is required"})
            return
        if not target.startswith("/"):
            self._json(400, {"detail": "Path must be absolute"})
            return
        part = parse_upload(raw, self.headers.get("Content-Type", ""))
        if part is None or part[0] != "file":
            self._json(422, {"detail": "a multipart 'file' field is required"})
            return
        directory = posixpath.dirname(target)
        with rt.lock:
            if any(directory == d or directory.startswith(d.rstrip("/") + "/")
                   for d in rt.read_only_dirs):
                self._json(500, {"detail": "Failed to upload file: [Errno 13] "
                                           f"Permission denied: '{target}'"})
                return
            rt.uploads.append((target, len(part[1])))
            rt.files[target] = rt.store_as(target, part[1]) if rt.store_as else part[1]
        self._json(200, {"success": True, "file_size": len(part[1])})

    @staticmethod
    def _info(c: FakeConversation) -> dict[str, Any]:
        return {
            "id": c.id,
            "execution_status": c.status,
            "title": c.title,
            "tags": c.tags,
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
