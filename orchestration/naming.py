"""Human-readable, deterministic conversation names.

Why this module exists
----------------------
Neither API honours a ``title`` sent at **creation** time, so a name supplied
there is silently lost. Verified against both live servers:

* The local agent-server's ``StartConversationRequest`` has no ``title`` field
  at all. ``POST /api/conversations`` answers ``201`` and the conversation keeps
  ``title: None``.
* OpenHands Cloud *does* accept ``title`` and echoes it back inside the start
  task's ``request``, but the conversation that eventually appears carries an
  **auto-generated** title derived from the first user message (``autotitle``
  defaults to true). A Sr conversation was therefore named
  ``"You are the senior (Sr) agent for this workflow..."`` instead of
  ``Sr.<goal>``, and a Jr conversation was named after its goal text -- so two
  tasks with the same goal were indistinguishable in the Cloud list.

``PATCH`` is the only call that sticks, so a name must be applied *after* the
conversation is READY. These helpers build the name; the callers apply it at
that point.

The short reference suffix is deliberate. The same goal submitted twice
produced rows that looked identical, which is what made the earlier duplicate
incident hard to read; the reference makes every conversation traceable back to
the task or workflow that owns it.
"""

from __future__ import annotations

#: Cloud rejects nothing at this length, and the local rename route caps at 200.
#: The bound keeps the name readable in the UI rather than at the API limit.
_GOAL_LIMIT = 80
_REF_LIMIT = 8


def _one_line(text: str | None, limit: int) -> str:
    """Collapse whitespace so a multi-line goal cannot break the name."""
    return " ".join((text or "").split())[:limit]


def conversation_title(kind: str, goal: str | None, *, ref: str) -> str:
    """A stable, readable name such as ``Sr.Audit the parser [fede8a2f]``.

    ``kind`` is ``Sr`` or ``Jr``; ``ref`` is the owning workflow or task id.
    """
    short = (ref or "").replace("-", "")[:_REF_LIMIT]
    label = _one_line(goal, _GOAL_LIMIT) or "workflow"
    return f"{kind}.{label} [{short}]"
