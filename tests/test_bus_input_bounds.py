"""MO-17: bus inputs are bounded — override end capped, replace opt-in, quiesce attributed."""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import timedelta, timezone


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_o = _load("_clock_base_bi", "test_outage_clock.py")
_w = _load("_windows_base_bi", "test_maintenance_windows.py")
svc, clock = _o.svc, _o.clock
NOW, MIN, HOST_CI = _o.NOW, _o.MIN, _o.HOST_CI
_host, _item, _once = _o._host, _w._item, _w._once


def parse(raw):
    from core.api import maintenance as mw

    return mw.parse_iso(raw) if isinstance(raw, str) else raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw


def _start(svc, until, override_id="dep-1"):
    from core.api import maintenance as mw

    svc.maintenance.on_override_started({
        "override_id": override_id, "ci_ids": [HOST_CI], "source": "iac-orchestrator",
        "until": mw.iso(until), "started_at": mw.iso(NOW), "linger_minutes": 15,
    })
    return svc.maintenance.store.get_override(override_id)


def test_bus_override_without_a_sane_end_is_capped(svc, clock):
    stored = _start(svc, NOW + timedelta(days=365 * 70))  # "until 2099"
    cap = timedelta(minutes=max(svc.maintenance.settings.override_max_minutes, 24 * 60))
    assert parse(stored["until"]) == NOW + cap


def test_bus_override_within_the_cap_is_untouched(svc, clock):
    stored = _start(svc, NOW + timedelta(hours=4))
    assert parse(stored["until"]) == NOW + timedelta(hours=4)


def test_windows_published_without_replaces_flag_does_not_wipe_the_source(svc, clock):
    _host(svc, "host:a", ci_id=HOST_CI)
    mnt = svc.maintenance
    window = _once(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    other = "22222222-2222-2222-2222-222222222222"
    mnt.on_windows_published({"source": "iac-orchestrator", "replaces": True,
                              "items": [_item(HOST_CI, [window]), _item(other, [window])]})
    assert len(mnt.store.list_windows(source="iac-orchestrator")) == 2
    mnt.on_windows_published({"source": "iac-orchestrator", "items": [_item(HOST_CI, [window])]})  # no flag
    assert len(mnt.store.list_windows(source="iac-orchestrator")) == 2
    mnt.on_windows_published({"source": "iac-orchestrator", "replaces": True, "items": [_item(HOST_CI, [window])]})
    assert len(mnt.store.list_windows(source="iac-orchestrator")) == 1


def test_peer_quiesce_request_is_logged_with_its_origin(svc):
    logged: list = []
    svc.ctx.log.info = logged.append
    svc.on_quiesce({"on": True, "ttl": 30, "_origin": "pod-7"})
    assert any("pod-7" in m and "quiesce on" in m for m in logged)
    assert svc._quiesced_until > 0
