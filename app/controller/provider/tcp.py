import asyncio
import ipaddress
from contextlib import suppress
from typing import Any, Dict, List, Optional

from ...model.models import MonitorState, _utc_now


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


async def run_tcp_probe(host: str, port: int, timeout_seconds: int) -> Dict[str, Any]:
    started = _utc_now()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=max(1, timeout_seconds)
        )
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        latency_ms = round((_utc_now() - started).total_seconds() * 1000.0, 2)
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    except Exception as exc:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": f"TCP {port}: {exc}"}


def tcp_fallback_ports(metadata: Dict[str, Any], target: str) -> List[int]:
    from .docker import is_docker_host
    if ":" in str(target) and str(target).count(":") == 1:
        host_part, port_part = str(target).rsplit(":", 1)
        if port_part.isdigit() and host_part:
            return [int(port_part)]
    ports: List[int] = []
    if is_docker_host(metadata):
        ports.append(2375)
    ports.extend([22, 443, 80])
    unique: List[int] = []
    for port in ports:
        if port not in unique:
            unique.append(port)
    return unique
