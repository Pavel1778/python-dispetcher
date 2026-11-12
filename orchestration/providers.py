"""LLM provider cascade: ordered orchestrator adapters with real failover.

The NEXUS orchestrator asks a model to turn a goal into a structured plan. No
single provider is reliable: a key can be missing, a free tier can return
``429`` with a ``Retry-After``, and a host can simply be down. This module
implements the *provider* side of that problem and nothing else.

Design rules, each enforced in code:

* **Orchestrator ≠ executor.** These adapters only ever *propose* a plan as
  JSON. Executing the plan is OpenHands' job (``cloud_direct``). A provider is
  never asked to run code, and a coding-task failure is never a reason to fail
  over: failover happens only on a *provider* or *transport* problem.
* **Ordered cascade.** ``ProviderCascade`` tries providers in the configured
  order (Gemini → Groq → OpenRouter → OpenHands by default) and returns the
  first valid structured response. A provider whose key is absent is skipped,
  not failed.
* **Bounded, honest retry.** A transient error (transport, 5xx, 429) is retried
  a bounded number of times with exponential backoff that honours
  ``Retry-After``. A non-retryable error (400/401/403, a malformed body) moves
  to the next provider at once -- retrying a bad key only burns quota.
* **No secrets in logs.** Keys are read from the environment and never logged,
  returned, or included in an exception message. ``__repr__`` is redacted.
* **Structured output.** Every provider returns a plan-shaped object validated
  by :func:`validate_plan_json`; an invalid response is a failure of that
  provider, not a result.
* **A request is sized before it is sent.** Each provider knows how many tokens
  its model reads at once (``context_tokens``, overridable per provider with
  ``<NAME>_CONTEXT_TOKENS``). A prompt that cannot fit is not sent to that
  provider at all; the reason names both numbers. A provider that still answers
  "too large" raises :class:`ProviderTooLarge`, which a caller that can make the
  request smaller (``nexus_spec_planner``) acts on.

The module is standard-library only, like the rest of the orchestration layer,
so it needs no new dependency and is exercised against a real local HTTP server
in ``tests/test_providers.py``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------- errors


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderUnavailable(ProviderError):
    """Transport failure, 5xx or 429. Transient: another provider may work."""


class ProviderAuthError(ProviderError):
    """401/403, or no key. Retrying this provider cannot succeed."""


class ProviderRejected(ProviderError):
    """400/422 or a malformed body. The request or the response is wrong."""


class ProviderTooLarge(ProviderRejected):
    """The request does not fit the model: 413, or a 400 that says so, or a
    prompt this module already knows is larger than the model's window."""


# --------------------------------------------------------------------- tokens

_LONG_RUN = re.compile(r"[!-~]{33,}")
_GREEK_CYRILLIC = re.compile(r"[\u0370-\u052f]")
#: What a provider's 400/422 body says when the problem is the size of the request.
_TOO_LARGE_BODY = re.compile(
    r"context[ _-]?(?:length|window)|too (?:large|long|many tokens)|maximum[^.]{0,40}tokens|"
    r"token limit|tokens per minute|request_too_large|exceeds? the (?:limit|maximum)", re.IGNORECASE)

#: Tokens left free in a model's window for its answer.
RESPONSE_RESERVE_TOKENS = 2_000


def estimate_tokens(text: str) -> int:
    """How many model tokens *text* is likely to be, erring high.

    No tokenizer is available (this layer is standard-library only, and each
    provider has its own), so this is a per-script estimate: three ASCII
    characters per token, two for long unbroken ASCII runs (hashes, base64 and
    URLs tokenise badly), two Cyrillic or Greek letters per token, one token
    per other character, two per astral character (emoji). Real tokenizers do
    better than this on prose in every script, which is the point: a request
    that fits by this count fits.
    """
    if not text:
        return 0
    total = len(text)
    ascii_count = len(text.encode("ascii", "ignore"))
    astral = len(text.encode("utf-16-le", "surrogatepass")) // 2 - total
    cyrillic = total - len(_GREEK_CYRILLIC.sub("", text)) if ascii_count < total else 0
    other = total - ascii_count - cyrillic - astral
    long_runs = sum(len(m.group(0)) for m in _LONG_RUN.finditer(text)) if ascii_count else 0
    tokens = ((ascii_count - long_runs) / 3.0 + long_runs / 2.0
              + cyrillic * 0.5 + other * 1.0 + astral * 2.0)
    return int(tokens) + 1


