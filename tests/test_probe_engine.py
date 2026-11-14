"""Probe engine hardening — MO-04 (overall deadline, body never downloaded),
MO-05 (DNS off the event loop, request pinned to the validated address),
MO-06 (ICMP not permitted ≠ DOWN) and MO-07 (dedicated thread pools).

No real network: local sockets on loopback (with the loopback rule of the SSRF
guard switched off for that test) or ``httpx.MockTransport``."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import socket
import sys
import threading
import time
import types

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture


@pytest.fixture()
def allow_loopback(monkeypatch):
    from app.controller.provider import _ssrf

    monkeypatch.setattr(_ssrf, "_is_blocked_ip", lambda ip: False)


def _fake_dns(monkeypatch, answers):
    """``answers``: host → address, or host → list of addresses handed out one
    per lookup (the last one repeats). Returns the list of looked-up hosts."""
    from app.controller.provider import _ssrf

    calls = []

    def fake(host, *a, **k):
        calls.append(host)
        answer = answers[host]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, 0))]

    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", fake)
    return calls


def _serve(handler):
    """One-connection TCP server on loopback; ``handler(conn, stop)`` in a thread."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            handler(conn, stop)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], stop, srv


# ── MO-04: overall deadline, no body download ────────────────────────────────


def test_streaming_target_answers_like_any_other_url(allow_loopback):
    """An SSE/MJPEG-style endless body used to hang the probe forever."""
    from app.controller.provider.http import run_http_probe
    from app.model.models import MonitorState

    def endless(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        while not stop.is_set():
            conn.sendall(b"data: x\n\n")
            time.sleep(0.2)

    port, stop, srv = _serve(endless)

    async def main():
        async with httpx.AsyncClient(follow_redirects=False) as client:
            started = time.perf_counter()
            result = await asyncio.wait_for(run_http_probe(client, f"http://127.0.0.1:{port}/events", 2), 8)
            return result, time.perf_counter() - started

    try:
        result, took = asyncio.run(main())
    finally:
        stop.set()
        srv.close()
    assert result["state"] == MonitorState.UP and result["error_message"] is None
    assert took < 2.0 and result["latency_ms"] is not None


def test_dripping_backend_hits_the_overall_deadline(allow_loopback):
    """A half-dead backend that keeps sending a byte now and then never trips
    httpx' per-operation timeout — the probe deadline ends it."""
    from app.controller.provider.http import PROBE_DEADLINE_GRACE_SECONDS, run_http_probe
    from app.model.models import MonitorState

    def drip(conn, stop):
        for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"y" * 200:
            if stop.is_set():
                return
            conn.sendall(bytes([byte]))
            time.sleep(0.4)

    port, stop, srv = _serve(drip)

    async def main():
        async with httpx.AsyncClient(follow_redirects=False) as client:
            started = time.perf_counter()
            result = await asyncio.wait_for(run_http_probe(client, f"http://127.0.0.1:{port}/", 1), 10)
            return result, time.perf_counter() - started

    try:
        result, took = asyncio.run(main())
    finally:
        stop.set()
        srv.close()
    assert result["state"] == MonitorState.DOWN and "deadline exceeded" in result["error_message"]
    assert took < 1 + PROBE_DEADLINE_GRACE_SECONDS + 1.0


def test_scheduled_probe_has_a_hard_deadline_and_frees_its_slot(svc, monkeypatch):
    mid = _base._monitor(svc)
    for state in ("DOWN", "DOWN"):  # already DOWN → no flap dampening in the way
        _base._beat(svc, mid, state)

    async def hang(_record):
        await asyncio.sleep(3600)

    monkeypatch.setattr(svc, "_dispatch_probe", hang)
    monkeypatch.setattr(svc, "_probe_deadline", lambda _t: 0.2)

    async def main():
        await asyncio.wait_for(svc.run_scheduled_probe(mid), 5)
        return svc._probe_semaphore._value

    assert asyncio.run(main()) == 24 and not svc._inflight_probes
    item = svc.get_monitor(mid)
    assert item["latest_state"] == "DOWN" and item["latest_error"] == "Probe deadline exceeded"
    from app.controller.service import MonitoringService

    assert MonitoringService._probe_deadline(10) == 25.0 and MonitoringService._probe_deadline("x") == 25.0


def test_docker_probe_caps_the_body_and_survives_garbage(monkeypatch):
    from app.controller.provider import docker as docker_mod
    from app.model.models import MonitorState

    monkeypatch.setattr(docker_mod, "MAX_DOCKER_BODY_BYTES", 1024)
    bodies = {
        "big": b"[" + b" " * 5000 + b"]",
        "html": b"<html>not docker</html>",
        "ok": b'[{"Names": ["/web"], "State": "running", "Status": "Up 3 hours"}, "junk"]',
    }

    async def probe(which):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=bodies[which]))
        async with httpx.AsyncClient(transport=transport) as client:
            return await docker_mod.run_docker_service_probe(client, "10.0.0.5", "web", 2)

    big = asyncio.run(probe("big"))
    assert big["state"] == MonitorState.DOWN and "larger than 1024 bytes" in big["error_message"]
    assert asyncio.run(probe("html"))["state"] == MonitorState.UNKNOWN
    assert asyncio.run(probe("ok"))["state"] == MonitorState.UP


