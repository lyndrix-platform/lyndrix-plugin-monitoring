"""SSRF guard for active probers.

Monitors legitimately target internal/private infrastructure, so we do NOT
block RFC1918 ranges. We DO block loopback: this plugin runs IN-PROCESS
inside lyndrix-core, so loopback resolves to the core/Vault/DB process
itself, never to a legitimate monitored host. We also block link-local and
cloud-metadata addresses (169.254.0.0/16, incl. 169.254.169.254 /
fd00:ec2::254), multicast/reserved ranges, and unspecified addresses
(0.0.0.0 / ::), and we restrict outbound HTTP probes to the http/https
schemes.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import List
from urllib.parse import urlsplit

from ._executor import run_blocking

_ALLOWED_SCHEMES = {"http", "https"}


class ProbeTargetError(ValueError):
    """Raised when a probe target is rejected by the SSRF guard."""


class ProbeResolutionError(ProbeTargetError):
    """The probe host could not be resolved (in time). Reported as a normal
    DOWN result, like any other unreachable target."""


#: Upper bound for one name lookup. The system resolver may hang much longer
#: during a DNS outage (5 s × attempts × nameservers); the probe does not wait.
DNS_TIMEOUT_SECONDS = 5.0


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    # Link-local covers the 169.254.0.0/16 IMDS range; also block
    # multicast/reserved, loopback (the core process itself, since this
    # plugin runs in-process), and unspecified (0.0.0.0 / ::).
    # Deliberate trade-off: private ranges stay ALLOWED because monitors
    # target internal RFC1918 infra; core services on the docker network
    # remain reachable to monitoring operators — accepted risk.
    return bool(
        ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_unspecified
    )


def _lookup(host: str) -> list:
    # Looked up through the module attribute so the blocking call is the one
    # that runs in the worker (and tests can replace it).
    return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)


async def resolve_probe_host(host: str, *, timeout: float = DNS_TIMEOUT_SECONDS, enforce: bool = True) -> List[str]:
    """Resolve ``host`` **off the event loop** and return its addresses, IPv4
    first (monitored infrastructure is IPv4-first; a container often has no
    IPv6 route).

    The synchronous ``assert_host_allowed`` called ``socket.getaddrinfo`` on the
    loop: during a DNS outage every hostname probe froze the whole core for the
    resolver timeout (MO-05). Here the lookup runs on the dedicated ``dns``
    pool and the wait is capped at ``timeout``.

    With ``enforce`` every returned address must pass the SSRF guard, and the
    caller connects to one of the returned addresses instead of resolving the
    name a second time — so a DNS answer that changes between check and
    connect (rebinding) cannot redirect the probe (MO-22).

    Raises ``ProbeTargetError`` (blocked) or ``ProbeResolutionError``."""
    if not host:
        raise ProbeTargetError("Empty probe host")
    literal = host.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        ip = None
    if ip is not None:
        if enforce and _is_blocked_ip(ip):
            raise ProbeTargetError(f"Probe target resolves to a blocked address: {ip}")
        return [str(ip)]
    try:
        infos = await run_blocking("dns", _lookup, host, timeout=max(0.5, float(timeout)))
    except asyncio.TimeoutError as exc:
        raise ProbeResolutionError(f"DNS resolution of '{host}' timed out") from exc
    except (socket.gaierror, UnicodeError) as exc:
        raise ProbeResolutionError(f"DNS resolution of '{host}' failed: {exc}") from exc
    addresses: List[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ProbeResolutionError(f"DNS resolution of '{host}' returned no address")
    if enforce:
        for address in addresses:
            if _is_blocked_ip(address):
                raise ProbeTargetError(f"Probe target resolves to a blocked address: {address}")
    addresses.sort(key=lambda a: a.version)  # stable: IPv4 before IPv6
    return [str(a) for a in addresses]


def assert_scheme_allowed(url: str) -> str:
    """Scheme allowlist of an HTTP probe URL; returns the URL's host."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ProbeTargetError(f"Unsupported probe scheme: {parts.scheme!r}")
    if not parts.hostname:
        raise ProbeTargetError("Probe URL has no host")
    return parts.hostname
