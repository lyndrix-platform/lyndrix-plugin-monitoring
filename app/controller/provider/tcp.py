import asyncio
import ipaddress
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional, Tuple

from ...model.models import MonitorState
from ._ssrf import DNS_TIMEOUT_SECONDS, resolve_probe_host


def looks_like_network_target(value: Optional[str]) -> bool:
    from .http import is_http_target
    if not value or is_http_target(value):
        return False
    raw = str(value).strip()
    if not raw or "/" in raw:
        return False
    host = raw
    if ":" in raw and raw.count(":") == 1:
        host = raw.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return all(c.isalnum() or c in {"-", "."} for c in host)


def split_host_port(target: str) -> Tuple[str, Optional[int]]:
    """``"host:8200"`` → ``("host", 8200)``; anything else (bare host, IPv6
    literal) → ``(target, None)``."""
    raw = str(target).strip()
    if raw.count(":") == 1:
        host_part, port_part = raw.rsplit(":", 1)
        if host_part and port_part.isdigit():
            return host_part, int(port_part)
    return raw, None


async def run_tcp_probe(host: str, port: int, timeout_seconds: int) -> Dict[str, Any]:
    """TCP connect. ``refused: True`` marks an actively refused connection —
    the port is closed, but the host answered."""
    timeout = max(1, timeout_seconds)
    started = time.perf_counter()
    try:
        # Same SSRF guard as the HTTP probe — a monitor on 127.0.0.1:8200 would be
        # a port oracle for the core's own loopback services (MO-22).
        # Resolved on the probe DNS pool: ``open_connection`` with a name would
        # resolve on the loop's shared default executor (MO-07).
        addresses = await resolve_probe_host(host, timeout=min(DNS_TIMEOUT_SECONDS, timeout))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(addresses[0], port), timeout=timeout
        )
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    except asyncio.TimeoutError:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": f"TCP {port}: timed out"}
    except ConnectionRefusedError as exc:
        return {
            "state": MonitorState.DOWN, "latency_ms": None,
            "error_message": f"TCP {port}: {exc}", "refused": True,
        }
    except Exception as exc:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": f"TCP {port}: {exc}"}


def tcp_fallback_ports(metadata: Dict[str, Any], target: str) -> List[int]:
    from .docker import is_docker_host
    _host, explicit_port = split_host_port(target)
    if explicit_port is not None:
        return [explicit_port]
    ports: List[int] = []
    if is_docker_host(metadata):
        ports.append(2375)
    ports.extend([22, 443, 80])
    unique: List[int] = []
    for port in ports:
        if port not in unique:
            unique.append(port)
    return unique
