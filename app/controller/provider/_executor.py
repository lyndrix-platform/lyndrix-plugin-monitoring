"""Dedicated, bounded thread pools for the blocking parts of a probe.

``asyncio.to_thread`` and ``loop.getaddrinfo`` use the loop's *default*
executor, which the core and every other plugin share for DB sessions, Vault
calls and health checks (``min(32, cpu + 4)`` workers). A site outage with a
few dozen unreachable hosts used to park one worker per ICMP probe for the
whole probe timeout, and everything else in the process queued behind them
(MO-07). Probes get their own pools instead:

* ``ping`` — ICMP echo (icmplib blocks for up to the probe timeout). Sized to
  the service's probe semaphore, so a probe never waits for a worker.
* ``dns``  — name resolution for every probe kind (MO-05). Kept apart from
  ``ping`` so a resolver outage (lookups hang far beyond our timeout and keep
  their worker) cannot delay ICMP probes of IP targets into false DOWNs.

The pools are created lazily and survive a lease handover; ``shutdown`` is
called from the service's ``stop()`` and the next probe recreates them.
"""
from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

POOL_SIZES: Dict[str, int] = {"ping": 24, "dns": 8}

_lock = threading.Lock()
_pools: Dict[str, ThreadPoolExecutor] = {}


def probe_executor(kind: str) -> ThreadPoolExecutor:
    with _lock:
        pool = _pools.get(kind)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=POOL_SIZES[kind], thread_name_prefix=f"monitoring-{kind}"
            )
            _pools[kind] = pool
        return pool


async def run_blocking(
    kind: str, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any
) -> Any:
    """Run ``fn`` on the ``kind`` pool. ``timeout`` bounds the *wait* (raises
    ``asyncio.TimeoutError``); the worker itself finishes whenever the blocking
    call returns — which is why the pools are bounded and separate."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(probe_executor(kind), functools.partial(fn, *args, **kwargs))
    if timeout is None:
        return await future
    return await asyncio.wait_for(future, timeout=timeout)


def shutdown_probe_executors() -> None:
    """Release the pools (plugin unload / lease loss). Queued work is dropped,
    running calls finish on their own; never blocks the caller."""
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)
