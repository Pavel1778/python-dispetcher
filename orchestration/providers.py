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

from .nexus_generation import GenerationCancelled

from .nexus_policy import UnsafeTarget, check_outbound_url

# --------------------------------------------------------------------- errors


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderUnavailable(ProviderError):
    """Transport failure, 5xx, 429 or an exhausted quota.

    Transient: another provider may work. When the provider said how long to
    wait (``Retry-After``), ``retry_after`` carries it, so the caller can skip
    this provider for that long instead of retrying it.
    """

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


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
#: What a 400/403 body says when the *credential* is the problem. Google answers
#: 400 ``API_KEY_INVALID`` for a bad key rather than 401, so the status alone is
#: not enough to tell a broken key from a malformed request.
_BAD_KEY_BODY = re.compile(
    r"api[ _-]?key[^.]{0,40}(?:invalid|not valid|expired|missing|required)|invalid[ _-]?api[ _-]?key|"
    r"api_key_invalid|incorrect[ _-]?api[ _-]?key|unauthenticated|authentication failed|"
    r"invalid[ _-]?(?:bearer|token|credential)|permission denied|credits? (?:exhausted|exceeded)",
    re.IGNORECASE)

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


# ----------------------------------------------------------------- cooldowns

def _env_seconds(name: str, default: float) -> float:
    """A duration from the environment; a junk or non-positive value is the default."""
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    return value if value > 0 else default


#: How long a provider whose key was refused (401/403, or 402 "credits
#: exhausted") is skipped. Retrying a bad key only burns quota, and a topped-up
#: account heals long before an hour passes.
AUTH_COOLDOWN_SECONDS = _env_seconds("PROVIDER_AUTH_COOLDOWN", 900.0)
#: First skip for a provider that was unreachable or out of quota; doubles with
#: each consecutive failure.
UNAVAILABLE_COOLDOWN_SECONDS = _env_seconds("PROVIDER_UNAVAILABLE_COOLDOWN", 60.0)
#: The longest a provider is ever skipped because of a transport problem.
MAX_COOLDOWN_SECONDS = _env_seconds("PROVIDER_MAX_COOLDOWN", 900.0)
#: The longest a single request waits inside its own retry loop. A provider that
#: asks for longer is marked cooling at once and another provider is asked: a
#: request must not block for minutes on a limit that is already known.
INLINE_WAIT_SECONDS = _env_seconds("PROVIDER_INLINE_WAIT", 10.0)


@dataclass
class _Cooling:
    seconds: float
    until: float
    kind: str


class Cooldowns:
    """Which providers just failed, and until when they are skipped.

    A cascade is only as useful as its memory. Without this, a provider whose
    key was refused was retried on *every* request, and one that answered 429
    made each request pay its retry backoff before the next provider was tried.
    Here a provider that cannot answer is remembered for a while and passed
    over at once, so the next provider in the hierarchy serves the request.

    The clock is injectable so a test can let a cooldown expire without waiting.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[str, _Cooling] = {}
        self._streak: dict[str, int] = {}

    def remaining(self, name: str) -> float:
        """Seconds this provider is still skipped for; ``0`` when it is usable."""
        entry = self._entries.get(name)
        if entry is None:
            return 0.0
        left = entry.until - self._clock()
        if left <= 0:
            self._entries.pop(name, None)
            return 0.0
        return left

    def cooling(self, name: str) -> bool:
        return self.remaining(name) > 0

    def note_failure(self, name: str, kind: str, retry_after: float | None = None) -> float:
        """Remember a failure and return how long *name* is now skipped for.

        A ``rejected``/``too_large`` failure is the request's or the answer's
        problem, not the provider's availability, so nothing is remembered for
        it. ``auth`` is a fixed long skip; ``unavailable`` grows with each
        consecutive failure and honours a ``Retry-After`` the provider sent.
        """
        if kind in ("rejected", "too_large"):
            self._streak.pop(name, None)
            return 0.0
        if kind == "auth":
            seconds = AUTH_COOLDOWN_SECONDS
        else:
            streak = self._streak.get(name, 0) + 1
            self._streak[name] = streak
            grown = UNAVAILABLE_COOLDOWN_SECONDS * (2 ** (streak - 1))
            seconds = min(max(grown, float(retry_after or 0.0)), MAX_COOLDOWN_SECONDS)
        self._entries[name] = _Cooling(seconds=seconds, until=self._clock() + seconds, kind=kind)
        return seconds

    def note_success(self, name: str) -> None:
        """A provider answered: it is usable again and its streak is forgotten."""
        self._entries.pop(name, None)
        self._streak.pop(name, None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The providers skipped right now, and why (no secret, no key)."""
        out: dict[str, dict[str, Any]] = {}
        for name in list(self._entries):
            left = self.remaining(name)
            if left > 0:
                out[name] = {"seconds": int(left), "kind": self._entries[name].kind}
        return out

    def reset(self) -> None:
        self._entries.clear()
        self._streak.clear()


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


