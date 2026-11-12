"""How long to wait before trying again, and how many times before waiting.

A durable run never gives a task up: when something keeps going wrong it tries
less and less often instead. This module is the whole policy, as pure
functions, so the numbers live in one place and can be tested without a clock:

* Inside one **work cycle** a task gets a bounded number of attempts (its
  ``max_attempts``, three unless the run says otherwise).
* When a cycle's attempts are used up the task **waits** and then starts the
  next cycle with its attempts restored. The wait doubles from cycle to cycle
  (``NEXUS_RETRY_BASE_SECONDS``, a minute) up to a ceiling
  (``NEXUS_RETRY_MAX_SECONDS``, an hour), so a task that cannot succeed today
  costs a few starts an hour, not a few a second.
* A start the service **refuses** (HTTP 429) is not an attempt. The wait is what
  the service asked for (``Retry-After``); without that it grows with the
  number of refusals in a row.
* ``NEXUS_RETRY_MAX_CYCLES`` is the operator's ceiling on cycles per task; 0,
  the default, means no ceiling. When it is reached the task stops and the run
  waits for a person - it is not thrown away.

Waits carry a small, deterministic spread (up to a tenth, derived from the
task's id and cycle) so that many tasks that failed together do not all come
back in the same second, and so that a test can still say exactly when.
"""
from __future__ import annotations

import hashlib
import os

#: The wait after the first exhausted cycle, in seconds.
DEFAULT_BASE_SECONDS = 60
#: The longest wait between cycles, in seconds.
DEFAULT_MAX_SECONDS = 3600
#: The wait after a refused start that named no ``Retry-After``.
REFUSED_BASE_SECONDS = 60
#: The wait when the service named a daily limit: nothing changes for hours.
REFUSED_DAILY_SECONDS = 900
#: Bounds for what ``Retry-After`` may ask.
REFUSED_MIN_SECONDS = 30
REFUSED_MAX_SECONDS = 3600


def _int_env(name: str, default: int, *, least: int, most: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(most, max(least, value))


def base_seconds() -> int:
    return _int_env("NEXUS_RETRY_BASE_SECONDS", DEFAULT_BASE_SECONDS, least=1, most=86_400)


def max_seconds() -> int:
    return max(base_seconds(),
               _int_env("NEXUS_RETRY_MAX_SECONDS", DEFAULT_MAX_SECONDS, least=1, most=7 * 86_400))


def max_cycles() -> int:
    """The most cycles a task may go through, or 0 for no ceiling."""
    return _int_env("NEXUS_RETRY_MAX_CYCLES", 0, least=0, most=1_000_000)


def _spread(seed: str) -> float:
    """A stable number in [0, 0.1) for *seed*."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") / 2 ** 32) * 0.1


def cycle_wait(cycle: int, *, seed: str = "", base: int | None = None,
               ceiling: int | None = None) -> float:
    """Seconds to wait before cycle number *cycle* begins (the first wait is
    before cycle 1): ``base * 2 ** (cycle - 1)``, capped, plus the spread."""
    base = base_seconds() if base is None else base
    ceiling = max_seconds() if ceiling is None else ceiling
    steps = max(0, int(cycle) - 1)
    # 2 ** 40 minutes is longer than any ceiling; no need to compute past it.
    wait = min(float(ceiling), float(base) * (2 ** min(steps, 40)))
    return min(float(ceiling), wait * (1.0 + (_spread(f"{seed}:{cycle}") if seed else 0.0)))


def refused_wait(*, retry_after: float | None, daily: bool, refusals: int) -> float:
    """Seconds to wait after the service refused a start (HTTP 429).

    *retry_after* is what the service asked for, kept between half a minute and
    an hour. Otherwise the wait doubles with each refusal in a row: from a
    minute, or from a quarter of an hour where the service named a daily limit,
    up to an hour.
    """
    if retry_after is not None:
        return min(float(REFUSED_MAX_SECONDS), max(float(REFUSED_MIN_SECONDS), float(retry_after)))
    first = REFUSED_DAILY_SECONDS if daily else REFUSED_BASE_SECONDS
    return min(float(REFUSED_MAX_SECONDS), float(first) * (2 ** min(max(0, int(refusals)), 20)))


def span(seconds: float) -> str:
    """*seconds* in the words a status line uses."""
    if seconds >= 5400:
        return f"{seconds / 3600:.1f} h".replace(".0 h", " h")
    if seconds >= 90:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds)} s"
