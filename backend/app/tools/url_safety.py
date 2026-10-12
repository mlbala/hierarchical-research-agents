"""SSRF protection for outbound page retrieval.

Two layers, both fail-closed:

1. ``validate_public_url`` checks the URL text: http/https only, standard
   ports only, no embedded credentials, no local/internal hostnames, and IP
   literals must be public.
2. ``SafeNetworkBackend`` is plugged into httpcore and performs the DNS lookup
   itself at connection time. Every resolved address must be public, and the
   socket is opened to one of *those validated addresses*. Because resolution
   and connection happen in the same step there is no window for DNS
   rebinding. TLS still uses the original hostname for SNI and certificate
   verification (httpcore passes the URL host as ``server_hostname``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpcore

from app.core.errors import UnsafeUrlError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_LENGTH = 2048

# Names that always point somewhere internal, regardless of DNS.
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal", "instance-data"}
)
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".corp",
)

# IPv6 translation prefixes that can embed (and therefore reach) IPv4
# destinations through a gateway. Python classifies some of these as global.
_NAT64_NETWORKS = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


@dataclass(frozen=True)
class ValidatedUrl:
    """A URL that passed the static checks (DNS is checked at connect time)."""

    url: str
    scheme: str
    host: str
    port: int

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
        if any(ip in net for net in _NAT64_NETWORKS):
            return False
        if ip.is_site_local:
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def validate_public_url(raw: str) -> ValidatedUrl:
    """Static URL validation. Raises ``UnsafeUrlError`` on any doubt."""
    if not isinstance(raw, str):
        raise UnsafeUrlError("URL must be a string.")
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is empty or too long.")
    if any(ord(ch) < 0x21 or ch == "\\" for ch in candidate):
        raise UnsafeUrlError("URL contains whitespace, control or backslash characters.")

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise UnsafeUrlError("Only http and https URLs are allowed.", code="UNSUPPORTED_PROTOCOL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed.")
    if port is not None and port != DEFAULT_PORTS[scheme]:
        raise UnsafeUrlError("Only standard ports (80 for http, 443 for https) are allowed.")

    hostname = parts.hostname
    if not hostname:
        raise UnsafeUrlError("URL has no host.")
    hostname = hostname.rstrip(".")
    ip = _parse_ip(hostname)
    if ip is None:
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeUrlError("Hostname is not valid.") from exc
        if "." not in hostname:
            raise UnsafeUrlError("Single-label hostnames are not allowed.")
        if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeUrlError(
                "Internal hostnames are not allowed.", code="DESTINATION_NOT_PUBLIC"
            )
    elif not is_public_ip(ip):
        raise UnsafeUrlError(
            "The destination address is not public.", code="DESTINATION_NOT_PUBLIC"
        )

    effective_port = port or DEFAULT_PORTS[scheme]
    netloc = f"[{hostname}]" if ip is not None and ip.version == 6 else hostname
    path = parts.path or "/"
    normalized = urlunsplit((scheme, netloc, path, parts.query, ""))
    return ValidatedUrl(url=normalized, scheme=scheme, host=hostname, port=effective_port)


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve with the OS resolver (A and AAAA)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


async def resolve_public_addresses(
    host: str, port: int, resolver: Resolver, *, resolve_seconds: float = 10.0
) -> list[str]:
    """Resolve ``host`` and require that *every* address is public.

    Rejecting the whole name when any record is private prevents attacks that
    mix public and internal records and rely on address-selection order.
    """
    literal = _parse_ip(host.strip("[]"))
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeUrlError(
                "The destination address is not public.", code="DESTINATION_NOT_PUBLIC"
            )
        return [str(literal)]
    try:
        addresses = await asyncio.wait_for(resolver(host, port), timeout=resolve_seconds)
    except (OSError, TimeoutError) as exc:
        raise UnsafeUrlError(
            "The destination could not be resolved.", code="DESTINATION_UNRESOLVED"
        ) from exc
    if not addresses:
        raise UnsafeUrlError(
            "The destination could not be resolved.", code="DESTINATION_UNRESOLVED"
        )
    validated: list[str] = []
    for address in addresses:
        if "%" in address:  # scoped (link-local) IPv6 addresses
            raise UnsafeUrlError(
                "The destination address is not public.", code="DESTINATION_NOT_PUBLIC"
            )
        ip = _parse_ip(address)
        if ip is None or not is_public_ip(ip):
            raise UnsafeUrlError(
                "The destination address is not public.", code="DESTINATION_NOT_PUBLIC"
            )
        validated.append(str(ip))
    return validated


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that only connects to validated public IPs."""

    def __init__(
        self,
        resolver: Resolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()
        # Addresses actually dialled; useful for tests and diagnostics.
        self.connected_addresses: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in DEFAULT_PORTS.values():
            raise UnsafeUrlError("Only standard ports are allowed.")
        addresses = await resolve_public_addresses(host, port, self._resolver)
        last_error: Exception | None = None
        for address in addresses:
            try:
                stream = await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
                continue
            self.connected_addresses.append((address, port))
            return stream
        raise last_error or httpcore.ConnectError("No address could be connected.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeUrlError("Unix sockets are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)