#: Sent on every outbound provider request. Python's own ``Python-urllib/<ver>``
#: is answered with a bare ``403`` by Groq's edge, which reads as a rejected key
#: and fails a real completion the same way - so the probe and the working path
#: must both name themselves. A value that resembles a bot is what gets refused;
#: a plain product token is accepted.
USER_AGENT = "nexus-orchestrator/1.0"


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
        # A provider base URL comes from the environment, so it obeys the same
        # SSRF policy as every other outbound target: a private/metadata address
        # is refused unless the operator explicitly permits private targets.
        try:
            check_outbound_url(base_url)
        except UnsafeTarget as e:
            raise ProviderError(f"{self.name}: {e}") from e
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
        req.add_header("User-Agent", USER_AGENT)
        for key, value in self._headers().items():
            req.add_header(key, value)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"{self.name} -> {e.code}") from e
            if e.code == 402:
                # Credits spent. No request can fix that, so it is a refused
                # identity rather than a transient fault.
                raise ProviderAuthError(f"{self.name} -> 402 (credits exhausted)") from e
            if e.code in (429,) or e.code >= 500:
                raise ProviderUnavailable(f"{self.name} -> {e.code}",
                                          retry_after=_retry_after(e.headers)) from e
            if self._classify_4xx(e) == "auth":
                # A probe that hits a refused credential in a 400 must still
                # read as a refused key, or the cascade would keep asking it.
                raise ProviderAuthError(f"{self.name} -> {e.code}: the key was refused") from e
            raise ProviderRejected(f"{self.name} -> {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e

    def _post(self, prompt: str, system: str, *, prose: bool = False,
              timeout: float | None = None, token: Any = None) -> Any:
        data = json.dumps(self._payload(prompt, system, prose=prose)).encode()
        req = urllib.request.Request(self._endpoint(), data=data, method="POST")
        req.add_header("User-Agent", USER_AGENT)
        for key, value in self._headers().items():
            req.add_header(key, value)
        # The response object is bound to the generation token *before* it is
        # read, so a cancel that arrives while the read is blocked can close it
        # and make the read raise at once. Without the token this is unchanged.
        try:
            if token is not None:
                token.check()
            resp = self._opener.open(req, timeout=timeout or self.timeout)
            if token is not None:
                token.bind(resp)
            try:
                raw = resp.read()
            finally:
                if token is not None:
                    token.unbind()
                resp.close()
            if token is not None:
                token.check()
            return json.loads(raw) if raw else None
        except GenerationCancelled:
            raise
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"{self.name} -> {e.code}") from e
            if e.code == 402:
                raise ProviderAuthError(f"{self.name} -> 402 (credits exhausted)") from e
            if e.code in (429,) or e.code >= 500:
                raise ProviderUnavailable(f"{self.name} -> {e.code}",
                                          retry_after=_retry_after(e.headers)) from e
            verdict = self._classify_4xx(e)
            if verdict == "auth":
                # A refused credential in a 400: retrying cannot help, so it is
                # reported as auth and the cascade skips this provider for a while.
                raise ProviderAuthError(f"{self.name} -> {e.code}: the key was refused") from e
            if e.code == 413 or verdict == "too_large":
                raise ProviderTooLarge(
                    f"{self.name} -> {e.code}: the request is too large for the model") from e
            raise ProviderRejected(f"{self.name} -> {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            # A closed response (the generation was stopped mid-read) surfaces
            # as one of these; report it as a cancellation, not a provider
            # failure, so the caller saves the partial text and stays usable.
            if token is not None and token.cancelled:
                raise GenerationCancelled(token.partial) from e
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e

    @staticmethod
    def _classify_4xx(error: urllib.error.HTTPError) -> str:
        """What a 4xx body says the problem is: ``auth``, ``too_large`` or ``rejected``.

        The status alone does not settle it: Google answers ``400 API_KEY_INVALID``
        for a refused key, and a provider says "too large" in prose. Only a
        bounded prefix is read, and it is used for this one decision - an error
        body can echo the prompt, so it is never kept or logged.
        """
        try:
            body = error.read(4000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an unreadable body decides nothing
            return "rejected"
        if _BAD_KEY_BODY.search(body):
            return "auth"
        if _TOO_LARGE_BODY.search(body):
            return "too_large"
        return "rejected"

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

    def complete_text(self, prompt: str, system: str = "", *,
                      token: Any = None) -> str:
        """Call the provider and return the raw text of its answer.

        Used by the chat surface, which needs prose, not a plan. The retry
        policy is identical: only a transient failure is retried, bounded.
        *token*, when given, is a ``GenerationToken``: a cancel makes the call
        raise :class:`GenerationCancelled` at once instead of finishing.
        """
        return self._text_with_retry(prompt, system, prose=True, token=token)[0]

    def _text_with_retry(self, prompt: str, system: str, *, prose: bool = False,
                         timeout: float | None = None, token: Any = None) -> tuple[str, int]:
        if not self.configured:
            raise ProviderAuthError(f"{self.name}: no API key configured")
        if token is not None:
            token.check()
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
                body = self._post(prompt, system, prose=prose, timeout=timeout, token=token)
            except GenerationCancelled:
                raise
            except ProviderUnavailable as e:
                last = e
                if attempts > self.max_retries:
                    raise
                wait = self.backoff_base * (2 ** (attempts - 1))
                if e.retry_after:
                    # A wait longer than a request may block for is not spent
                    # here: the cascade records it against this provider and
                    # asks the next one, which is what having a cascade is for.
                    if e.retry_after > INLINE_WAIT_SECONDS:
                        raise
                    wait = max(wait, e.retry_after)
                self._sleep(wait)
                continue
            text = self._extract_text(body)
            if not text:
                raise ProviderRejected(f"{self.name}: empty completion")
            if token is not None:
                token.note(text)
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
        # Groq retired the Llama models; a key's own list now carries the
        # gpt-oss family. The old default answered every probe with "model not
        # in the key's list", which read as a broken key.
        kw.setdefault("model", os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
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
    """The cascade in its configured order.

    ``PROVIDER_ORDER`` (comma-separated names) is the hierarchy: the names it
    lists are tried first, in that order, and a provider it does not name keeps
    its place after them - so a typo costs ordering, never a provider. The
    default is Gemini → Groq → OpenRouter → OpenHands.

    The order decides *priority*, not exclusivity: whichever provider answers
    first wins, so an exhausted or broken one costs one failed attempt before
    the next in line serves the request.
    """
    built = [GeminiProvider(), GroqProvider(), OpenRouterProvider(),
             OpenHandsOrchestratorProvider()]
    raw = os.environ.get("PROVIDER_ORDER", "").strip()
    if not raw:
        return built
    by_name = {p.name: p for p in built}
    wanted = [name.strip().lower() for name in raw.split(",") if name.strip()]
    first = [by_name[name] for name in wanted if name in by_name]
    chosen = {p.name for p in first}
    return first + [p for p in built if p.name not in chosen]


class ProviderCascade:
    """Try providers in order; return the first valid structured plan.

    *cooldowns* is the cascade's memory: a provider that just refused a key or
    ran out of quota is skipped at once for a while instead of being retried on
    every request. It is shared by passing the same object to several cascades;
    the default is one per cascade.
    """

    def __init__(self, providers: list[Provider] | None = None, *,
                 cooldowns: Cooldowns | None = None):
        self.providers = providers if providers is not None else default_providers()
        self.cooldowns = cooldowns if cooldowns is not None else Cooldowns()

    def _attempt_order(self) -> list[Provider]:
        """Configured providers: those that are ready first, then the cooling ones.

        A cooling provider is still tried when every ready one has failed -
        being passed over is a matter of priority, never a refusal to try at all.
        """
        configured = [p for p in self.providers if p.configured]
        ready = [p for p in configured if not self.cooldowns.cooling(p.name)]
        cooling = [p for p in configured if self.cooldowns.cooling(p.name)]
        return ready + cooling

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

        The answers also refresh the cascade's memory: a probe that earned
        ``ok`` makes that provider usable at once, and one that proved a refused
        key or an unreachable host records the same cooldown a real request
        would have. ``blocked`` (no key) says nothing about reachability.
        """
        results = [p.probe() for p in self.providers]
        for result in results:
            if result.kind == "ok":
                self.cooldowns.note_success(result.provider)
            elif result.kind in ("auth", "unavailable"):
                self.cooldowns.note_failure(result.provider, result.kind)
        return results

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
        for provider in self._attempt_order():
            try:
                response = provider.complete(prompt, system, timeout=timeout)
                plan = validate_plan_json(response.data)
            except ProviderError as e:
                self.cooldowns.note_failure(provider.name, failure_kind(e),
                                            getattr(e, "retry_after", None))
                failures.append((provider.name, str(e)))
                kinds.append(failure_kind(e))
                continue
            except ValueError as e:
                failures.append((provider.name, f"invalid plan: {e}"))
                kinds.append("rejected")
                continue
            self.cooldowns.note_success(provider.name)
            return CascadeResult(response=response, plan=plan, failovers=list(failures))
        raise AllProvidersFailed(failures, kinds)

    def text(self, prompt: str, system: str = "", *,
             token: Any = None) -> ProviderResponse:
        """Ask each configured provider for prose until one answers.

        The chat surface needs a natural-language answer, not a plan. Failover
        is identical to :meth:`plan`: only provider/transport problems move to
        the next provider. The answer is returned as a
        :class:`ProviderResponse` whose ``data`` is ``{"text": ...}``, so the
        caller records the same provenance either way. *token* is a
        ``GenerationToken``: a cancel ends the answer at once, and does not
        fail over to another provider (the person asked to stop, not to try
        again elsewhere).
        """
        failures: list[tuple[str, str]] = []
        kinds: list[str] = []
        for provider in self._attempt_order():
            try:
                text = provider.complete_text(prompt, system, token=token)
            except GenerationCancelled:
                raise
            except ProviderError as e:
                self.cooldowns.note_failure(provider.name, failure_kind(e),
                                            getattr(e, "retry_after", None))
                failures.append((provider.name, str(e)))
                kinds.append(failure_kind(e))
                continue
            if not text.strip():
                failures.append((provider.name, "empty completion"))
                kinds.append("rejected")
                continue
            self.cooldowns.note_success(provider.name)
            return ProviderResponse(provider=provider.name, model=provider.model,
                                    data={"text": text})
        raise AllProvidersFailed(failures, kinds)
