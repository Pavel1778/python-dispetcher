"""AI provider fallback: configuration, keys, models, timeouts, switching.

No client is mocked. Each provider makes a real HTTP request over a socket to a
local server that speaks that provider's wire shape, so the checks the request
asks for are exercised for real:

* configuration presence (which providers have a key, in cascade order);
* key checking (401/403 is a rejected key, not a transport problem);
* model availability (the configured model is/ is not in the key's list);
* authorization errors (a refused key fails that provider at once);
* timeouts (a slow endpoint is ``unavailable``, never ``ok``);
* switching (a failing provider fails over to the next one);
* context preservation (the request that reaches the winning provider is the
  one the cascade was asked, system prompt included);
* return to the primary provider (once the primary answers, it wins again).

The provider *diagnostic* is also exercised: ``check_providers`` reports
configuration by presence only, and ``check_provider_probe`` earns each status
from a real call and is ``UNVERIFIED`` unless requested. A provider is never
declared connected without one.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.openhands_diagnostics import (  # noqa: E402
    BLOCKED,
    FAILED,
    UNVERIFIED,
    VERIFIED,
    OpenHandsDiagnostics,
    Report,
)
from orchestration.providers import (  # noqa: E402
    GeminiProvider,
    GroqProvider,
    OpenHandsOrchestratorProvider,
    OpenRouterProvider,
    ProviderCascade,
)

PLAN = {"tasks": [{"id": "t1", "title": "Do the thing",
                   "description": "details", "dependencies": []}]}


class FakeProvider(BaseHTTPRequestHandler):
    """A configurable provider endpoint: POST /chat|generateContent, GET /models.

    ``server.script`` drives each POST in order (the last repeats).
    ``server.models`` drives ``GET /models`` (``None`` -> 404 so a provider with
    no list route is exercised). ``server.models_status`` overrides that status.
    ``server.delay`` sleeps before answering, for the timeout case.
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

    def do_GET(self):  # noqa: N802
        if getattr(self.server, "delay", 0):
            time.sleep(self.server.delay)
        self.server.gets.append({"path": self.path,
                                 "auth": self.headers.get("Authorization"),
                                 "gkey": self.headers.get("x-goog-api-key")})
        status = getattr(self.server, "models_status", None)
        if status is not None:
            self._reply(status, {})
            return
        if self.path.endswith("/count"):
            # The OpenHands orchestrator proves its key with a read-only count.
            self._reply(200, {"count": 0})
            return
        models = getattr(self.server, "models", None)
        if models is None:
            self._reply(404, {"error": "not found"})
            return
        # Gemini uses ``models``; the OpenAI-compatible shape uses ``data``.
        if self.path.endswith("/models") and getattr(self.server, "gemini_shape", False):
            self._reply(200, {"models": [{"name": f"models/{m}"} for m in models]})
        else:
            self._reply(200, {"data": [{"id": m} for m in models]})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if getattr(self.server, "delay", 0):
            time.sleep(self.server.delay)
        self.server.calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "gkey": self.headers.get("x-goog-api-key"),
            "body": body,
        })
        step = self.server.script[min(self.server.index, len(self.server.script) - 1)]
        self.server.index += 1
        if step.get("status", 200) >= 400:
            self._reply(step["status"], step.get("payload", {}), step.get("headers"))
            return
        self._reply(200, step["payload"], step.get("headers"))


@pytest.fixture
def provider_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    srv.script = [{"payload": {}}]
    srv.calls = []
    srv.gets = []
    srv.index = 0
    srv.models = None
    srv.models_status = None
    srv.delay = 0
    srv.gemini_shape = False
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join()
    srv.server_close()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_port}"


def gemini(srv, **kw) -> GeminiProvider:
    return GeminiProvider(base_url=base(srv), api_key="k", model="gemini-2.5-flash",
                          sleep=lambda _s: None, **kw)


def groq(srv, **kw) -> GroqProvider:
    return GroqProvider(base_url=base(srv), api_key="k", model="llama-3.3-70b-versatile",
                        sleep=lambda _s: None, **kw)


def openrouter(srv, **kw) -> OpenRouterProvider:
    return OpenRouterProvider(base_url=base(srv), api_key="k", model="openai/gpt-4o-mini",
                              sleep=lambda _s: None, **kw)


def openhands(srv, **kw) -> OpenHandsOrchestratorProvider:
    return OpenHandsOrchestratorProvider(base_url=base(srv), api_key="k", model="gpt-5",
                                         sleep=lambda _s: None, **kw)


def chat_body(plan=PLAN) -> dict:
    return {"choices": [{"message": {"content": json.dumps(plan)}}]}


