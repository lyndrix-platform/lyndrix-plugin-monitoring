import httpx
from typing import Any, Dict, Optional
from urllib.parse import urljoin

from ...model.models import MonitorState
from ._ssrf import ProbeTargetError, assert_url_allowed

# Redirect hops we will manually follow per probe (mirrors a sane
# httpx-style cap without relying on the client's own redirect handling).
_MAX_REDIRECT_HOPS = 5


def is_http_target(value: Optional[str]) -> bool:
    if not value:
        return False
    return str(value).startswith(("http://", "https://"))


async def run_http_probe(client: httpx.AsyncClient, target: str, timeout_seconds: int) -> Dict[str, Any]:
    """Probe an HTTP(S) target.

    The shared client never follows redirects on its own
    (``follow_redirects=False`` in service.py) — every hop of the redirect
    chain is walked here (up to ``_MAX_REDIRECT_HOPS``) and its absolute
    target is re-validated against the SSRF guard *before* it is requested.
    Without this, a monitored host answering e.g. ``302 -> http://169.254.169.254/...``
    (or ``127.0.0.1``) would be followed straight past a guard that only ever
    checked the original URL.
    """
    url = target
    # SSRF guard: reject link-local/metadata targets before issuing the request.
    try:
        assert_url_allowed(url)
    except ProbeTargetError as exc:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": str(exc)}

    response = await client.get(url, timeout=httpx.Timeout(timeout_seconds))
    hops = 0
    while response.has_redirect_location and hops < _MAX_REDIRECT_HOPS:
        next_url = urljoin(str(response.url), response.headers["location"])
        try:
            assert_url_allowed(next_url)
        except ProbeTargetError as exc:
            # A disallowed hop is treated as a check failure — same error
            # path as a blocked original URL.
            return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": str(exc)}
        url = next_url
        response = await client.get(url, timeout=httpx.Timeout(timeout_seconds))
        hops += 1

    if response.has_redirect_location:
        # Still redirecting after exhausting the hop budget.
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": "Too many redirects"}

    # Final response: either a non-redirect status, or a 3xx without a
    # (usable) Location header — both are evaluated the same way as before.
    latency_ms = round(response.elapsed.total_seconds() * 1000.0, 2)
    if response.status_code < 400:
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    return {
        "state": MonitorState.DOWN,
        "latency_ms": latency_ms,
        "error_message": f"HTTP {response.status_code}",
    }