class AllProvidersFailed(ProviderError):
    """Every provider in the cascade failed. Carries each provider's reason.

    *kinds* says, per failure, what sort it was, for a caller that can act on
    the difference: ``too_large`` (the request does not fit the model - a
    smaller request may), ``auth`` (no key, or the key was refused - retrying
    cannot help), ``unavailable`` (transport, 5xx, 429 - a later attempt may
    work) and ``rejected`` (the request or the answer was wrong, including an
    answer that is not a valid plan).
    """

    def __init__(self, failures: list[tuple[str, str]], kinds: list[str] | None = None):
        self.failures = failures
        self.kinds = list(kinds or [])
        detail = "; ".join(f"{name}: {reason}" for name, reason in failures) or "no providers configured"
        super().__init__(f"all providers failed ({detail})")

    @property
    def too_large(self) -> bool:
        """At least one provider refused the request for its size."""
        return "too_large" in self.kinds

    @property
    def permanent(self) -> bool:
        """No provider could ever answer this: none has a usable key."""
        return not self.failures or (bool(self.kinds) and all(k == "auth" for k in self.kinds))


def failure_kind(error: Exception) -> str:
    """The sort of failure *error* is, as :class:`AllProvidersFailed` names them."""
    if isinstance(error, ProviderTooLarge):
        return "too_large"
    if isinstance(error, ProviderAuthError):
        return "auth"
    if isinstance(error, ProviderUnavailable):
        return "unavailable"
    return "rejected"


# ------------------------------------------------------------- http plumbing


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: ``urlopen`` would resend the bearer to a new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class ProviderResponse:
    """One provider's answer: parsed JSON plus non-secret provenance."""

    provider: str
    model: str
    data: dict[str, Any]
    attempts: int = 1


@dataclass
class ProbeResult:
    """What one real, read-only probe of a provider proved. Never a key.

    ``kind`` is the same vocabulary :func:`failure_kind` produces, plus
    ``blocked`` for a provider that has no key to probe with:

    * ``ok``          — the endpoint answered, the key was accepted and (when
                        the endpoint lists models) the configured model is
                        among them. This is what earns ``VERIFIED``.
    * ``auth``        — the key was refused (401/403).
    * ``unavailable`` — transport failure, 5xx or 429: nothing was proved.
    * ``rejected``    — the request was refused (400/422) or the configured
                        model is not one the key can use.
    * ``blocked``     — no key is configured, so nothing could be probed.
    """

    provider: str
    model: str
    kind: str
    detail: str = ""
    models: list[str] = field(default_factory=list)
    model_available: bool | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _model_listed(model: str, models: list[str]) -> bool:
    """Whether *model* appears in *models*, tolerating provider prefixes.

    Gemini lists ``models/gemini-2.5-flash`` and OpenRouter lists
    ``openai/gpt-4o-mini``; the configured model may carry or omit the prefix,
    so an exact match or a match on the last path segment both count.
    """
    wanted = model.strip().lower()
    tail = wanted.rsplit("/", 1)[-1]
    for candidate in models:
        name = str(candidate).strip().lower()
        if name == wanted or name.rsplit("/", 1)[-1] == tail:
            return True
    return False


def _retry_after(headers: Any) -> float | None:
    """Parse a ``Retry-After`` header (seconds form). ``None`` if unusable."""
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


# --------------------------------------------------------------- provider API


