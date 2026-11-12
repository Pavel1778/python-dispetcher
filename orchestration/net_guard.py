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
That is the operator's decision, not the caller's. It is made in the server's
environment and read through :mod:`orchestration.nexus_policy`, which holds the
policy for every outbound target - this module adds no switch of its own:

* ``NEXUS_OPENHANDS_ALLOWED_HOSTS`` - a comma-separated list of ``host`` or
  ``host:port`` that may be private (``openhands.internal, 10.0.0.5:3000``);
* ``NEXUS_ALLOW_PRIVATE_TARGETS=1`` - any private address may be used. For a
  deployment that lives entirely on a trusted network, and for the tests,
  which talk to servers on 127.0.0.1.

A cloud metadata address is refused whatever was permitted.

``nexus_policy.check_outbound_url`` looks at a URL as it is written, for every
base URL the server is configured with. This module goes further for a URL a
person supplied through the API: it looks the name up and checks the addresses,
when the connection is saved and again when a socket is opened.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import nexus_policy


class AddressRefused(ValueError):
    """A URL or an address this server will not connect to."""


@dataclass(frozen=True)
class AddressPolicy:
    """What an outgoing connection may reach."""

    allow_private: bool = False
    #: ``host`` or ``host:port``, lower case: these may resolve to anything.
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "AddressPolicy":
        """The operator's decisions, read where the policy is kept
        (``nexus_policy``): ``NEXUS_ALLOW_PRIVATE_TARGETS`` and
        ``NEXUS_OPENHANDS_ALLOWED_HOSTS``."""
        return cls(allow_private=nexus_policy.private_targets_allowed(),
                   allowed_hosts=nexus_policy.allowed_private_hosts())

    def exempt(self, host: str, port: int | None) -> bool:
        return self.allow_private or nexus_policy.host_listed(self.allowed_hosts, host, port)

    def check_address(self, host: str, port: int | None, address: str) -> None:
        """Raise :class:`AddressRefused` unless *address* may be connected to
        on behalf of *host*."""
        reason = private_reason(address)
        if not reason:
            return
        ip = _unwrapped(address)
        if ip is not None and nexus_policy.is_metadata_address(str(ip)):
            # Never, whatever the operator permitted: nothing a connection is
            # for lives there, and what does live there is the server's own
            # credentials.
            raise AddressRefused(
                f"{host} resolves to {address}, which is a cloud metadata address; "
                "this server never connects to it")
        if self.exempt(host, port):
            return
        raise AddressRefused(
            f"{host} resolves to {address}, which is {reason}; this server does not "
            "connect to such addresses for a connection added through the API")


def _unwrapped(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """*address* as an IP object, with an IPv4 address taken out of the IPv6
    forms that carry one (mapped, 6to4, Teredo); ``None`` when it is not an IP
    address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip.teredo is not None:
            inner = ip.teredo[1]
        if inner is not None:
            return inner
    return ip


def private_reason(address: str) -> str:
    """Why *address* is not a public one, or ``""`` when it is."""
    ip = _unwrapped(address)
    if ip is None:
        return "not an IP address"
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