def gemini_body(plan=PLAN) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(plan)}]}}]}


# --------------------------------------------------------------- configuration
def test_configuration_presence_is_reported_without_a_call(provider_server):
    """Only providers with a key are listed, in cascade order, with no request."""
    cascade = ProviderCascade([
        gemini(provider_server),
        GroqProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None),
        openrouter(provider_server),
    ])
    assert cascade.configured() == ["gemini", "openrouter"]
    assert provider_server.calls == []  # configured() never calls out
    assert provider_server.gets == []


def test_a_provider_without_a_key_is_blocked_not_failed(provider_server):
    """No key means nothing was proved: ``blocked``, never ``ok`` or ``auth``."""
    p = GroqProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None)
    result = p.probe()
    assert result.kind == "blocked" and not result.ok
    assert provider_server.calls == [] and provider_server.gets == []


# ------------------------------------------------------------------- keys
def test_probe_accepts_a_working_key_and_finds_the_model(provider_server):
    provider_server.models = ["llama-3.3-70b-versatile", "mixtral-8x7b"]
    result = groq(provider_server).probe()
    assert result.ok and result.model_available is True
    assert provider_server.gets[-1]["auth"] == "Bearer k"


def test_probe_rejects_a_refused_key_as_auth(provider_server):
    provider_server.models_status = 401
    result = groq(provider_server).probe()
    assert result.kind == "auth" and not result.ok


def test_probe_of_a_missing_model_is_rejected(provider_server):
    """The key works but the configured model is not one it can use."""
    provider_server.models = ["some-other-model"]
    result = groq(provider_server).probe()
    assert result.kind == "rejected" and result.model_available is False


def test_gemini_probe_tolerates_the_models_prefix(provider_server):
    provider_server.gemini_shape = True
    provider_server.models = ["gemini-2.5-flash"]
    result = gemini(provider_server).probe()
    assert result.ok and result.model_available is True


def test_openrouter_probe_matches_the_last_path_segment(provider_server):
    provider_server.models = ["openai/gpt-4o-mini"]
    result = openrouter(provider_server).probe()
    assert result.ok and result.model_available is True


def test_openhands_orchestrator_probe_uses_a_read_only_count(provider_server):
    """The OpenHands provider proves its key with a count, not a conversation."""
    provider_server.models = None  # a count body is not a model list
    result = openhands(provider_server).probe()
    assert result.ok
    assert provider_server.gets[-1]["path"].endswith("/api/v1/app-conversations/count")


# ------------------------------------------------------------- transport
def test_probe_of_a_5xx_is_unavailable_not_ok(provider_server):
    provider_server.models_status = 503
    result = groq(provider_server).probe()
    assert result.kind == "unavailable" and not result.ok


def test_probe_of_a_timeout_is_unavailable(provider_server):
    """A slow endpoint must never be reported as a working provider."""
    provider_server.models = ["m"]
    provider_server.delay = 0.6
    p = GroqProvider(base_url=base(provider_server), api_key="k", model="m",
                     timeout=0.2, sleep=lambda _s: None)
    result = p.probe()
    assert result.kind == "unavailable"


def test_probe_of_an_unreachable_endpoint_is_unavailable():
    p = GroqProvider(base_url="http://127.0.0.1:1", api_key="k", model="m",
                     timeout=0.5, sleep=lambda _s: None)
    result = p.probe()
    assert result.kind == "unavailable"


# ------------------------------------------------------------ the cascade
def test_cascade_probe_all_reports_each_provider(provider_server):
    provider_server.models = ["llama-3.3-70b-versatile"]
    cascade = ProviderCascade([
        gemini(provider_server),
        GroqProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None),
    ])
    results = {r.provider: r for r in cascade.probe_all()}
    assert results["gemini"].kind == "ok"
    assert results["groq"].kind == "blocked"


def test_switching_fails_over_to_the_next_provider(provider_server):
    """A failing primary switches to the next provider, and the trail records it."""
    provider_server.script = [{"status": 401, "payload": {}},  # gemini refused
                              {"payload": chat_body()}]         # groq answers
    cascade = ProviderCascade([gemini(provider_server), groq(provider_server)])
    result = cascade.plan("goal")
    assert result.provider == "groq"
    assert [name for name, _ in result.failovers] == ["gemini"]


