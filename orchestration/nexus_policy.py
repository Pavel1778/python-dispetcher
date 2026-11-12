"""The NEXUS permission policy: scopes, RBAC, SSRF and log redaction.

Section 10 of the acceptance text asks for one thing above all: *every* external
and potentially dangerous operation must obey the NEXUS permission policy. This
module is that policy, in one place, with no dependency on the store so any
layer (HTTP, MCP, CLI, the worker) can consult it.

It holds four things:

1. **Roles and scopes** — a project member has a role (``owner`` or ``member``)
   and each role carries a set of scopes. A service asks "may this role do X?",
   never "is this person an owner?" scattered through the code.
2. **MCP tool permissions** — every MCP tool declares the scope it needs, so an
   MCP caller has exactly the authority of an authenticated NEXUS user and no
   more. A tool with no scope entry cannot run.
3. **SSRF guard** — ``check_outbound_url`` refuses a URL that would make the
   server speak to itself, to a private network, or to a cloud metadata
   endpoint. Loopback and RFC1918 targets are allowed only when the operator
   opts in (``NEXUS_ALLOW_PRIVATE_TARGETS``), which a test session does because
   its fakes listen on 127.0.0.1; a production deployment does not.
4. **Secret redaction** — a ``logging.Filter`` that scrubs credential-looking
   values out of a log record before it is emitted, so a stray key in a message
   never reaches a log file.

Nothing here prints or returns a secret: ``redact_text`` only ever replaces.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from urllib.parse import urlsplit

# --------------------------------------------------------------------- roles
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_MEMBER)

# -------------------------------------------------------------------- scopes
SCOPE_PROJECT_READ = "project:read"
SCOPE_PROJECT_WRITE = "project:write"
SCOPE_PROJECT_ADMIN = "project:admin"
SCOPE_TASK_DISPATCH = "task:dispatch"
SCOPE_RUN_CONTROL = "run:control"
SCOPE_ACCOUNT_ADMIN = "account:admin"

#: What each role may do. A ``member`` works inside a project; only an
#: ``owner`` may change what the project *is* (its repository binding, its
#: plans, its membership) or administer accounts.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset({
        SCOPE_PROJECT_READ, SCOPE_PROJECT_WRITE, SCOPE_PROJECT_ADMIN,
        SCOPE_TASK_DISPATCH, SCOPE_RUN_CONTROL, SCOPE_ACCOUNT_ADMIN,
    }),
    ROLE_MEMBER: frozenset({
        SCOPE_PROJECT_READ, SCOPE_PROJECT_WRITE, SCOPE_TASK_DISPATCH,
    }),
}

#: Every MCP tool and the scope it needs. A tool absent from this table is
#: refused rather than run, so adding a tool without declaring its authority is
#: a bug the test suite catches.
MCP_TOOL_SCOPES: dict[str, str] = {
    "list_projects": SCOPE_PROJECT_READ,
    "get_project": SCOPE_PROJECT_READ,
    "list_repositories": SCOPE_PROJECT_READ,
    "list_tasks": SCOPE_PROJECT_READ,
    "get_task": SCOPE_PROJECT_READ,
    "list_evidence": SCOPE_PROJECT_READ,
    "verify_evidence": SCOPE_PROJECT_READ,
    "create_task": SCOPE_PROJECT_WRITE,
    "dispatch_task": SCOPE_TASK_DISPATCH,
    "get_execution_status": SCOPE_PROJECT_READ,
}


class PolicyViolation(PermissionError):
    """An operation was refused by the NEXUS permission policy."""


class PermissionDenied(PolicyViolation):
    """The caller's role does not carry the scope an operation needs."""


class UnsafeTarget(PolicyViolation):
    """An outbound URL points at something the SSRF policy refuses."""


def role_scopes(role: str) -> frozenset[str]:
    """The scopes a role carries. An unknown role carries none."""
    return ROLE_SCOPES.get(role, frozenset())


def allows(role: str | None, scope: str) -> bool:
    """Whether *role* carries *scope*. A missing role carries nothing."""
    return scope in role_scopes(role or "")


def require(role: str | None, scope: str) -> None:
    """Raise :class:`PermissionDenied` unless *role* carries *scope*."""
    if not allows(role, scope):
        raise PermissionDenied(
            f"role {role or '(none)'} lacks scope {scope}")


def scopes_for_tool(tool_name: str) -> str:
    """The scope an MCP tool needs. Raises for an undeclared tool."""
    try:
        return MCP_TOOL_SCOPES[tool_name]
    except KeyError:
        raise PolicyViolation(f"MCP tool {tool_name!r} declares no scope") from None


def require_tool_scope(role: str | None, tool_name: str) -> None:
    """Refuse an MCP tool call whose scope the caller's role does not carry."""
    require(role, scopes_for_tool(tool_name))


# ----------------------------------------------------------------------- SSRF
#: Host names that never legitimately name an external provider endpoint.
_INTERNAL_NAMES = frozenset({
    "localhost", "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "kubernetes.default", "kubernetes.default.svc",
})
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

#: Individual metadata addresses not covered by ``is_link_local``/``is_private``.
_METADATA_ADDRESSES = frozenset({"169.254.169.254", "100.100.100.200",
                                 "fd00:ec2::254"})


