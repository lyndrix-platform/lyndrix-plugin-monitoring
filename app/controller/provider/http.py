import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import urljoin

import httpx

from ...model.models import MonitorState
from ._ssrf import DNS_TIMEOUT_SECONDS, ProbeTargetError, assert_scheme_allowed, resolve_probe_host

# Redirect hops we will manually follow per probe (mirrors a sane
# httpx-style cap without relying on the client's own redirect handling).
_MAX_REDIRECT_HOPS = 5
#: Slack on top of the monitor's timeout for the WHOLE probe (name lookup,
#: connect, TLS, every redirect hop). ``httpx.Timeout`` only bounds each single
#: socket operation — a target that keeps dripping bytes never trips it.
PROBE_DEADLINE_GRACE_SECONDS = 2.0


def is_http_target(value: Optional[str]) -> bool:
    if not value:
        return False
    return str(value).startswith(("http://", "https://"))


def _down(message: str, latency_ms: Optional[float] = None) -> Dict[str, Any]:
    return {"state": MonitorState.DOWN, "latency_ms": latency_ms, "error_message": message}


async def pin_request(url: str, timeout_seconds: float) -> Tuple[httpx.URL, Dict[str, str], Dict[str, Any]]:
    """Validate ``url`` against the SSRF guard and return ``(request_url,
    headers, extensions)`` that make httpx connect to the *validated* address.

    The name is resolved once (off the loop); the request then goes to that IP
    with the original ``Host`` header and, for https, the original name as SNI
    and certificate hostname — httpx does not resolve the name again, so the
    answer cannot change between check and connect."""
    host = assert_scheme_allowed(url)
    addresses = await resolve_probe_host(host, timeout=min(DNS_TIMEOUT_SECONDS, max(1.0, timeout_seconds)))
    parsed = httpx.URL(url)
    # Every probe uses its own connection: the body is never drained, and a
    # pooled TLS connection is keyed by IP — it must not be reused for another
    # virtual host on the same address.
    headers = {"Connection": "close"}
    extensions: Dict[str, Any] = {}
    if parsed.host.strip("[]") == addresses[0]:
        return parsed, headers, extensions  # literal IP target: nothing to pin
    headers["Host"] = parsed.netloc.decode("ascii")
    if parsed.scheme == "https":
        extensions["sni_hostname"] = parsed.host
    return parsed.copy_with(host=addresses[0]), headers, extensions


@asynccontextmanager
async def pinned_stream(
    client: httpx.AsyncClient, url: str, timeout_seconds: float, *, params: Optional[Dict[str, Any]] = None
) -> AsyncIterator[httpx.Response]:
    """``client.stream("GET", …)`` to the validated address of ``url``. The
    response body is NOT read; leaving the block closes the connection."""
    request_url, headers, extensions = await pin_request(url, timeout_seconds)
    async with client.stream(
        "GET", request_url, params=params, headers=headers, extensions=extensions,
        timeout=httpx.Timeout(timeout_seconds),
    ) as response:
        yield response


async def read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` of the body; a larger body is an error (the
    probe must never buffer an arbitrary download in RAM)."""
    chunks = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ProbeTargetError(f"Response body larger than {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def run_http_probe(client: httpx.AsyncClient, target: str, timeout_seconds: int) -> Dict[str, Any]:
    """Probe an HTTP(S) target: UP when the final status is < 400.

    * The whole probe has one deadline (``timeout_seconds`` + a small grace).
      Only status line and headers are read — the body is never downloaded, so
      an SSE/MJPEG stream, a long-poll endpoint or a huge file answers like any
      other URL instead of hanging the monitor and holding a probe slot.
    * The shared client never follows redirects on its own
      (``follow_redirects=False`` in service.py) — every hop of the redirect
      chain is walked here (up to ``_MAX_REDIRECT_HOPS``) and its absolute
      target is re-validated against the SSRF guard *before* it is requested.
      Without this, a monitored host answering e.g. ``302 -> http://169.254.169.254/...``
      (or ``127.0.0.1``) would be followed straight past a guard that only ever
      checked the original URL.
    * Names are resolved off the event loop and the request is pinned to the
      validated address (see ``pin_request``).
    """
    timeout = max(1.0, float(timeout_seconds))
    try:
        return await asyncio.wait_for(
            _walk_redirects(client, target, timeout), timeout=timeout + PROBE_DEADLINE_GRACE_SECONDS
        )
    except asyncio.TimeoutError:
        return _down(f"HTTP probe deadline exceeded ({timeout:g} s)")
    except ProbeTargetError as exc:
        # Blocked by the SSRF guard or unresolvable — a check failure.
        return _down(str(exc))


async def _walk_redirects(client: httpx.AsyncClient, target: str, timeout: float) -> Dict[str, Any]:
    url = target
    hops = 0
    while True:
        started = time.perf_counter()
        async with pinned_stream(client, url, timeout) as response:
            status = response.status_code
            location = response.headers.get("location") if response.has_redirect_location else None
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if location is None:
            break
        if hops >= _MAX_REDIRECT_HOPS:
            # Still redirecting after exhausting the hop budget.
            return _down("Too many redirects")
        # Relative to the logical URL (the name), not to the pinned IP URL.
        url = urljoin(url, location)
        hops += 1

    # Final response: either a non-redirect status, or a 3xx without a
    # (usable) Location header — both are evaluated the same way as before.
    if status < 400:
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    return _down(f"HTTP {status}", latency_ms)
