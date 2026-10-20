from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..domain.errors import UnsafeEndpointError

Resolver = Callable[[str, int], Sequence[str]]

_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$", re.IGNORECASE)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_PRIVATE_MESSAGE = "Private MCP endpoints require explicit deployment opt-in"


def system_resolver(host: str, port: int) -> Sequence[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndpointSecurityPolicy:
    """Outbound endpoint policy applied at registration (syntax) and at connect (DNS)."""

    require_tls: bool = True
    allow_private_networks: bool = False
    allowed_hosts: frozenset[str] | None = None
    resolver: Resolver = system_resolver

    @classmethod
    def local_development(cls) -> EndpointSecurityPolicy:
        """Explicit opt-in for plain-HTTP and loopback/private MCP servers during development."""
        return cls(require_tls=False, allow_private_networks=True)

    def validate(self, endpoint: str, *, resolve: bool = True) -> None:
        try:
            parsed = urlsplit(endpoint)
            hostname = parsed.hostname
            _ = parsed.port  # raises ValueError for malformed ports
        except ValueError as error:
            raise UnsafeEndpointError("MCP endpoint is not a valid URL") from error
        if self.require_tls and parsed.scheme != "https":
            raise UnsafeEndpointError("MCP endpoint must use HTTPS")
        if parsed.scheme not in ("http", "https") or not hostname:
            raise UnsafeEndpointError("MCP endpoint is not a valid HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeEndpointError("MCP endpoint must not contain user information")
        if parsed.fragment:
            raise UnsafeEndpointError("MCP endpoint must not contain a fragment")

        host = hostname.lower().rstrip(".")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise UnsafeEndpointError(f"MCP endpoint host {host} is not in the allowlist")
        if not self.allow_private_networks and (
            host == "localhost" or host.endswith(_PRIVATE_SUFFIXES)
        ):
            raise UnsafeEndpointError(_PRIVATE_MESSAGE)

        literal = _literal_address(host)
        if literal is not None:
            if not self.allow_private_networks:
                self._require_global(literal)
            return
        if _NUMERIC_HOST.match(host):
            raise UnsafeEndpointError("MCP endpoint host is an unsupported numeric address form")
        if not resolve:
            return
        self.resolve_endpoint(endpoint)

    def resolve_endpoint(self, endpoint: str) -> ResolvedEndpoint:
        """Resolve once and return the exact addresses a transport may connect to."""
        self.validate(endpoint, resolve=False)
        parsed = urlsplit(endpoint)
        assert parsed.hostname is not None
        host = parsed.hostname.lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        literal = _literal_address(host)
        if literal is not None:
            addresses = (literal,)
        else:
            try:
                addresses = tuple(
                    dict.fromkeys(_strip_zone(item) for item in self.resolver(host, port))
                )
            except OSError as error:
                raise UnsafeEndpointError(f"MCP endpoint host {host} did not resolve") from error
        if not addresses:
            raise UnsafeEndpointError(f"MCP endpoint host {host} did not resolve")
        if not self.allow_private_networks:
            for address in addresses:
                self._require_global(address)
        return ResolvedEndpoint(hostname=host, port=port, addresses=addresses)

    def _legacy_resolve(self, host: str, port: int) -> None:
        """Kept private for stable error semantics in older callers."""
        try:
            addresses = self.resolver(host, port)
        except OSError as error:
            raise UnsafeEndpointError(f"MCP endpoint host {host} did not resolve") from error
        if not addresses:
            raise UnsafeEndpointError(f"MCP endpoint host {host} did not resolve")
        for address in addresses:
            self._require_global(_strip_zone(address))

    @staticmethod
    def _require_global(address: str) -> None:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise UnsafeEndpointError("MCP endpoint resolved to an invalid address") from error
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            parsed = parsed.ipv4_mapped
        if not parsed.is_global or parsed.is_multicast:
            raise UnsafeEndpointError(_PRIVATE_MESSAGE)


def _strip_zone(address: str) -> str:
    return address.split("%", 1)[0]


def _literal_address(host: str) -> str | None:
    """Return a canonical IP for literal hosts, including legacy inet_aton forms."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if _NUMERIC_HOST.match(host):
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return None
    return None
