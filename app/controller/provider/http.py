import httpx
from typing import Any, Dict, Optional

from ...model.models import MonitorState
from ._ssrf import ProbeTargetError, assert_url_allowed


def is_http_target(value: Optional[str]) -> bool:
    if not value:
        return False
    return str(value).startswith(("http://", "https://"))


async def run_http_probe(client: httpx.AsyncClient, target: str, timeout_seconds: int) -> Dict[str, Any]:
    # SSRF guard: reject link-local/metadata targets before issuing the request.
    try:
        assert_url_allowed(target)
    except ProbeTargetError as exc:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": str(exc)}
    response = await client.get(target, timeout=httpx.Timeout(timeout_seconds))
    latency_ms = round(response.elapsed.total_seconds() * 1000.0, 2)
    if response.status_code < 400:
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    return {
        "state": MonitorState.DOWN,
        "latency_ms": latency_ms,
        "error_message": f"HTTP {response.status_code}",
    }
