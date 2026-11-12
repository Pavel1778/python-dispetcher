"""Defaults for the whole test session."""
import os

# A server advances autonomous runs itself unless NEXUS_LAB_INLINE says no
# (``orchestration.web_server.lab_inline_default``). Most tests here start a
# server to exercise something else, and many create a run to look at it as it
# was planned; a loop that started its tasks would be a second actor in every
# one of them. So for the test session the switch is off, and a test about the
# loop turns it on: ``App(..., lab_inline=True)``, or the variable itself.
os.environ.setdefault("NEXUS_LAB_INLINE", "0")

# The SSRF policy refuses loopback/private targets unless the operator opts in.
# A test session's fake Cloud, GitHub and provider servers all listen on
# 127.0.0.1, so the session opts in here; the security tests clear the variable
# to prove the guard refuses a private target. A production deployment leaves it
# unset, so a base URL aimed at its own network is refused.
os.environ.setdefault("NEXUS_ALLOW_PRIVATE_TARGETS", "1")
