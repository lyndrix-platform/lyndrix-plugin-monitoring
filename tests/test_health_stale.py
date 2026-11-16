"""MO-11 — "nobody probes" is visible in health() on every instance."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
from datetime import timedelta

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


def _age(svc, mid, seconds, created_too=True):
    from app.model.models import MonitorRecord, _utc_now

    with svc._session() as s:
        row = s.get(MonitorRecord, mid)
        if row.last_checked_at is not None:
            row.last_checked_at = _utc_now() - timedelta(seconds=seconds)
        if created_too:
            row.created_at = _utc_now() - timedelta(seconds=seconds)
        s.commit()


def _health(svc, monkeypatch):
    import monitoring_plugin.entrypoint as ep

    monkeypatch.setitem(ep.plugin_state, "service", svc)
    return asyncio.run(ep.health(svc.ctx))


def test_standby_reports_probes_stale_when_nobody_probes(svc, monkeypatch):
    for i in range(5):
        mid = _monitor(svc, f"host:h{i}")
        _beat(svc, mid, "UP", 1.0)
    assert svc.is_prober is False  # a standby: it never probes itself
    fresh = _health(svc, monkeypatch)
    assert fresh.status == "ok" and fresh.details["monitors_stale"] == 0
    assert fresh.details["last_probe_age_s"] is not None and fresh.details["last_probe_age_s"] < 5

    for i in range(5):
        _age(svc, f"host:h{i}", 3600)  # the prober died an hour ago; every state still reads UP
    stale = _health(svc, monkeypatch)
    assert stale.status == "degraded" and stale.details["reason"] == "probes_stale"
    assert stale.details["monitors_stale"] == 5 and stale.details["last_probe_age_s"] >= 3590
    assert stale.details["monitors_up"] == 5


def test_stale_threshold_shares_and_exclusions(svc):
    from app.model.models import AdminOverride, MonitorType, MonitorUpsert

    for i in range(10):
        _beat(svc, _monitor(svc, f"host:h{i}"), "UP", 1.0)
    # default interval 60 s, circuit pause up to 300 s → stale after 3·60 + 300 s
    _age(svc, "host:h0", 400)
    assert svc.probe_freshness()["stale"] == 0
    _age(svc, "host:h0", 500)
    one = svc.probe_freshness()
    assert one["stale"] == 1 and one["probeable"] == 10 and one["probes_stale"] is False  # 10 % < 20 %
    _age(svc, "host:h1", 500)
    assert svc.probe_freshness()["probes_stale"] is True  # a dead shard owner: 20 %

    # paused and passive monitors are not expected to move
    svc.apply_admin_override(AdminOverride(monitor_id="host:h0", action="pause"))
    svc.apply_admin_override(AdminOverride(monitor_id="host:h1", action="pause"))
    svc.upsert_monitor(MonitorUpsert(monitor_id="passive:p", name="p", monitor_type=MonitorType.SERVICE),
                       sync_scheduler=False)
    _age(svc, "passive:p", 9999)
    after = svc.probe_freshness()
    assert after["stale"] == 0 and after["probeable"] == 8


def test_new_monitor_is_not_stale_until_it_should_have_been_probed(svc):
    mid = _monitor(svc, "host:new")
    assert svc.probe_freshness() == {
        "probeable": 1, "stale": 0, "last_probe_at": None, "last_probe_age_s": None, "probes_stale": False,
    }
    _age(svc, mid, 3600)  # created an hour ago and never probed
    never = svc.probe_freshness()
    assert never["stale"] == 1 and never["probes_stale"] is True and never["last_probe_at"] is None