class Provider:
    """Base class: a named adapter that turns a prompt into structured JSON.

    A concrete provider implements :meth:`_call`, which returns the raw decoded
    body. This class handles auth-header shaping, the retry/backoff policy,
    ``Retry-After``, and turning an HTTP status into the right exception.
    """

    name: str = "provider"
    #: Tokens the model reads in one request when nothing more specific is known.
    DEFAULT_CONTEXT_TOKENS = 100_000

    def __init__(self, *, api_key: str | None = None, base_url: str,
                 model: str, timeout: float = 30.0, max_retries: int = 2,
                 backoff_base: float = 0.5, sleep: Callable[[float], None] = time.sleep,
                 context_tokens: int | None = None):
        self.api_key = api_key if api_key is not None else self._key_from_env()
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        # The model's window. An account can have less than the model does (a
        # free tier's tokens-per-minute cap rejects a large request outright),
        # so the operator can state the real figure.
        self.context_tokens = context_tokens or self._context_from_env()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- subclass hooks ----------------------------------------------------
    _ENV_KEY = ""

    def _key_from_env(self) -> str | None:
        value = os.environ.get(self._ENV_KEY, "").strip() if self._ENV_KEY else ""
        return value or None

    def _context_from_env(self) -> int:
        raw = os.environ.get(f"{self.name.upper()}_CONTEXT_TOKENS", "").strip()
        try:
            value = int(raw) if raw else 0
        except ValueError:
            value = 0
        return value if value > 0 else self.DEFAULT_CONTEXT_TOKENS

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def room_for(self, prompt: str, system: str = "") -> tuple[int, int]:
        """``(tokens the request needs, tokens the model has for a request)``."""
        return (estimate_tokens(prompt) + estimate_tokens(system),
                self.context_tokens - RESPONSE_RESERVE_TOKENS)

    def __repr__(self) -> str:  # never expose the key
        return f"{type(self).__name__}(name={self.name!r}, model={self.model!r}, configured={self.configured})"

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, prompt: str, system: str, *, prose: bool = False) -> dict[str, Any]:
        """The request body. ``prose=True`` asks for plain text, not a JSON object.

        A plan must be machine-readable, so the default constrains the model to
        JSON where the API supports it. A chat answer is the opposite: sending
        the same constraint makes the model wrap its answer in JSON, or makes the
        API reject the request outright because the prompt never mentions JSON.
        """
        raise NotImplementedError

    def _extract_text(self, body: Any) -> str:
        raise NotImplementedError

    # -- probe (read-only) -------------------------------------------------
    def _models_path(self) -> str | None:
        """The endpoint that lists this provider's usable models, or ``None``.

        A provider that can list its models lets a probe answer "is the
        configured model available to this key" as well as "is the key
        accepted". The base class returns ``None``; a concrete provider that
        knows its route overrides this.
        """
        return None

    def _extract_models(self, body: Any) -> list[str]:
        """Pull model names out of a ``_models_path`` response body."""
        return []

    def _probe(self) -> ProbeResult:
        """One read-only request that proves the key works and names the models.

        Never sends a prompt, so it costs no tokens and cannot be mistaken for
        a plan. ``models`` is empty when the provider has no list route.
        """
        path = self._models_path()
        if path is None:
            return ProbeResult(self.name, self.model, "ok", "key accepted (no model list route)")
        body = self._get(path)
        models = self._extract_models(body)
        if not models:
            return ProbeResult(self.name, self.model, "ok",
                               "key accepted (endpoint returned no model list)")
        available = _model_listed(self.model, models)
        detail = (f"key accepted; model {self.model} is available"
                  if available else
                  f"key accepted but model {self.model} is not in the key's list")
        return ProbeResult(self.name, self.model, "ok" if available else "rejected",
                           detail, models=models, model_available=available)

    def probe(self) -> ProbeResult:
        """Probe this provider for real and return what was proved.

        A provider with no key is ``blocked`` — nothing is claimed about
        whether it would work. A probe never raises: every failure mode becomes
        a :class:`ProbeResult`, so a caller can report all providers at once.
        """
        if not self.configured:
            return ProbeResult(self.name, self.model, "blocked", "no API key configured")
        try:
            return self._probe()
        except ProviderError as e:
            return ProbeResult(self.name, self.model, failure_kind(e), str(e))
        except Exception as e:  # noqa: BLE001 - a probe reports, it never raises
            return ProbeResult(self.name, self.model, "rejected", f"{type(e).__name__}: {e}")

    # -- shared request path ----------------------------------------------
    def _get(self, path: str) -> Any:
        """A read-only GET to *path* on the provider, same error mapping."""
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        for key, value in self._headers().items():
            req.add_header(key, value)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"{self.name} -> {e.code}") from e
            if e.code == 429:
                raise ProviderUnavailable(f"{self.name} -> 429") from e
            if e.code >= 500:
                raise ProviderUnavailable(f"{self.name} -> {e.code}") from e
            raise ProviderRejected(f"{self.name} -> {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e

    def _post(self, prompt: str, system: str, *, prose: bool = False,
              timeout: float | None = None) -> Any:
        data = json.dumps(self._payload(prompt, system, prose=prose)).encode()
        req = urllib.request.Request(self._endpoint(), data=data, method="POST")
        for key, value in self._headers().items():
            req.add_header(key, value)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"{self.name} -> {e.code}") from e
            if e.code == 429:
                raise ProviderUnavailable(f"{self.name} -> 429") from e
            if e.code >= 500:
                raise ProviderUnavailable(f"{self.name} -> {e.code}") from e
            if e.code == 413 or self._says_too_large(e):
                raise ProviderTooLarge(
                    f"{self.name} -> {e.code}: the request is too large for the model") from e
            raise ProviderRejected(f"{self.name} -> {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e

    @staticmethod
    def _says_too_large(error: urllib.error.HTTPError) -> bool:
        """Whether a 4xx body blames the size of the request.

        Only a bounded prefix is read, and it is used for this one decision:
        an error body can echo the prompt, so it is never kept or logged.
        """
        try:
            body = error.read(4000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an unreadable body decides nothing
            return False
        return bool(_TOO_LARGE_BODY.search(body))

    def complete(self, prompt: str, system: str = "", *,
                 timeout: float | None = None) -> ProviderResponse:
        """Call the provider, retrying only transient failures, bounded."""
        text, attempts = self._text_with_retry(prompt, system, timeout=timeout)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderRejected(f"{self.name}: completion was not JSON") from e
        if not isinstance(parsed, dict):
            raise ProviderRejected(f"{self.name}: completion was not a JSON object")
        return ProviderResponse(provider=self.name, model=self.model,
                                data=parsed, attempts=attempts)

    def complete_text(self, prompt: str, system: str = "") -> str:
        """Call the provider and return the raw text of its answer.

        Used by the chat surface, which needs prose, not a plan. The retry
        policy is identical: only a transient failure is retried, bounded.
        """
        return self._text_with_retry(prompt, system, prose=True)[0]

    def _text_with_retry(self, prompt: str, system: str, *, prose: bool = False,
                         timeout: float | None = None) -> tuple[str, int]:
        if not self.configured:
            raise ProviderAuthError(f"{self.name}: no API key configured")
        needed, room = self.room_for(prompt, system)
        if needed > room:
            # Not sent: the model cannot read it, and a provider's own refusal
            # of an oversized request is slower and says less.
            raise ProviderTooLarge(
                f"{self.name}: the request needs about {needed} tokens and the model "
                f"reads {room} ({self.name.upper()}_CONTEXT_TOKENS={self.context_tokens})")
        attempts = 0
        last: ProviderUnavailable | None = None
        while attempts <= self.max_retries:
            attempts += 1
            try:
                body = self._post(prompt, system, prose=prose, timeout=timeout)
            except ProviderUnavailable as e:
                last = e
                if attempts > self.max_retries:
                    raise
                self._sleep(self.backoff_base * (2 ** (attempts - 1)))
                continue
            text = self._extract_text(body)
            if not text:
                raise ProviderRejected(f"{self.name}: empty completion")
            return text, attempts
        raise last or ProviderUnavailable(f"{self.name}: exhausted retries")


class GeminiProvider(Provider):
    name = "gemini"
    _ENV_KEY = "GEMINI_API_KEY"
    DEFAULT_CONTEXT_TOKENS = 1_000_000

    def __init__(self, **kw: Any):
        kw.setdefault("base_url", os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"))
        kw.setdefault("model", os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
        super().__init__(**kw)

    def _endpoint(self) -> str:
        return f"{self.base_url}/models/{self.model}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key or ""}

    def _payload(self, prompt: str, system: str, *, prose: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        if not prose:
            payload["generationConfig"] = {"responseMimeType": "application/json"}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _extract_text(self, body: Any) -> str:
        try:
            parts = body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    def _models_path(self) -> str | None:
        return "/models"

    def _extract_models(self, body: Any) -> list[str]:
        raw = body.get("models") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return []
        return [m.get("name") or m.get("id") for m in raw if isinstance(m, dict)]


class _OpenAICompatProvider(Provider):
    """Shared shape for Groq and OpenRouter (OpenAI-compatible chat completions)."""

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key or ''}"}

    def _payload(self, prompt: str, system: str, *, prose: bool = False) -> dict[str, Any]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if not prose:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _extract_text(self, body: Any) -> str:
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def _models_path(self) -> str | None:
        return "/models"

    def _extract_models(self, body: Any) -> list[str]:
        raw = body.get("data") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return []
        return [m.get("id") or m.get("name") for m in raw if isinstance(m, dict)]


class GroqProvider(_OpenAICompatProvider):
    name = "groq"
    _ENV_KEY = "GROQ_API_KEY"
    DEFAULT_CONTEXT_TOKENS = 128_000

    def __init__(self, **kw: Any):
        kw.setdefault("base_url", os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
        kw.setdefault("model", os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"))
        super().__init__(**kw)


class OpenRouterProvider(_OpenAICompatProvider):
    name = "openrouter"
    _ENV_KEY = "OPENROUTER_API_KEY"
    DEFAULT_CONTEXT_TOKENS = 128_000

    def __init__(self, **kw: Any):
        kw.setdefault("base_url", os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
        kw.setdefault("model", os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"))
        super().__init__(**kw)


class OpenHandsOrchestratorProvider(Provider):
    """The last-resort orchestrator: OpenHands' own model, via its Cloud API.

    This is deliberately *not* a chat-completion adapter. It posts a
    single-turn conversation to OpenHands Cloud and reads the final assistant
    message. It is the fallback *orchestrator* only; it must never be used as
    the executor of a task it planned (that would recurse OpenHands into
    itself), so the cascade marks it ``executor=False``.
    """

    name = "openhands"
    _ENV_KEY = "OPENHANDS_API_KEY"
    executor = False

    def __init__(self, **kw: Any):
        kw.setdefault("base_url", os.environ.get("OPENHANDS_CLOUD_URL", "https://app.all-hands.dev"))
        kw.setdefault("model", os.environ.get("OPENHANDS_ORCHESTRATOR_MODEL", "gpt-5"))
        super().__init__(**kw)

    def _endpoint(self) -> str:
        return f"{self.base_url}/api/v1/app-conversations"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key or ''}"}

    def _payload(self, prompt: str, system: str, *, prose: bool = False) -> dict[str, Any]:
        content = (system + "\n\n" + prompt).strip() if system else prompt
        return {"initial_message": {"role": "user", "content": [{"type": "text", "text": content}]},
                "llm_model": self.model}

    def _extract_text(self, body: Any) -> str:
        # The start-task response does not carry the model's answer; a caller
        # that needs the text polls the conversation. For planning we accept an
        # inline answer when present and otherwise report it as rejected.
        if isinstance(body, dict):
            for key in ("content", "response", "text"):
                if isinstance(body.get(key), str):
                    return body[key]
        return ""

    def _models_path(self) -> str | None:
        # A read-only count proves the key is accepted without creating a
        # conversation. OpenHands Cloud exposes no model-list route for a key.
        return "/api/v1/app-conversations/count"

    def _extract_models(self, body: Any) -> list[str]:
        return []


# --------------------------------------------------------------- the cascade


def validate_plan_json(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the plan shape an orchestrator must return.

    Kept intentionally close to ``nexus_planning.validate_plan`` but tolerant of
    a provider that returns a bare list under ``tasks``. Raises ``ValueError``
    on anything that is not a plan, so a provider cannot pass off prose as a
    plan.
    """
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("plan must contain a non-empty 'tasks' list")
    out = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task {i} is not an object")
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"task {i} has no title")
        deps = task.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise ValueError(f"task {i} dependencies must be a list of ids")
        out.append({"id": task.get("id") or f"task-{i + 1}",
                    "title": title.strip(),
                    "description": (task.get("description") or "").strip()
                    if isinstance(task.get("description"), str) else "",
                    "dependencies": deps})
    return out


@dataclass
class CascadeResult:
    """The winning provider's plan plus the failover trail (for the audit log)."""

    response: ProviderResponse
    plan: list[dict[str, Any]]
    failovers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.response.provider


def default_providers() -> list[Provider]:
    """The cascade in the documented order: Gemini → Groq → OpenRouter → OpenHands."""
    return [GeminiProvider(), GroqProvider(), OpenRouterProvider(),
            OpenHandsOrchestratorProvider()]


class ProviderCascade:
    """Try providers in order; return the first valid structured plan."""

    def __init__(self, providers: list[Provider] | None = None):
        self.providers = providers if providers is not None else default_providers()

    def configured(self) -> list[str]:
        return [p.name for p in self.providers if p.configured]

    def usable(self) -> list[Provider]:
        """The providers that have a key, in cascade order."""
        return [p for p in self.providers if p.configured]

    def probe_all(self) -> list[ProbeResult]:
        """Probe every provider in cascade order and report what each proved.

        A real, read-only request per *configured* provider (see
        :meth:`Provider.probe`); a provider with no key is ``blocked``. Nothing
        here is guessed: the report says which providers answered and which did
        not, so the caller can never declare a provider connected on faith.
        """
        return [p.probe() for p in self.providers]

    def plan(self, prompt: str, system: str = "", *,
             timeout: float | None = None) -> CascadeResult:
        """Ask each configured provider for a plan until one answers validly.

        Failover happens only on a provider/transport failure or an invalid
        plan body. An unconfigured provider is skipped silently. If nothing
        succeeds, :class:`AllProvidersFailed` carries every reason so the
        operator sees *why*, not just "failed".
        """
        failures: list[tuple[str, str]] = []
        kinds: list[str] = []
        for provider in self.providers:
            if not provider.configured:
                continue
            try:
                response = provider.complete(prompt, system, timeout=timeout)
                plan = validate_plan_json(response.data)
            except ProviderError as e:
                failures.append((provider.name, str(e)))
                kinds.append(failure_kind(e))
                continue
            except ValueError as e:
                failures.append((provider.name, f"invalid plan: {e}"))
                kinds.append("rejected")
                continue
            return CascadeResult(response=response, plan=plan, failovers=list(failures))
        raise AllProvidersFailed(failures, kinds)

    def text(self, prompt: str, system: str = "") -> ProviderResponse:
        """Ask each configured provider for prose until one answers.

        The chat surface needs a natural-language answer, not a plan. Failover
        is identical to :meth:`plan`: only provider/transport problems move to
        the next provider. The answer is returned as a
        :class:`ProviderResponse` whose ``data`` is ``{"text": ...}``, so the
        caller records the same provenance either way.
        """
        failures: list[tuple[str, str]] = []
        kinds: list[str] = []
        for provider in self.providers:
            if not provider.configured:
                continue
            try:
                text = provider.complete_text(prompt, system)
            except ProviderError as e:
                failures.append((provider.name, str(e)))
                kinds.append(failure_kind(e))
                continue
            if not text.strip():
                failures.append((provider.name, "empty completion"))
                kinds.append("rejected")
                continue
            return ProviderResponse(provider=provider.name, model=provider.model,
                                    data={"text": text})
        raise AllProvidersFailed(failures, kinds)
