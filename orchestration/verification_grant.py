"""Operator-configured, expiring verification capability. Never accepted as the web bearer."""

from dataclasses import dataclass, field
import hashlib
import hmac
import os
import time
from uuid import UUID


@dataclass(frozen=True)
class VerificationGrant:
    project: str
    principal: str
    expires: int
    digest: bytes = field(repr=False)

    @classmethod
    def from_env(cls, *, owner_token=None):
        token = os.getenv("NEXUS_MCP_VERIFY_TOKEN")
        if not token:
            return None
        principal = os.getenv("NEXUS_MCP_VERIFY_PRINCIPAL", "").strip()
        try:
            project = str(UUID(os.getenv("NEXUS_MCP_VERIFY_PROJECT", "")))
            expires = int(os.getenv("NEXUS_MCP_VERIFY_EXPIRES", ""))
            if (
                len(token) < 32
                or not token.isascii()
                or token == owner_token
                or not principal
                or expires <= 0
            ):
                raise ValueError()
        except ValueError:
            raise ValueError(
                "invalid verifier grant; requires a distinct token, principal, project UUID and expiry"
            ) from None
        return cls(
            project,
            principal,
            expires,
            hashlib.sha256(("Bearer " + token).encode()).digest(),
        )

    def accepts(self, header):
        return (
            time.time() < self.expires
            and isinstance(header, str)
            and hmac.compare_digest(
                hashlib.sha256(header.encode()).digest(), self.digest
            )
        )
