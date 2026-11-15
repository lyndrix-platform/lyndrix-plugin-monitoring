import asyncio
import errno
import functools
from typing import Any, Dict

from ...model.models import MonitorState
from ._executor import run_blocking
from ._ssrf import DNS_TIMEOUT_SECONDS, ProbeResolutionError, ProbeTargetError, resolve_probe_host


def _is_permission_error(exc: BaseException) -> bool:
    """icmplib raises ``SocketPermissionError`` when the process may not open an
    ICMP socket (``net.ipv4.ping_group_range`` / missing NET_RAW). That says
    something about THIS instance, nothing about the target."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.EPERM, errno.EACCES):
        return True
    return type(exc).__name__ == "SocketPermissionError"


def _unavailable(detail: str) -> Dict[str, Any]:
    """ICMP cannot be used from this instance: not a DOWN (MO-06). The caller
    falls back to TCP where it can and otherwise reports UNKNOWN."""
    return {
        "state": MonitorState.UNKNOWN,
        "latency_ms": None,
        "error_message": f"ICMP unavailable on this instance: {detail}",
        "unavailable": True,
    }


def _down(message: str) -> Dict[str, Any]:
    return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": message}


async def run_icmp_probe(target: str, timeout_seconds: int) -> Dict[str, Any]:
    """One ICMP echo. Never raises for a probe outcome: ``state`` is UP, DOWN,
    or UNKNOWN with ``unavailable: True`` when this instance cannot ping at all."""
    timeout = max(1, int(timeout_seconds))
    # Same guard as the HTTP probe: loopback/link-local/metadata targets are no
    # legitimate monitoring subject, and the ping goes to the validated address
    # (no second lookup between check and use) (MO-22).
    try:
        target = (await resolve_probe_host(target, timeout=min(DNS_TIMEOUT_SECONDS, float(timeout))))[0]
    except ProbeResolutionError as exc:
        return _down(f"ICMP probe failed: {exc}")  # the target did not resolve: a regular failed check
    except ProbeTargetError as exc:
        return _down(f"ICMP probe refused: {exc}")
    try:
        from icmplib import ping as icmp_ping
    except ModuleNotFoundError:
        return await _ping_subprocess(target, timeout)

    try:
        # Blocking for up to ``timeout`` when the host is down → dedicated pool,
        # never the loop's default executor (MO-07).
        ping_once = functools.partial(icmp_ping, target, count=1, timeout=timeout, privileged=False)
        result = await run_blocking("ping", ping_once, timeout=timeout + 2)
    except asyncio.TimeoutError:
        return _down("ICMP probe timed out")
    except Exception as exc:
        if _is_permission_error(exc):
            return _unavailable(str(exc) or type(exc).__name__)
        # Name lookup failure, unreachable network, bad address: the target
        # did not answer — a regular failed check.
        return _down(f"ICMP probe failed: {exc}")
    if result.is_alive:
        return {
            "state": MonitorState.UP,
            "latency_ms": round(result.avg_rtt or result.min_rtt or 0.0, 2),
            "error_message": None,
        }
    return _down("ICMP probe failed")


async def _ping_subprocess(target: str, timeout: int) -> Dict[str, Any]:
    """Fallback without icmplib: the system ``ping`` binary."""
    try:
        process = await asyncio.create_subprocess_exec(
            # "--" stops option parsing so a target like "-I eth0" can't be
            # interpreted as extra ping flags (argument injection).
            "ping", "-c", "1", "-W", str(timeout), "--", target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return _unavailable("install icmplib or ping")
    except PermissionError as exc:
        return _unavailable(str(exc))
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 2)
    except asyncio.TimeoutError:
        process.kill()
        return _down("ICMP probe timed out")
    if process.returncode == 0:
        return {"state": MonitorState.UP, "latency_ms": None, "error_message": None}
    error = stderr.decode(errors="replace").strip() or "ICMP probe failed"
    if "permission" in error.lower() or "not permitted" in error.lower():
        return _unavailable(error)
    return _down(error)
