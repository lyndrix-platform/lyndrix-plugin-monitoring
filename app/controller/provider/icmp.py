import asyncio
from typing import Any, Dict

from ...model.models import MonitorState


async def run_icmp_probe(target: str, timeout_seconds: int) -> Dict[str, Any]:
    try:
        from icmplib import ping as icmp_ping

        result = await asyncio.to_thread(
            icmp_ping,
            target,
            count=1,
            timeout=max(1, timeout_seconds),
            privileged=False,
        )
        if result.is_alive:
            return {
                "state": MonitorState.UP,
                "latency_ms": round(result.avg_rtt or result.min_rtt or 0.0, 2),
                "error_message": None,
            }
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": "ICMP probe failed"}
    except ModuleNotFoundError:
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, timeout_seconds)), target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0:
            return {"state": MonitorState.UP, "latency_ms": None, "error_message": None}
        error = stderr.decode().strip() or "ICMP probe failed"
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": error}
    except FileNotFoundError:
        return {
            "state": MonitorState.DOWN,
            "latency_ms": None,
            "error_message": "ICMP probing unavailable: install icmplib or ping",
        }
