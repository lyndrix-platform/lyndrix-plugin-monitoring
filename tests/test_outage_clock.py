"""Outage clock vs. maintenance — MO-02 (``suppressed_until`` follows the
CURRENT maintenance state and can shrink), MO-03 (circuit breaker never causes
a false outage; DOWN is confirmed by a fresh probe before it is reported) and
MO-12 (one ``effective_state`` evaluation per CI, no invented far-future end).
Fake clock, real service against in-memory SQLite."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "_outage_base", pathlib.Path(__file__).with_name("test_outage_rule.py")
)
_o = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_o)
svc = _o.svc  # pytest fixtures
clock = _o.clock
NOW, MIN, HOST_CI, SVC_CI = _o.NOW, _o.MIN, _o.HOST_CI, _o.SVC_CI
_host, _service, _down, _up = _o._host, _o._service, _o._down, _o._up
_publish, _item, _once, _persisted = _o._publish, _o._item, _o._once, _o._persisted


def _row(svc, mid):
    from app.model.models import OutageRecord

    with svc._session() as s:
        row = s.get(OutageRecord, mid)
        return None if row is None else SimpleNamespace(state=row.state, suppressed_until=row.suppressed_until)


def _at(minutes):
    return (NOW + minutes * MIN).replace(tzinfo=None)


def _override(svc, until_min, override_id="dep-1"):
    from core.api import maintenance as mw

    svc.maintenance.on_override_started({
        "override_id": override_id, "ci_ids": [HOST_CI], "source": "iac-orchestrator",
        "until": mw.iso(NOW + until_min * MIN), "started_at": mw.iso(NOW), "linger_minutes": 15,
    })


def _end_override(svc, at_min, override_id="dep-1"):
    from core.api import maintenance as mw

    svc.maintenance.on_override_ended({
        "override_id": override_id, "source": "iac-orchestrator",
        "ended_at": mw.iso(NOW + at_min * MIN), "linger_minutes": 15,
    })


# ── MO-02 ────────────────────────────────────────────────────────────────────


def test_override_ended_early_reports_after_end_plus_linger_plus_offline(svc, clock):
    """Deploy override until +4 h, ended after 5 min (linger 15), host stays down:
    due at +30 min — it used to be +250 min."""
    mid = _host(svc, "host:a", ci_id=HOST_CI)
    mnt = svc.maintenance
    _override(svc, 240)
    _down(svc, mid)
    assert svc.get_monitor(mid)["latest_state"] == "MAINTENANCE"
    assert _row(svc, mid).suppressed_until == _at(240)

    clock["now"] = NOW + 5 * MIN
    _end_override(svc, 5)
    assert _row(svc, mid).suppressed_until == _at(20)  # shrunk to the real end at once

    clock["now"] = NOW + 21 * MIN
    _down(svc, mid)
    assert svc.get_monitor(mid)["latest_state"] == "DOWN"
    assert mnt.evaluate_outages(now=NOW + 29 * MIN)["persisted"] == 0
    assert mnt.evaluate_outages(now=NOW + 30 * MIN)["persisted"] == 1
    p = _persisted(svc)[0]
    assert p["persisted_at"] == "2026-10-20T12:30:00Z" and p["since"] == "2026-10-20T12:00:00Z"


def test_stale_bound_is_dropped_even_when_nobody_refreshed_it(svc, clock):
    """Safety net: the stored end is still ahead but the monitor is DOWN outside
    maintenance → the clock counts from that moment (heartbeat and evaluator)."""
    from app.model.models import OutageRecord

    mid = _host(svc, "host:b", ci_id=HOST_CI)
    _down(svc, mid)
    with svc._session() as s:  # as if an old version had stored a far end
        s.get(OutageRecord, mid).suppressed_until = _at(600)
        s.commit()
    clock["now"] = NOW + 3 * MIN
    _down(svc, mid)
    assert _row(svc, mid).suppressed_until == _at(3)

    with svc._session() as s:
        s.get(OutageRecord, mid).suppressed_until = _at(600)
        s.commit()
    assert svc.maintenance.evaluate_outages(now=NOW + 4 * MIN)["persisted"] == 0
    assert _row(svc, mid).suppressed_until == _at(4)
    assert svc.maintenance.evaluate_outages(now=NOW + 14 * MIN)["persisted"] == 1


def test_manual_window_deleted_while_down(svc, clock):
    from app.model.models import WindowCreate
    from core.api import maintenance as mw

    mid = _host(svc, "host:c", ci_id=HOST_CI)
    mnt = svc.maintenance
    window = mnt.create_manual_window(WindowCreate(
        ci_id=HOST_CI, windows=[{"type": "once", "start": mw.iso(NOW - 10 * MIN), "end": mw.iso(NOW + 300 * MIN)}],
    ))
    _down(svc, mid)
    assert _row(svc, mid).suppressed_until == _at(300)
    clock["now"] = NOW + 7 * MIN
    assert mnt.delete_manual_window(window["id"]) == "deleted"
    assert _row(svc, mid).suppressed_until == _at(7)
    assert mnt.evaluate_outages(now=NOW + 16 * MIN)["persisted"] == 0
    assert mnt.evaluate_outages(now=NOW + 17 * MIN)["persisted"] == 1


def test_published_window_replaced_by_a_shorter_one(svc, clock):
    mid = _host(svc, "host:d", ci_id=HOST_CI)
    _publish(svc, _item(HOST_CI, windows=[_once(NOW - 5 * MIN, NOW + 120 * MIN)]))
    _down(svc, mid)
    assert _row(svc, mid).suppressed_until == _at(120)
    clock["now"] = NOW + 2 * MIN
    _publish(svc, _item(HOST_CI, windows=[_once(NOW - 5 * MIN, NOW + 20 * MIN)]))
    assert _row(svc, mid).suppressed_until == _at(20)
    assert svc.maintenance.evaluate_outages(now=NOW + 29 * MIN)["persisted"] == 0
    assert svc.maintenance.evaluate_outages(now=NOW + 30 * MIN)["persisted"] == 1


def test_invented_far_future_end_is_never_stored(svc, clock):
    """A window without a gap: core ≤ 1.11.1 reports until = now + 30 d. The
    clock is re-marked "now" on each evaluation instead; once the window is
    gone the outage is due after offline_minutes, not after 30 days."""
    mid = _host(svc, "host:e", ci_id=HOST_CI)
    always_on = {"type": "cron", "expr": "*/15 * * * *", "duration_minutes": 30, "tz": "UTC"}
    _publish(svc, _item(HOST_CI, windows=[always_on]))
    _down(svc, mid)
    assert svc.get_monitor(mid)["latest_state"] == "MAINTENANCE"
    assert _row(svc, mid).suppressed_until == _at(0)
    assert svc.maintenance.evaluate_outages(now=NOW + 9 * MIN)["suppressed"] == 1
    assert _row(svc, mid).suppressed_until == _at(9)

    clock["now"] = NOW + 12 * MIN
    svc.maintenance.evaluate_outages(now=NOW + 12 * MIN)  # the evaluator ticks every minute
    assert _row(svc, mid).suppressed_until == _at(12)
    _publish(svc, _item(HOST_CI, windows=[]))  # the window is withdrawn
    _down(svc, mid)
    assert svc.maintenance.evaluate_outages(now=NOW + 21 * MIN)["persisted"] == 0
    assert svc.maintenance.evaluate_outages(now=NOW + 22 * MIN)["persisted"] == 1


def test_open_ended_flag_of_newer_cores_is_honoured():
    from app.controller.maintenance import MaintenanceManager

    near = NOW + 60 * MIN
    plain = SimpleNamespace(active=True, until=near)
    flagged = SimpleNamespace(active=True, until=near, open_ended=True)
    assert MaintenanceManager.suppression_mark(plain, NOW) == near.replace(tzinfo=None)
    assert MaintenanceManager.suppression_mark(flagged, NOW) == NOW.replace(tzinfo=None)
    far = SimpleNamespace(active=True, until=NOW + timedelta(days=30))
    assert MaintenanceManager.suppression_mark(far, NOW) == NOW.replace(tzinfo=None)
    assert MaintenanceManager.suppression_mark(SimpleNamespace(active=True, until=None), NOW) == NOW.replace(tzinfo=None)


# ── MO-03 ────────────────────────────────────────────────────────────────────


def _probe_harness(svc, clock, monkeypatch):
    """Drive ``run_scheduled_probe`` minute by minute with a scripted result."""
    import app.controller.service as service_mod
    from app.model.models import MonitorState

    mono = {"t": 1000.0}
    monkeypatch.setattr(service_mod.time, "monotonic", lambda: mono["t"])
    result = {"state": MonitorState.DOWN}
    dispatched = []

    async def fake_dispatch(rd):
        dispatched.append((rd["monitor_id"], clock["now"]))
        down = result["state"] == MonitorState.DOWN
        return result["state"], None, "down" if down else None

    monkeypatch.setattr(svc, "_dispatch_probe", fake_dispatch)
    svc._probing = True

    async def tick(minute, *mids):
        clock["now"] = NOW + minute * MIN
        mono["t"] = 1000.0 + minute * 60
        for mid in mids:
            await svc.run_scheduled_probe(mid)

    return result, dispatched, tick


def test_no_false_outage_after_a_long_window(svc, clock, monkeypatch):
    """Host down for a 100-minute planned window, back at its end. The breaker
    used to skip probes for up to 30 min → `outage_persisted` for a host that
    was up. The host monitor is probed every interval now."""
    from app.model.models import MonitorState

    mid = _host(svc, "host:c", ci_id=HOST_CI)
    _publish(svc, _item(HOST_CI, windows=[_once(NOW, NOW + 100 * MIN)]))
    result, dispatched, tick = _probe_harness(svc, clock, monkeypatch)

    async def run():
        for minute in range(0, 101):
            await tick(minute, mid)
        result["state"] = MonitorState.UP  # host back exactly at the end of the window
        for minute in range(101, 140):
            await tick(minute, mid)
            svc.maintenance.evaluate_outages(now=NOW + minute * MIN)

    asyncio.run(run())
    assert _persisted(svc) == []
    assert len(dispatched) == 140  # never skipped: one probe per interval
    assert svc.get_monitor(mid)["latest_state"] == "UP" and svc.maintenance.list_outages() == []


def test_recovery_of_a_long_down_host_is_seen_within_one_interval(svc, clock, monkeypatch):
    from app.model.models import MonitorState

    mid = _host(svc, "host:r", ci_id=HOST_CI)
    result, dispatched, tick = _probe_harness(svc, clock, monkeypatch)

    async def run():
        for minute in range(0, 60):
            await tick(minute, mid)
        svc.maintenance.evaluate_outages(now=NOW + 59 * MIN)
        result["state"] = MonitorState.UP
        await tick(60, mid)

    asyncio.run(run())
    assert len(_persisted(svc)) == 1
    assert [p["outage_id"] for t, p in svc.ctx.events if t == "monitoring:outage_recovered"] == [
        _persisted(svc)[0]["outage_id"]
    ]
    assert svc.get_monitor(mid)["latest_state"] == "UP" and not svc._host_circuit_until


def test_service_circuit_is_capped_and_closed_by_the_host_probe(svc, clock, monkeypatch):
    from app.model.models import MonitorState

    host = _host(svc, "host:s", ci_id=HOST_CI)
    web = _service(svc, "service:s:web", "s", ci_id=SVC_CI, host_ci_id=HOST_CI)
    result, dispatched, tick = _probe_harness(svc, clock, monkeypatch)

    async def run():
        for minute in range(0, 40):
            await tick(minute, host, web)
        open_for = svc._host_circuit_until["s"] - time.monotonic()
        web_probes = [m for m, _ in dispatched if m == web]
        result["state"] = MonitorState.UP
        await tick(40, host)  # the canary succeeds …
        closed = "s" not in svc._host_circuit_until
        await tick(41, web)  # … and the service is probed on its next interval
        return open_for, len(web_probes), closed

    open_for, web_probes, closed = asyncio.run(run())
    assert 0 < open_for <= 300  # min(1800, offline_minutes · 60 / 2) with the default 10 min
    assert web_probes < 40  # the breaker still spares the dependent probes
    assert closed and svc.get_monitor(web)["latest_state"] == "UP"
    svc.apply_settings({"outage_default_offline_minutes": "0"})
    assert svc._circuit_cap() == 1800.0
    svc.apply_settings({"outage_default_offline_minutes": "1"})
    assert svc._circuit_cap() == 60.0


def test_circuit_is_reset_when_the_maintenance_of_a_service_ends(svc, clock, monkeypatch):
    """No host monitor to act as canary: a service still shown as MAINTENANCE
    after its window ended is probed at once and its circuit is closed."""
    web = _service(svc, "service:x:web", "x", ci_id=SVC_CI)
    _publish(svc, _item(SVC_CI, kind="service", windows=[_once(NOW, NOW + 30 * MIN)]))
    result, dispatched, tick = _probe_harness(svc, clock, monkeypatch)

    async def run():
        for minute in range(0, 30):
            await tick(minute, web)
        inside = len(dispatched)
        assert svc._host_circuit_until.get("x", 0) > time.monotonic()  # open, well past the window end
        await tick(30, web)  # window over
        return inside

    inside = asyncio.run(run())
    assert inside < 30 and len(dispatched) == inside + 1
    assert dispatched[-1] == (web, NOW + 30 * MIN)
    assert svc.get_monitor(web)["latest_state"] == "DOWN"  # counted from the end of the window


def test_scheduled_evaluator_confirms_down_with_a_fresh_probe(svc, clock, monkeypatch):
    from app.model.models import MonitorState, MonitorType, MonitorUpsert

    back = _host(svc, "host:back", ci_id=HOST_CI)
    still = _host(svc, "host:still", ci_id=SVC_CI)
    svc.upsert_monitor(MonitorUpsert(monitor_id="passive:x", name="p", monitor_type=MonitorType.SERVICE,
                                     ci_id="33333333-3333-4333-8333-333333333333"), sync_scheduler=False)
    for mid in (back, still, "passive:x"):
        _down(svc, mid)
    svc._host_circuit_until["back"] = time.monotonic() + 9999  # the breaker must not hide the host
    probed = []

    async def fake_dispatch(rd):
        probed.append(rd["monitor_id"])
        if rd["monitor_id"] == back:
            return MonitorState.UP, 1.0, None
        return MonitorState.DOWN, None, "still down"

    monkeypatch.setattr(svc, "_dispatch_probe", fake_dispatch)
    svc._probing = True
    clock["now"] = NOW + 11 * MIN
    stats = asyncio.run(svc.evaluate_outages())

    assert sorted(probed) == [back, still]  # the passive monitor has nothing to probe
    assert stats["persisted"] == 2
    assert sorted(p["monitor_id"] for p in _persisted(svc)) == [still, "passive:x"]
    assert svc.get_monitor(back)["latest_state"] == "UP" and _row(svc, back) is None


def test_unconfirmed_outage_is_not_reported(svc, clock, monkeypatch):
    """Probe could not run (quiesced) or says UNKNOWN (ICMP unavailable here):
    nothing is reported this round; the row stays for the next one."""
    from app.model.models import MonitorState

    mid = _host(svc, "host:u", ci_id=HOST_CI)
    _down(svc, mid)
    svc._probing = True
    clock["now"] = NOW + 11 * MIN

    svc.apply_quiesce(True)
    assert asyncio.run(svc.evaluate_outages())["persisted"] == 0
    svc._quiesced_until = 0.0

    async def unknown(rd):
        return MonitorState.UNKNOWN, None, "ICMP unavailable on this instance: x"

    monkeypatch.setattr(svc, "_dispatch_probe", unknown)
    assert asyncio.run(svc.evaluate_outages())["persisted"] == 0
    assert _persisted(svc) == [] and _row(svc, mid).state == "open"
    # the direct (sync) evaluator does not report a monitor that is not DOWN either
    stats = svc.maintenance.evaluate_outages(now=NOW + 12 * MIN)
    assert stats["persisted"] == 0 and stats["unconfirmed"] == 1


# ── MO-12 ────────────────────────────────────────────────────────────────────


def _count_effective_state(monkeypatch):
    from core.api import maintenance as mw

    calls = {"n": 0}
    real = mw.effective_state

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(mw, "effective_state", counting)
    return calls


def test_effective_state_is_evaluated_once_per_ci(svc, clock, monkeypatch):
    _host(svc, "host:m", ci_id=HOST_CI)
    for i in range(60):
        _service(svc, f"service:m:s{i:02d}", "m")  # borrow the host CI
    always_on = {"type": "cron", "expr": "*/15 * * * *", "duration_minutes": 30, "tz": "UTC"}
    _publish(svc, _item(HOST_CI, windows=[always_on]))  # the 191 ms-per-call case of the audit
    calls = _count_effective_state(monkeypatch)

    started = time.perf_counter()
    monitors = svc.list_monitors()
    took = time.perf_counter() - started
    assert len(monitors) == 61 and all(m["in_maintenance"] for m in monitors)
    assert calls["n"] == 1 and took < 1.0

    for mid in ("service:m:s00", "service:m:s01", "host:m"):  # heartbeats + evaluator share it too
        _down(svc, mid)
    svc.maintenance.evaluate_outages(now=clock["now"])
    assert calls["n"] == 1
    svc.maintenance.evaluate_outages(now=clock["now"] + timedelta(seconds=61))  # TTL over
    assert calls["n"] == 2


def test_memo_never_serves_a_state_across_a_transition_or_a_reload(svc, clock, monkeypatch):
    mid = _host(svc, "host:t", ci_id=HOST_CI)
    _publish(svc, _item(HOST_CI, windows=[_once(NOW - 5 * MIN, NOW + timedelta(seconds=20)),
                                          _once(NOW + timedelta(seconds=40), NOW + 10 * MIN)]))
    calls = _count_effective_state(monkeypatch)
    cache = svc.maintenance.cache
    record = {"monitor_id": mid, "ci_id": HOST_CI, "monitor_type": "server", "host_name": "t"}
    assert cache.state_for(record, NOW).active is True
    assert cache.state_for(record, NOW + timedelta(seconds=19)).active is True and calls["n"] == 1
    assert cache.state_for(record, NOW + timedelta(seconds=20)).active is False  # window end
    assert cache.state_for(record, NOW + timedelta(seconds=39)).active is False and calls["n"] == 2
    assert cache.state_for(record, NOW + timedelta(seconds=40)).active is True  # next start
    assert calls["n"] == 3
    assert cache.state_for(record, NOW).active is True and calls["n"] == 4  # clock went back: recompute

    _publish(svc, _item(HOST_CI, windows=[]))  # reload drops the memo
    assert cache.state_for(record, NOW + timedelta(seconds=41)).active is False

    # an override that starts later bounds the validity as well
    from core.api import maintenance as mw

    svc.maintenance.on_override_started({
        "override_id": "later", "ci_ids": [HOST_CI], "source": "iac-orchestrator",
        "until": mw.iso(NOW + 60 * MIN), "started_at": mw.iso(NOW + timedelta(seconds=70)),
    })
    assert cache.state_for(record, NOW + timedelta(seconds=45)).active is False
    assert cache.state_for(record, NOW + timedelta(seconds=70)).active is True


def test_monitor_level_windows_do_not_leak_to_siblings_on_the_same_ci(svc, clock):
    from app.model.models import WindowCreate
    from core.api import maintenance as mw

    _host(svc, "host:n", ci_id=HOST_CI)
    a = _service(svc, "service:n:a", "n")
    b = _service(svc, "service:n:b", "n")
    svc.maintenance.reload()
    svc.maintenance.create_manual_window(WindowCreate(
        monitor_id=a, windows=[{"type": "once", "start": mw.iso(NOW - MIN), "end": mw.iso(NOW + 30 * MIN)}],
    ))
    # the manual row carries the (host) CI too, so both are covered — but each
    # monitor is evaluated under its own key
    states = {m["monitor_id"]: m["in_maintenance"] for m in svc.list_monitors()}
    assert states[a] is True and states[b] is True


def test_endless_cron_window_is_rejected_with_a_clear_message(svc, clock):
    from app.model.models import WindowCreate

    mnt = svc.maintenance
    with pytest.raises(ValueError, match="never ends"):
        mnt.create_manual_window(WindowCreate(ci_id=HOST_CI, windows=[
            {"type": "cron", "expr": "*/15 * * * *", "duration_minutes": 30, "tz": "UTC"}]))
    with pytest.raises(ValueError, match="never ends"):
        mnt.create_manual_window(WindowCreate(ci_id=HOST_CI, windows=[
            {"type": "cron", "expr": "0 * * * *", "duration_minutes": 60, "tz": "UTC"}]))
    assert mnt.list_windows() == []
    ok = mnt.create_manual_window(WindowCreate(ci_id=HOST_CI, windows=[
        {"type": "cron", "expr": "0 3 * * *", "duration_minutes": 120, "tz": "Europe/Berlin"}]))
    assert ok["windows"][0]["type"] == "cron"
