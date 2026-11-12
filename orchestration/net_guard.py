"""Where an outgoing HTTP request may go, when a person supplied the URL.

An OpenHands connection is added through the API with a URL of the caller's
choosing, and from then on this server sends requests there carrying a key.
Without a rule that URL is a way to make the server talk to anything it can
reach and the caller cannot: the cloud metadata address, a database port on
localhost, an admin panel on the private network (server-side request forgery).

The rule, in one place:

* only ``http`` and ``https``, a host, no user or password in the URL, no
  query and no fragment;
* every address the host resolves to must be a public one. Loopback, private
  and link-local ranges, carrier-grade NAT, multicast, the unspecified address
  and anything else that is not globally routable are refused - for IPv4, IPv6
  and IPv4 addresses wrapped in IPv6;
* the check is made **again at the moment of connecting**, on the address the
  socket is actually opened to (:class:`GuardedHTTPConnection`). A name that
  resolved to a public address when the connection was saved and resolves to
  ``127.0.0.1`` a minute later (DNS rebinding) is refused then;
* redirects are not this module's business: the clients that use it refuse
  them already, so a public host cannot bounce a request inward either.

A self-hosted OpenHands on a private network is a legitimate thing to connect.
That is the operator's decision, not the caller's, and it is made in the
server's environment:

* ``NEXUS_OPENHANDS_ALLOWED_HOSTS`` - a comma-separated list of ``host`` or
  ``host:port`` that may be private (``openhands.internal, 10.0.0.5:3000``);
* ``NEXUS_OPENHANDS_ALLOW_PRIVATE=1`` - any private address may be used. For a
  deployment that lives entirely on a trusted network, and for the tests,
  which talk to servers on 127.0.0.1.

The server's own ``OPENHANDS_CLOUD_URL`` is not checked here: it comes from the
same environment and is trusted for the same reason.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class AddressRefused(ValueError):
    """A URL or an address this server will not connect to."""


def _host_key(host: str, port: int | None) -> tuple[str, str]:
    host = host.strip().strip("[]").lower().rstrip(".")
    return host, f"{host}:{port}" if port else host


@dataclass(frozen=True)
class AddressPolicy:
    """What an outgoing connection may reach."""

    allow_private: bool = False
    #: ``host`` or ``host:port``, lower case: these may resolve to anything.
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "AddressPolicy":
        allow = os.environ.get("NEXUS_OPENHANDS_ALLOW_PRIVATE", "").strip().lower() in (
            "1", "true", "yes", "on")
        hosts = frozenset(
            item.strip().strip("[]").lower().rstrip(".")
            for item in os.environ.get("NEXUS_OPENHANDS_ALLOWED_HOSTS", "").split(",")
            if item.strip())
        return cls(allow_private=allow, allowed_hosts=hosts)

    def exempt(self, host: str, port: int | None) -> bool:
        if self.allow_private:
            return True
        name, with_port = _host_key(host, port)
        return name in self.allowed_hosts or with_port in self.allowed_hosts

    def check_address(self, host: str, port: int | None, address: str) -> None:
        """Raise :class:`AddressRefused` unless *address* may be connected to
        on behalf of *host*."""
        if self.exempt(host, port):
            return
        reason = private_reason(address)
        if reason:
            raise AddressRefused(
                f"{host} resolves to {address}, which is {reason}; this server does not "
                "connect to such addresses for a connection added through the API")


def private_reason(address: str) -> str:
    """Why *address* is not a public one, or ``""`` when it is."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is None and ip.teredo is not None:
            mapped = ip.teredo[1]
        if mapped is not None:
            return private_reason(str(mapped))
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_private:
        return "a private-network address"
    if ip.is_reserved:
        return "a reserved address"
    if not ip.is_global:
        return "not a public address"
    return ""