# ── MO-05: DNS off the loop, resolve once, connect to the validated address ───


def test_dns_outage_does_not_stall_the_event_loop(monkeypatch):
    from app.controller.provider import _ssrf
    from app.controller.provider.http import run_http_probe
    from app.model.models import MonitorState

    def slow_gai(*a, **k):
        time.sleep(1.0)  # resolver hang during a DNS outage (shortened)
        raise socket.gaierror("temporary failure in name resolution")

    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", slow_gai)

    async def main():
        lag = []

        async def heartbeat():
            while True:
                t = time.perf_counter()
                await asyncio.sleep(0.05)
                lag.append(time.perf_counter() - t - 0.05)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.1)
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*(run_http_probe(client, f"http://svc{i}.lan/", 5) for i in range(5)))
        hb.cancel()
        return max(lag), results

    worst, results = asyncio.run(main())
    assert worst < 0.3, f"event loop stalled for {worst:.2f} s"
    assert all(r["state"] == MonitorState.DOWN and "DNS resolution" in r["error_message"] for r in results)


def test_dns_lookup_wait_is_capped(monkeypatch):
    from app.controller.provider import _ssrf

    def hang(*a, **k):
        time.sleep(1.5)
        return []

    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", hang)

    async def main():
        started = time.perf_counter()
        with pytest.raises(_ssrf.ProbeResolutionError, match="timed out"):
            await _ssrf.resolve_probe_host("slow.lan", timeout=0.5)
        return time.perf_counter() - started

    assert asyncio.run(main()) < 1.2


