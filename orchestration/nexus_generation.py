"""Stop generation: cancel the AI answer in flight, keep what it produced.

One AI answer is one *generation* (``nexus_generations``). This module is the
in-process side of it: a :class:`GenerationToken` the answer carries, which a
cancel request can trip. The durable row is the store's; the token is the
signal, and the two are joined by the generation id.

Why a token and not a database flag alone: the answer is produced by a blocking
provider call inside the request's thread. A flag written to PostgreSQL would
only be seen the *next* time the answer checks, which for a single call is when
it is already over. The token therefore also holds the live HTTP response of the
provider call, and :meth:`GenerationToken.cancel` closes it - which makes the
blocked read raise at once, so a cancel is immediate and not "after the model
finishes", which the requirement forbids.

What cancellation is *not*: it is not a run cancel. Stopping a text answer
leaves any autonomous run of the same project alone, and cancelling a run does
not stop an unrelated answer. The two actions are separate on purpose (the
requirement's "Stop AI Response" versus "Cancel Run").

The token is honest about what a single, non-streamed completion can keep: an
answer that is not yet returned has no partial text, so what is saved is the
partial text produced by the rounds that did finish (``partial``), or nothing,
and the message says it was stopped. A streamed transport could keep more; this
one keeps exactly what it has.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class GenerationCancelled(Exception):
    """Raised inside an answer whose generation was cancelled.

    Carries whatever partial text had been produced, so the caller can save it
    with the cancellation mark instead of losing it.
    """

    def __init__(self, partial: str = ""):
        super().__init__("generation cancelled")
        self.partial = partial


class GenerationToken:
    """A cancellable answer. One per running generation.

    ``cancel()`` is safe to call from another thread (the request that received
    the STOP). It trips the event and closes the bound response, if any, so a
    blocked provider read raises immediately.
    """

    def __init__(self, generation_id: str, *, on_cancel: Callable[[], None] | None = None):
        self.generation_id = generation_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._response: Any = None
        self._closer: Callable[[], None] | None = None
        self._on_cancel = on_cancel
        self.partial = ""

    # ------------------------------------------------------------- the signal
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise :class:`GenerationCancelled` if the answer was stopped."""
        if self._event.is_set():
            raise GenerationCancelled(self.partial)

    def cancel(self) -> None:
        """Stop the answer now: trip the signal and abort the live request."""
        already = self._event.is_set()
        self._event.set()
        with self._lock:
            closer, response = self._closer, self._response
        # Close the in-flight response first so a blocked read wakes at once.
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing a socket must never raise
                pass
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        if not already and self._on_cancel is not None:
            try:
                self._on_cancel()
            except Exception:  # noqa: BLE001 - the record is a side effect
                pass

    # ---------------------------------------------------------- the transport
    def bind(self, response: Any) -> None:
        """Bind the live provider response, so a cancel can abort it."""
        with self._lock:
            self._response = response
        # A cancel that arrived a moment ago is honoured at once.
        if self._event.is_set():
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def unbind(self) -> None:
        with self._lock:
            self._response = None

    def set_closer(self, closer: Callable[[], None]) -> None:
        with self._lock:
            self._closer = closer

    def note(self, text: str) -> None:
        """Remember partial text produced so far, for a cancelled answer."""
        if text:
            self.partial = text


class GenerationRegistry:
    """The running answers of this process, by generation id.

    In-process on purpose: a token holds a live socket and cannot be shared
    between processes. A cancel for a generation this process does not hold
    (another worker owns it, or it already ended) finds no token and reports
    that nothing was stopped - it never claims to have stopped an answer it
    cannot reach.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, GenerationToken] = {}
        self._lock = threading.Lock()

    def register(self, token: GenerationToken) -> GenerationToken:
        with self._lock:
            self._tokens[token.generation_id] = token
        return token

    def unregister(self, generation_id: str) -> None:
        with self._lock:
            self._tokens.pop(generation_id, None)

    def get(self, generation_id: str) -> GenerationToken | None:
        with self._lock:
            return self._tokens.get(generation_id)

    def cancel(self, generation_id: str) -> bool:
        """Cancel *generation_id* if this process is running it. ``True`` if so."""
        token = self.get(generation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def running(self) -> list[str]:
        with self._lock:
            return list(self._tokens)