def check_url(url: Any, *, policy: AddressPolicy | None = None, resolve: bool = True) -> str:
    """Return *url* normalised when a connection may use it, else raise.

    The form is checked always; with *resolve* the host is also looked up and
    every address it has must pass the policy. A host that does not resolve is
    refused: a connection nobody can reach is not worth saving, and "it will
    resolve later" is how a rebinding starts.
    """
    if not isinstance(url, str) or not url.strip():
        raise AddressRefused("the URL is empty")
    url = url.strip()
    if len(url) > 2000 or any(ord(ch) < 0x20 or ch == "\x7f" or ch.isspace() for ch in url):
        raise AddressRefused("the URL contains characters a URL cannot have")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise AddressRefused(f"the URL cannot be read: {e}") from None
    if parts.scheme not in ("http", "https"):
        raise AddressRefused("the URL must begin with https:// (or http://)")
    if parts.username is not None or parts.password is not None:
        raise AddressRefused("the URL must not carry a user name or a password; "
                             "the key is given separately")
    if parts.query or parts.fragment:
        raise AddressRefused("the URL must not have a query or a fragment")
    host = parts.hostname
    if not host:
        raise AddressRefused("the URL names no host")
    policy = policy or AddressPolicy.from_env()
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if resolve:
        for address in resolve_host(host, port):
            policy.check_address(host, port, address)
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            policy.check_address(host, port, host)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def resolve_host(host: str, port: int) -> list[str]:
    """Every address *host* has, as text. Raises :class:`AddressRefused` when
    it has none."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise AddressRefused(f"{host} does not resolve: {e}") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise AddressRefused(f"{host} does not resolve to any address")
    return addresses


def _connect(host: str, port: int, timeout: Any, source_address: Any,
             policy: AddressPolicy) -> socket.socket:
    """Open a TCP connection to *host*, to an address the policy allows.

    Every address the name has is checked before any is tried, and the socket
    is opened to one of exactly those addresses: there is no second lookup
    between the check and the connect for an attacker to answer differently.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise OSError(f"{host} does not resolve: {e}") from e
    if not infos:
        raise OSError(f"{host} does not resolve to any address")
    for info in infos:
        policy.check_address(host, port, info[4][0])
    last: Exception | None = None
    for family, kind, proto, _canon, address in infos:
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(address)
            return sock
        except OSError as e:
            last = e
            if sock is not None:
                sock.close()
    raise last if last is not None else OSError(f"could not connect to {host}")


class GuardedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that only opens to addresses its policy allows."""

    def __init__(self, *args: Any, policy: AddressPolicy, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._policy = policy

    def connect(self) -> None:
        self.sock = _connect(self.host, self.port, self.timeout, self.source_address,
                             self._policy)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self._tunnel()  # type: ignore[attr-defined]


class GuardedHTTPSConnection(http.client.HTTPSConnection):
    """The same over TLS. The certificate is still checked against the name."""

    def __init__(self, *args: Any, policy: AddressPolicy, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._policy = policy

    def connect(self) -> None:
        sock = _connect(self.host, self.port, self.timeout, self.source_address, self._policy)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        if self._tunnel_host:
            self._tunnel()  # type: ignore[attr-defined]
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)  # type: ignore[attr-defined]


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, policy: AddressPolicy):
        super().__init__()
        self._policy = policy

    def http_open(self, req):  # noqa: D102 - urllib's handler protocol
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> GuardedHTTPConnection:
        return GuardedHTTPConnection(host, policy=self._policy, **kwargs)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, policy: AddressPolicy):
        super().__init__()
        self._policy = policy

    def https_open(self, req):  # noqa: D102 - urllib's handler protocol
        return self.do_open(self._connection, req, context=self._context)  # type: ignore[attr-defined]

    def _connection(self, host: str, **kwargs: Any) -> GuardedHTTPSConnection:
        return GuardedHTTPSConnection(host, policy=self._policy, **kwargs)


class _NoProxy(urllib.request.ProxyHandler):
    """No proxy: a request that leaves through one is connected by the proxy,
    and this module's check of the address would not be the check that counts."""

    def __init__(self) -> None:
        super().__init__({})


def guarded_handlers(policy: AddressPolicy) -> list[urllib.request.BaseHandler]:
    """The handlers that make an opener connect only where *policy* allows.

    Give them to ``urllib.request.build_opener`` together with the caller's own
    (a redirect rule, for one).
    """
    return [_NoProxy(), _GuardedHTTPHandler(policy), _GuardedHTTPSHandler(policy)]
