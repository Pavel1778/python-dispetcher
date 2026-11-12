"""Defaults for the whole test session."""
import os

# A server advances autonomous runs itself unless NEXUS_LAB_INLINE says no
# (``orchestration.web_server.lab_inline_default``). Most tests here start a
# server to exercise something else, and many create a run to look at it as it
# was planned; a loop that started its tasks would be a second actor in every
# one of them. So for the test session the switch is off, and a test about the
# loop turns it on: ``App(..., lab_inline=True)``, or the variable itself.
os.environ.setdefault("NEXUS_LAB_INLINE", "0")

# Stored OpenHands connections may only point at public addresses
# (``orchestration.net_guard``). The tests talk to servers on 127.0.0.1, which
# is what ``NEXUS_OPENHANDS_ALLOW_PRIVATE`` exists for; the tests of the rule
# itself take it away again.
os.environ.setdefault("NEXUS_OPENHANDS_ALLOW_PRIVATE", "1")