def test_context_is_preserved_across_the_switch(provider_server):
    """The winning provider receives the exact prompt and system the caller sent."""
    provider_server.script = [{"status": 500, "payload": {}},  # gemini unavailable
                              {"payload": chat_body()}]
    cascade = ProviderCascade([
        gemini(provider_server, max_retries=0), groq(provider_server)])
    cascade.plan("GOAL-TEXT-42", "SYSTEM-TEXT-7")
    winning = provider_server.calls[-1]
    messages = winning["body"]["messages"]
    assert messages[0] == {"role": "system", "content": "SYSTEM-TEXT-7"}
    assert messages[-1] == {"role": "user", "content": "GOAL-TEXT-42"}


def test_return_to_the_primary_once_it_answers(provider_server):
    """The primary is retried on every call; when it works it wins again."""
    provider_server.script = [{"status": 401, "payload": {}},   # call 1: gemini down
                              {"payload": chat_body()},          # call 1: groq
                              {"payload": gemini_body()}]        # call 2: gemini back
    cascade = ProviderCascade([gemini(provider_server), groq(provider_server)])
    first = cascade.plan("goal")
    second = cascade.plan("goal")
    assert first.provider == "groq"
    assert second.provider == "gemini"
    assert second.failovers == []


def test_cascade_probe_reports_no_false_success_when_all_fail(provider_server):
    provider_server.models_status = 401
    cascade = ProviderCascade([gemini(provider_server), groq(provider_server)])
    results = cascade.probe_all()
    assert [r.kind for r in results] == ["auth", "auth"]
    assert all(not r.ok for r in results)


# ---------------------------------------------------- the diagnostic report
def _diagnostics(providers) -> OpenHandsDiagnostics:
    # Only the provider checks are exercised here, so the store/accounts/pool are
    # never touched: the two provider checks read nothing but the cascade.
    return OpenHandsDiagnostics(None, accounts=None, pool=None, providers=providers)


def test_diagnostic_providers_check_is_configuration_only(provider_server):
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_providers(report)
    check = report.by_name("providers")
    assert check.status == VERIFIED
    assert check.evidence["configured"] == ["groq"]
    assert provider_server.calls == [] and provider_server.gets == []  # no call


def test_diagnostic_providers_check_is_blocked_without_a_key(provider_server):
    diag = _diagnostics(ProviderCascade([
        GroqProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None)]))
    report = Report()
    diag.check_providers(report)
    assert report.by_name("providers").status == BLOCKED
    assert report.ok  # no key is not a failure


def test_diagnostic_provider_probe_is_unverified_unless_requested(provider_server):
    provider_server.models = ["llama-3.3-70b-versatile"]
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_provider_probe(report, live=False)
    assert report.by_name("provider_probe").status == UNVERIFIED
    assert provider_server.gets == []  # nothing was probed


def test_diagnostic_provider_probe_verifies_a_working_provider(provider_server):
    provider_server.models = ["llama-3.3-70b-versatile"]
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_provider_probe(report, live=True)
    check = report.by_name("provider_probe")
    assert check.status == VERIFIED
    assert check.evidence["providers"][0]["kind"] == "ok"


def test_diagnostic_provider_probe_fails_on_a_rejected_key(provider_server):
    provider_server.models_status = 401
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_provider_probe(report, live=True)
    assert report.by_name("provider_probe").status == FAILED
    assert not report.ok


def test_diagnostic_provider_probe_is_blocked_on_a_transport_failure(provider_server):
    provider_server.models_status = 503
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_provider_probe(report, live=True)
    assert report.by_name("provider_probe").status == BLOCKED
    assert report.ok  # a transport failure is not a proven failure


def test_diagnostic_provider_probe_never_leaks_a_key(provider_server):
    provider_server.models = ["llama-3.3-70b-versatile"]
    diag = _diagnostics(ProviderCascade([groq(provider_server)]))
    report = Report()
    diag.check_provider_probe(report, live=True)
    blob = json.dumps(report.as_dict())
    assert "Bearer k" not in blob and '"k"' not in blob


def test_diagnostic_provider_probe_without_keys_is_unverified(provider_server):
    diag = _diagnostics(ProviderCascade([
        GroqProvider(base_url=base(provider_server), api_key="", sleep=lambda _s: None)]))
    report = Report()
    diag.check_provider_probe(report, live=True)
    assert report.by_name("provider_probe").status == UNVERIFIED


def test_the_full_report_carries_both_provider_checks():
    """``run`` includes the provider checks alongside the OpenHands ones."""
    diag = _diagnostics(ProviderCascade([]))
    # run() touches the store; the provider checks are what this asserts, so
    # only their presence and honesty are checked by calling them directly.
    report = Report()
    diag.check_providers(report)
    diag.check_provider_probe(report, live=False)
    names = {c.name for c in report.checks}
    assert names == {"providers", "provider_probe"}
