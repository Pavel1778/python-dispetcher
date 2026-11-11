"""Provider cascade: real HTTP against a local fake provider server.

No client is mocked: each provider makes a real request over a socket to a
local server that speaks the provider's wire shape, so auth headers, the retry
policy, ``Retry-After`` handling, failover order and structured-output
validation are all exercised for real.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.providers import (  # noqa: E402
    AllProvidersFailed,
    GeminiProvider,
    GroqProvider,
    OpenHandsOrchestratorProvider,
    OpenRouterProvider,
    ProviderAuthError,
    ProviderCascade,
    ProviderRejected,
    validate_plan_json,
)

PLAN = {"tasks": [{"id": "t1", "title": "Do the thing",
                   "description": "details", "dependencies": []}]}


class FakeProvider(BaseHTTPRequestHandler):
    """A configurable provider endpoint. ``server.script`` drives each call.

    A test that needs an answer made from the request sets ``server.respond``
    to a function of the request body returning ``(status, payload)``; it then
    takes the place of the script.
    """

    def log_message(self, *a):  # silence
        pass

    def _reply(self, status, payload, headers=None):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.server.calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "gkey": self.headers.get("x-goog-api-key"),
            "body": body,
        })
        respond = getattr(self.server, "respond", None)
        if respond is not None:
            self.server.index += 1
            status, payload = respond(body)
            self._reply(status, payload)
            return
        # Each entry in the script is consumed in order; the last repeats.
        step = self.server.script[min(self.server.index, len(self.server.script) - 1)]
        self.server.index += 1
        if step.get("status", 200) >= 400:
            self._reply(step["status"], step.get("payload", {}),
                        step.get("headers"))
            return
        self._reply(200, step["payload"], step.get("headers"))


@pytest.fixture
def provider_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    srv.script = [{"payload": {}}]
    srv.calls = []
    srv.index = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join()
    srv.server_close()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_port}"


def gemini(srv, **kw) -> GeminiProvider:
    return GeminiProvider(base_url=base(srv), api_key="k", model="m", sleep=lambda _s: None, **kw)


def groq(srv, **kw) -> GroqProvider:
    return GroqProvider(base_url=base(srv), api_key="k", model="m", sleep=lambda _s: None, **kw)


def openrouter(srv, **kw) -> OpenRouterProvider:
    return OpenRouterProvider(base_url=base(srv), api_key="k", model="m", sleep=lambda _s: None, **kw)


def openhands(srv, **kw) -> OpenHandsOrchestratorProvider:
    return OpenHandsOrchestratorProvider(base_url=base(srv), api_key="k", model="m",
                                         sleep=lambda _s: None, **kw)


def gemini_body(plan=PLAN) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(plan)}]}}]}


def chat_body(plan=PLAN) -> dict:
    return {"choices": [{"message": {"content": json.dumps(plan)}}]}


# --------------------------------------------------------------- happy paths
def test_gemini_parses_a_plan(provider_server):
    provider_server.script = [{"payload": gemini_body()}]
    resp = gemini(provider_server).complete("goal")
    assert resp.data == PLAN and resp.provider == "gemini"
    call = provider_server.calls[-1]
    assert call["gkey"] == "k"
    assert "/models/m:generateContent" in call["path"]


def test_groq_and_openrouter_use_bearer(provider_server):
    provider_server.script = [{"payload": chat_body()}]
    groq(provider_server).complete("goal")
    assert provider_server.calls[-1]["auth"] == "Bearer k"
    assert provider_server.calls[-1]["path"].endswith("/chat/completions")


def test_openhands_orchestrator_posts_a_conversation(provider_server):
    provider_server.script = [{"payload": {"content": json.dumps(PLAN)}}]
    resp = openhands(provider_server).complete("goal", system="be terse")
    assert resp.data == PLAN
    body = provider_server.calls[-1]["body"]
    assert "llm_model" in body and "initial_message" in body


# ------------------------------------------------------------------ failures
def test_missing_key_is_auth_error_not_a_request(provider_server):
    provider_server.script = [{"payload": gemini_body()}]
    p = GeminiProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None)
    with pytest.raises(ProviderAuthError):
        p.complete("goal")
    assert provider_server.calls == []  # nothing was sent


def test_401_is_not_retried(provider_server):
    provider_server.script = [{"status": 401, "payload": {}}]
    with pytest.raises(ProviderAuthError):
        groq(provider_server).complete("goal")
    assert len(provider_server.calls) == 1  # no retry


def test_429_is_retried_then_gives_up(provider_server):
    provider_server.script = [{"status": 429, "payload": {}}]
    with pytest.raises(Exception):
        groq(provider_server, max_retries=2).complete("goal")
    assert len(provider_server.calls) == 3  # 1 + 2 retries


def test_5xx_then_success_retries_and_wins(provider_server):
    provider_server.script = [{"status": 503, "payload": {}},
                              {"payload": chat_body()}]
    resp = groq(provider_server).complete("goal")
    assert resp.attempts == 2
    assert len(provider_server.calls) == 2


def test_non_json_completion_is_rejected(provider_server):
    provider_server.script = [{"payload": {"choices": [{"message": {"content": "not json"}}]}}]
    with pytest.raises(ProviderRejected):
        groq(provider_server).complete("goal")


# ------------------------------------------------------------------- cascade
def test_cascade_skips_unconfigured_and_fails_over(provider_server):
    provider_server.script = [{"status": 401, "payload": {}},   # gemini auth fails
                              {"payload": chat_body()}]          # groq wins
    cascade = ProviderCascade([
        gemini(provider_server),
        groq(provider_server),
        OpenRouterProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None),
    ])
    result = cascade.plan("goal")
    assert result.provider == "groq"
    assert [name for name, _ in result.failovers] == ["gemini"]


def test_cascade_reports_every_failure(provider_server):
    provider_server.script = [{"status": 500, "payload": {}}]
    cascade = ProviderCascade([groq(provider_server, max_retries=0),
                               gemini(provider_server, max_retries=0)])
    with pytest.raises(AllProvidersFailed) as e:
        cascade.plan("goal")
    names = [name for name, _ in e.value.failures]
    assert names == ["groq", "gemini"]


def test_cascade_rejects_a_non_plan_body(provider_server):
    provider_server.script = [{"payload": {"choices": [{"message": {"content": json.dumps({"foo": 1})}}]}},
                              {"payload": gemini_body()}]
    cascade = ProviderCascade([groq(provider_server), gemini(provider_server)])
    result = cascade.plan("goal")
    assert result.provider == "gemini"  # groq's body was not a plan


def test_repr_never_exposes_the_key(provider_server):
    p = groq(provider_server)
    assert "k" not in repr(p).replace("configured", "")
    assert "configured=True" in repr(p)


# ----------------------------------------------------------------- validation
def test_validate_plan_json_rejects_prose():
    for bad in ({}, {"tasks": []}, {"tasks": [{"description": "x"}]},
                {"tasks": [{"title": "ok", "dependencies": "no"}]}):
        with pytest.raises(ValueError):
            validate_plan_json(bad)


def test_validate_plan_json_accepts_a_minimal_plan():
    plan = validate_plan_json({"tasks": [{"title": "A"}]})
    assert plan[0]["title"] == "A" and plan[0]["id"] == "task-1"