def _allow_private_targets() -> bool:
    """Whether loopback/RFC1918 targets are permitted for this process.

    Off by default: a production server must not be pointed at its own network
    by an operator-supplied URL. A test session turns it on because its fake
    Cloud/GitHub/provider servers listen on 127.0.0.1.
    """
    return os.environ.get("NEXUS_ALLOW_PRIVATE_TARGETS", "").strip().lower() in (
        "1", "true", "yes", "on")


def _is_internal_host(host: str) -> tuple[bool, str]:
    """Return ``(internal, why)`` for a host name or literal address."""
    name = host.strip().strip("[]").lower().rstrip(".")
    if not name:
        return True, "empty host"
    if name in _INTERNAL_NAMES or any(name.endswith(s) for s in _INTERNAL_SUFFIXES):
        return True, "internal host name"
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False, ""  # a normal DNS name; resolution policy is the network's
    if str(address) in _METADATA_ADDRESSES:
        return True, "cloud metadata address"
    if address.is_loopback:
        return True, "loopback address"
    if address.is_link_local:
        return True, "link-local address"
    if address.is_unspecified:
        return True, "unspecified address"
    if address.is_private or address.is_reserved:
        return True, "private or reserved address"
    return False, ""


def check_outbound_url(url: str, *, allow_private: bool | None = None) -> str:
    """Return *url* if it is safe to call out to, else raise :class:`UnsafeTarget`.

    Refuses, always: a non-http(s) scheme, a URL carrying credentials, a cloud
    metadata address, and (unless private targets are permitted) a loopback,
    link-local or private-network host. ``urlopen`` would otherwise honour a
    ``file://`` URL, send the stored key to a metadata endpoint, or let an
    operator aim the server at its own network.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeTarget("URL is required")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeTarget(
            f"only http(s) URLs may be called out to, got {parts.scheme or '(none)'}")
    if not parts.hostname:
        raise UnsafeTarget("URL has no host")
    if parts.username or parts.password:
        raise UnsafeTarget("URL must not carry credentials")
    internal, why = _is_internal_host(parts.hostname)
    if internal:
        permitted = _allow_private_targets() if allow_private is None else allow_private
        # A metadata endpoint is never permitted, even with private targets on.
        if "metadata" in why or not permitted:
            raise UnsafeTarget(f"refusing to call {why}: {parts.hostname}")
    return url.strip()


# ------------------------------------------------------------------ redaction
_REDACTED = "«redacted»"

#: Credential shapes worth scrubbing even when the value is not in the
#: environment (a token echoed by a remote service, a header in a traceback).
_SECRET_PATTERNS = (
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # user:password@ in a URL
)

#: Environment variables whose *value* must never appear in a log line.
SECRET_ENV_NAMES = (
    "OPENHANDS_API_KEY", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN",
    "WEB_AUTH_TOKEN", "AUTH_PASSWORD_HASH", "AUTH_SESSION_SECRET",
    "NEXUS_SECRET_KEY", "NEXUS_EVIDENCE_PRIVATE_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "SUPABASE_ACCESS_TOKEN",
    "SUPABASE_DB_URL", "NEXUS_DB_URL", "FROM_API_KEY",
)


def _known_secrets() -> list[str]:
    """Values from the environment that must be scrubbed from log text."""
    values = []
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        # A one- or two-character value would redact unrelated text; only a
        # value long enough to be a real credential is worth scrubbing.
        if len(value) >= 8:
            values.append(value)
    return values


def redact_text(text: str) -> str:
    """Replace credential-looking substrings in *text* with a placeholder."""
    if not text:
        return text
    for value in _known_secrets():
        if value in text:
            text = text.replace(value, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs secrets from every record it sees."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib name
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad record must not break logging
            return True
        cleaned = redact_text(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach the redaction filter to *logger* (root by default). Idempotent."""
    target = logger or logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, RedactingFilter):
            return existing
    flt = RedactingFilter()
    target.addFilter(flt)
    return flt


# ------------------------------------------------------------------- commands
def safe_argv(argv: list[str]) -> list[str]:
    """Validate an argument vector for a subprocess call.

    NEXUS runs exactly one external program (``git``, from the restore-point
    module) and always passes an argument *list*, never a shell string. This
    helper exists so that rule is checkable: it refuses a non-list, a
    non-string member, or an empty command, so no call site can drift into
    ``shell=True`` with an interpolated string.
    """
    if not isinstance(argv, list) or not argv:
        raise PolicyViolation("a command must be a non-empty argument list")
    for item in argv:
        if not isinstance(item, str) or not item:
            raise PolicyViolation("every command argument must be a non-empty string")
    return list(argv)


def policy_report() -> dict:
    """What the policy is, for an operator. Never a secret value."""
    return {
        "roles": {role: sorted(scopes) for role, scopes in ROLE_SCOPES.items()},
        "mcp_tool_scopes": dict(MCP_TOOL_SCOPES),
        "ssrf": {
            "allow_private_targets": _allow_private_targets(),
            "metadata_addresses": sorted(_METADATA_ADDRESSES),
            "internal_names": sorted(_INTERNAL_NAMES),
        },
        "log_redaction": {
            "secret_env_names": list(SECRET_ENV_NAMES),
            "patterns": len(_SECRET_PATTERNS),
        },
    }
