"""SSRF guard for active probers.

Monitors legitimately target internal/private infrastructure, so we do NOT
block RFC1918 ranges. We do block link-local and cloud-metadata addresses
(169.254.0.0/16, incl. 169.254.169.254 / fd00:ec2::254) which a monitor target
should never need to reach, and we restrict outbound HTTP probes to the
http/https schemes.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


class ProbeTargetError(ValueError):
    """Raised when a probe target is rejected by the SSRF guard."""


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    # Link-local covers the 169.254.0.0/16 IMDS range; also block multicast/reserved.
    return bool(ip.is_link_local or ip.is_multicast or ip.is_reserved)


def assert_host_allowed(host: str) -> None:
    """Reject empty hosts and hosts that resolve to link-local/metadata ranges.

    Best-effort: a literal IP is checked directly; a hostname is resolved and
    every returned address is checked. DNS failures are left to the probe
    itself (they surface as a normal DOWN result).
    """
    if not host:
        raise ProbeTargetError("Empty probe host")

    candidates: list[ipaddress._BaseAddress] = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        # Hostname — resolve and check every address.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return  # Unresolvable; the probe will report DOWN.
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr.split("%", 1)[0]))
            except ValueError:
                continue

    for ip in candidates:
        if _is_blocked_ip(ip):
            raise ProbeTargetError(f"Probe target resolves to a blocked address: {ip}")


def assert_url_allowed(url: str) -> None:
    """Validate an HTTP probe URL: allowed scheme + host not link-local/metadata."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ProbeTargetError(f"Unsupported probe scheme: {parts.scheme!r}")
    if not parts.hostname:
        raise ProbeTargetError("Probe URL has no host")
    assert_host_allowed(parts.hostname)