def test_request_is_pinned_to_the_validated_address(monkeypatch):
    """Resolve once, connect to that address, keep Host header and SNI — a DNS
    answer that flips to loopback afterwards (rebinding) is never used."""
    from app.controller.provider.http import run_http_probe
    from app.model.models import MonitorState

    lookups = _fake_dns(monkeypatch, {"svc.lan": ["10.1.2.3", "127.0.0.1"]})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname"),
                     request.headers.get("connection")))
        return httpx.Response(204)

    async def main(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_http_probe(client, url, 5)

    assert asyncio.run(main("https://svc.lan:8443/health?x=1"))["state"] == MonitorState.UP
    assert seen == [("https://10.1.2.3:8443/health?x=1", "svc.lan:8443", "svc.lan", "close")]
    assert lookups == ["svc.lan"]  # exactly one lookup; httpx got an IP

    seen.clear()
    assert asyncio.run(main("http://10.9.9.9/"))["state"] == MonitorState.UP  # literal IP: nothing to pin
    assert seen == [("http://10.9.9.9/", "10.9.9.9", None, "close")]


def test_redirect_hops_are_revalidated_and_resolved_per_hop(monkeypatch):
    from app.controller.provider.http import run_http_probe
    from app.model.models import MonitorState

    lookups = _fake_dns(monkeypatch, {"a.lan": "10.0.0.1", "b.lan": "10.0.0.2", "evil.lan": "127.0.0.1"})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"], request.url.path))
        if request.headers["host"] == "a.lan" and request.url.path == "/":
            return httpx.Response(302, headers={"location": "/login"})  # relative → same logical host
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": "http://b.lan/next"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"location": "http://evil.lan/"})
        return httpx.Response(200)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_http_probe(client, "http://a.lan/", 5)

    result = asyncio.run(main())
    assert result["state"] == MonitorState.DOWN and "blocked address: 127.0.0.1" in result["error_message"]
    assert seen == [("10.0.0.1", "a.lan", "/"), ("10.0.0.1", "a.lan", "/login"), ("10.0.0.2", "b.lan", "/next")]
    assert lookups == ["a.lan", "a.lan", "b.lan", "evil.lan"]


def test_blocked_and_unsupported_targets_are_rejected_before_any_request(monkeypatch):
    from app.controller.provider.http import run_http_probe
    from app.model.models import MonitorState

    _fake_dns(monkeypatch, {"meta.lan": "169.254.169.254"})

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("request issued for a blocked target")

    async def main(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_http_probe(client, url, 5)

    for url in ("http://127.0.0.1:8200/", "http://meta.lan/latest", "http://[::1]/", "ftp://10.0.0.1/x"):
        result = asyncio.run(main(url))
        assert result["state"] == MonitorState.DOWN and result["error_message"], url


# ── MO-07: blocking probe work on dedicated pools ─────────────────────────────


def test_icmp_probes_do_not_occupy_the_default_executor(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.controller.provider import _executor, icmp as icmp_mod

    def fake_ping(target, count, timeout, privileged):
        time.sleep(1.0)  # host down: icmplib waits for the timeout
        return types.SimpleNamespace(is_alive=False)

    monkeypatch.setitem(sys.modules, "icmplib", types.SimpleNamespace(ping=fake_ping))
    _executor.shutdown_probe_executors()

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
        probes = [asyncio.create_task(icmp_mod.run_icmp_probe(f"10.9.0.{i}", 1)) for i in range(24)]
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        await asyncio.to_thread(lambda: None)  # the core's next DB call / health query
        waited = time.perf_counter() - started
        results = await asyncio.gather(*probes)
        return waited, results, time.perf_counter() - started

    waited, results, total = asyncio.run(main())
    assert waited < 0.3, f"unrelated to_thread waited {waited:.2f} s"
    assert total < 2.5 and len(results) == 24  # 24 pings ran in parallel on their own pool
    assert _executor.probe_executor("ping")._max_workers == 24
    assert _executor.probe_executor("ping")._thread_name_prefix == "monitoring-ping"
    _executor.shutdown_probe_executors()
    assert _executor._pools == {}


def test_tcp_probe_resolves_on_the_probe_dns_pool(monkeypatch):
    from app.controller.provider import tcp as tcp_mod
    from app.model.models import MonitorState

    lookups = _fake_dns(monkeypatch, {"db.lan": "127.0.0.1"})
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    async def main():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", None)  # the loop resolver must not be needed
        up = await tcp_mod.run_tcp_probe("db.lan", port, 2)
        srv.close()
        refused = await tcp_mod.run_tcp_probe("db.lan", port, 2)
        return up, refused

    up, refused = asyncio.run(main())
    assert up["state"] == MonitorState.UP and lookups == ["db.lan", "db.lan"]
    assert refused["state"] == MonitorState.DOWN and refused.get("refused") is True


# ── MO-06: ICMP not permitted on this instance ────────────────────────────────


class SocketPermissionError(Exception):
    """Same name as icmplib.exceptions.SocketPermissionError."""


def _no_icmp(monkeypatch):
    def fake_ping(*a, **k):
        raise SocketPermissionError("Root privileges are required to create the socket")

    monkeypatch.setitem(sys.modules, "icmplib", types.SimpleNamespace(ping=fake_ping))


def _record(monitor_type="server", target="10.0.0.7"):
    return {"target": target, "monitor_type": monitor_type, "timeout_seconds": 2, "address": None,
            "service_name": None, "metadata": {}}


def test_real_icmplib_permission_error_is_recognised():
    from icmplib.exceptions import NameLookupError
    from icmplib.exceptions import SocketPermissionError as RealPermissionError

    from app.controller.provider.icmp import _is_permission_error

    assert _is_permission_error(RealPermissionError(False))
    assert _is_permission_error(PermissionError("x")) and _is_permission_error(OSError(1, "EPERM"))
    assert not _is_permission_error(NameLookupError("nope.lan")) and not _is_permission_error(OSError(101, "unreach"))


def test_server_monitor_falls_back_to_tcp_when_icmp_is_not_permitted(svc, monkeypatch):
    import app.controller.service as service_mod
    from app.model.models import MonitorState

    _no_icmp(monkeypatch)
    tcp_calls = []

    async def fake_tcp(host, port, timeout):
        tcp_calls.append((host, port))
        up = port == 443
        return {"state": MonitorState.UP if up else MonitorState.DOWN, "latency_ms": 1.5 if up else None,
                "error_message": None if up else "TCP: timed out"}

    monkeypatch.setattr(service_mod, "run_tcp_probe", fake_tcp)
    svc._probing = True
    state, latency, error = asyncio.run(svc._dispatch_probe(_record()))
    assert (state, latency, error) == (MonitorState.UP, 1.5, None)
    assert sorted(tcp_calls) == [("10.0.0.7", 22), ("10.0.0.7", 80), ("10.0.0.7", 443)]
    assert svc.icmp_unavailable is True


def test_no_icmp_and_no_open_port_is_unknown_never_down(svc, monkeypatch):
    import app.controller.service as service_mod
    from app.model.models import MonitorState

    _no_icmp(monkeypatch)
    answer = {"refused": False}

    async def fake_tcp(host, port, timeout):
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": "TCP: nope", **answer}

    monkeypatch.setattr(service_mod, "run_tcp_probe", fake_tcp)
    state, _lat, error = asyncio.run(svc._dispatch_probe(_record()))
    assert state == MonitorState.UNKNOWN and "ICMP unavailable" in error and "TCP fallback" in error

    # a refused connection proves the host is alive …
    answer["refused"] = True
    assert asyncio.run(svc._dispatch_probe(_record()))[0] == MonitorState.UP
    # … but not when the operator asked for one specific port
    assert asyncio.run(svc._dispatch_probe(_record(target="10.0.0.7:8200")))[0] == MonitorState.UNKNOWN
    # ping monitors have no fallback: UNKNOWN
    assert asyncio.run(svc._dispatch_probe(_record("ping")))[0] == MonitorState.UNKNOWN


def test_ping_monitor_without_icmp_raises_no_outage(svc, monkeypatch):
    """The whole path: scheduled probe → heartbeat UNKNOWN → no outage clock."""
    _no_icmp(monkeypatch)
    mid = _base._monitor(svc, "host:edge", ci_id="11111111-1111-4111-8111-111111111111")
    svc._probing = True
    for _ in range(4):  # well past the flap threshold
        asyncio.run(svc.run_scheduled_probe(mid))
    item = svc.get_monitor(mid)
    assert item["latest_state"] == "UNKNOWN" and "ICMP unavailable" in item["latest_error"]
    assert svc.maintenance.list_outages() == []
    assert not svc._consec_failures and not svc._host_circuit_until


def test_icmp_errors_that_are_about_the_target_stay_down(monkeypatch):
    from app.controller.provider.icmp import run_icmp_probe
    from app.model.models import MonitorState

    class NameLookupError(Exception):
        pass

    def fake_ping(*a, **k):
        raise NameLookupError("The name 'gone.lan' cannot be resolved")

    monkeypatch.setitem(sys.modules, "icmplib", types.SimpleNamespace(ping=fake_ping))
    result = asyncio.run(run_icmp_probe("gone.lan", 1))
    assert result["state"] == MonitorState.DOWN and "unavailable" not in result


def test_health_is_degraded_while_icmp_is_unavailable(svc, monkeypatch):
    import monitoring_plugin.entrypoint as ep

    _base._monitor(svc)
    monkeypatch.setitem(ep.plugin_state, "service", svc)

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.05)
        before = await ep.health(svc.ctx)
        svc._note_icmp_result({"unavailable": True, "error_message": "ICMP unavailable on this instance: x"})
        degraded = await ep.health(svc.ctx)
        svc._note_icmp_result({"state": "UP"})
        after = await ep.health(svc.ctx)
        await svc.stop_probing()
        return before, degraded, after

    before, degraded, after = asyncio.run(run())
    assert before.status == "ok" and after.status == "ok"
    assert degraded.status == "degraded" and degraded.details["reason"] == "icmp_unavailable"
