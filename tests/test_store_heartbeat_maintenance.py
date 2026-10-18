"""``_store_heartbeat`` under maintenance (1.7.0): DOWN inside a window becomes
MAINTENANCE, aggregates/uptime exclude it, the outage clock row follows the
heartbeat in the same transaction. Real service against in-memory SQLite; run
inside lyndrix-core-dev like the other suites."""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


def _host(svc, mid, ci_id=None, host_name=None, **extra):
    """A real host monitor (type ``server``, ``host_name`` = the host) — the shape
    the inventory sync produces; ``_monitor`` makes PING monitors."""
    from app.model.models import MonitorType, MonitorUpsert

    hn = host_name or mid.split(":", 1)[-1]
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id=mid,
            name=mid,
            monitor_type=MonitorType.SERVER,
            target="10.0.0.1",
            address="10.0.0.1",
            host_name=hn,
            ci_id=ci_id,
            **extra,
        ),
        sync_scheduler=False,
    )
    return mid


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
HOST_CI = "11111111-1111-4111-8111-111111111111"


@pytest.fixture()
def clock(monkeypatch):
    """Freeze the plugin clock (service + models) at ``clock["now"]``."""
    import app.controller.service as service_mod
    import app.model.models as models_mod
    from core.api import maintenance as mw

    state = {"now": NOW}
    monkeypatch.setattr(service_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(models_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(mw, "utcnow", lambda: state["now"])
    return state


def _window(svc, ci_id, start, end, source="iac-orchestrator"):
    from core.api import maintenance as mw

    svc.maintenance.on_windows_published(
        {
            "source": source,
            "published_at": mw.iso(NOW),
            "replaces": True,
            "items": [
                {
                    "ci_id": ci_id,
                    "kind": "host",
                    "name": "h",
                    "inherited_from": None,
                    "windows": [
                        {"type": "once", "start": mw.iso(start), "end": mw.iso(end)}
                    ],
                }
            ],
        }
    )


def _outage(svc, mid):
    from app.model.models import OutageRecord

    with svc._session() as s:
        row = s.get(OutageRecord, mid)
        return (
            None if row is None else (row.state, row.down_since, row.suppressed_until)
        )


def _agg(svc, mid):
    from app.model.models import MonitorDailyAggregate

    with svc._session() as s:
        a = (
            s.query(MonitorDailyAggregate)
            .filter(MonitorDailyAggregate.monitor_id == mid)
            .one()
        )
        return (
            a.total_samples,
            a.up_samples,
            a.down_samples,
            a.paused_samples,
            a.maintenance_samples,
        )


def test_down_inside_window_is_stored_as_maintenance(svc, clock):
    from app.model.models import MonitorHeartbeat

    mid = _host(svc, "host:a", ci_id=HOST_CI)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    svc._save_probe_result(mid, _base_state("DOWN"), None, "connect timeout")

    m = svc.get_monitor(mid)
    assert m["latest_state"] == "MAINTENANCE"
    assert m["latest_error"] == "connect timeout"  # kept for diagnosis
    assert m["in_maintenance"] is True and m["maintenance_source"] == "window"
    assert (
        m["maintenance_until"] == "2026-10-20T13:00:00Z"
        and m["outage_since"] == "2026-10-20T12:00:00Z"
    )
    with svc._session() as s:
        hb = s.query(MonitorHeartbeat).filter(MonitorHeartbeat.monitor_id == mid).one()
    assert hb.state == "MAINTENANCE"
    assert _agg(svc, mid) == (1, 0, 0, 0, 1)
    # the transition event carries the maintenance flag and the ci
    ev = next(p for t, p in svc.ctx.events if t == "monitoring:state_changed")
    assert (
        ev["new_state"] == "MAINTENANCE"
        and ev["maintenance"] is True
        and ev["ci_id"] == HOST_CI
    )


def test_up_inside_window_stays_up_and_outside_down_stays_down(svc, clock):
    mid = _host(svc, "host:b", ci_id=HOST_CI)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    svc._save_probe_result(mid, _base_state("UP"), 4.0, None)
    assert svc.get_monitor(mid)["latest_state"] == "UP" and _outage(svc, mid) is None

    clock["now"] = NOW + timedelta(hours=2)  # window over
    svc._save_probe_result(mid, _base_state("DOWN"), None, "down")
    m = svc.get_monitor(mid)
    assert m["latest_state"] == "DOWN" and m["in_maintenance"] is False
    assert _outage(svc, mid) == (
        "open",
        (NOW + timedelta(hours=2)).replace(tzinfo=None),
        None,
    )


def test_uptime_denominator_excludes_maintenance(svc, clock):
    mid = _host(svc, "host:c", ci_id=HOST_CI)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    for state in ("UP", "UP", "DOWN", "DOWN"):  # the two DOWNs are inside the window
        svc._save_probe_result(
            mid, _base_state(state), 1.0 if state == "UP" else None, None
        )
    assert _agg(svc, mid) == (4, 2, 0, 0, 2)
    assert svc.list_monitors()[0]["uptime_all"] == 100.0  # 2 up of 2 counted samples
    svc._refresh_uptime_windows_sync()
    m = svc.list_monitors()[0]
    assert (
        m["uptime_24h"] == 100.0
        and m["uptime_7d"] == 100.0
        and m["uptime_30d"] == 100.0
    )
    # recompute from raw heartbeats agrees with the incremental counters
    with svc._session() as s:
        from app.model.models import MonitorRecord

        svc._recompute_daily_aggregate(s, s.get(MonitorRecord, mid))
        s.commit()
    assert _agg(svc, mid) == (4, 2, 0, 0, 2)
    assert (
        svc.stats()["maintenance_count"] == 1
        and svc.status_counts()["maintenance_count"] == 1
    )


def test_outage_row_follows_the_heartbeats(svc, clock):
    mid = _host(svc, "host:d", ci_id=HOST_CI)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    svc._save_probe_result(mid, _base_state("DOWN"), None, "x")
    assert _outage(svc, mid) == (
        "open",
        NOW.replace(tzinfo=None),
        (NOW + timedelta(hours=1)).replace(tzinfo=None),
    )

    # the window is extended → suppressed_until follows (down_since does not move)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=2))
    clock["now"] = NOW + timedelta(minutes=5)
    svc._save_probe_result(mid, _base_state("DOWN"), None, "x")
    assert _outage(svc, mid) == (
        "open",
        NOW.replace(tzinfo=None),
        (NOW + timedelta(hours=2)).replace(tzinfo=None),
    )

    # an UNKNOWN sample neither opens nor closes the clock
    svc._save_probe_result(mid, _base_state("UNKNOWN"), None, None)
    assert _outage(svc, mid)[0] == "open"

    # UP → an unreported outage simply disappears (no recovered event)
    svc._save_probe_result(mid, _base_state("UP"), 2.0, None)
    assert _outage(svc, mid) is None
    assert not any(t == "monitoring:outage_recovered" for t, _ in svc.ctx.events)


def test_passive_results_are_treated_the_same(svc, clock):
    from app.model.models import PassiveResult

    mid = _host(svc, "host:e", ci_id=HOST_CI)
    _window(svc, HOST_CI, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    out = svc.ingest_passive_result(
        PassiveResult(monitor_id=mid, state="DOWN", timestamp=NOW.isoformat())
    )
    assert out["latest_state"] == "MAINTENANCE"
    assert _outage(svc, mid)[0] == "open"
    with pytest.raises(
        ValueError
    ):  # MAINTENANCE is derived, never reported (pydantic ValidationError)
        PassiveResult(monitor_id=mid, state="MAINTENANCE")


def test_admin_pause_clears_the_outage_clock(svc, clock):
    from app.model.models import AdminOverride

    mid = _host(svc, "host:f", ci_id=HOST_CI)
    svc._save_probe_result(mid, _base_state("DOWN"), None, "x")
    assert _outage(svc, mid) is not None
    svc.apply_admin_override(AdminOverride(monitor_id=mid, action="pause"))
    assert (
        _outage(svc, mid) is None and svc.get_monitor(mid)["latest_state"] == "PAUSED"
    )


def test_clear_states_and_delete_monitors_drop_outages(svc, clock):
    from app.model.models import OutageRecord, WindowCreate

    a = _host(svc, "host:g", ci_id=HOST_CI, owner_source="orchestrator_service")
    b = _host(svc, "manual:h")
    for mid in (a, b):
        svc._save_probe_result(mid, _base_state("DOWN"), None, "x")
    svc.maintenance.create_manual_window(
        WindowCreate(
            monitor_id=a,
            windows=[{"weekly": {"days": ["sun"], "start": "02:00", "end": "04:00"}}],
        )
    )
    with svc._session() as s:
        assert s.query(OutageRecord).count() == 2
    svc.delete_monitors("managed")
    with svc._session() as s:
        assert [r.monitor_id for r in s.query(OutageRecord).all()] == [b]
    assert (
        svc.maintenance.store.list_windows(monitor_id=a) == []
    )  # pinned manual window went with the monitor
    svc.clear_states_db()
    with svc._session() as s:
        assert s.query(OutageRecord).count() == 0


def test_aggregate_state_orders_maintenance_between_unknown_and_up(svc):
    from app.ui.styles import aggregate_state, state_badge_classes, state_color

    assert aggregate_state(["UP", "MAINTENANCE"]) == "MAINTENANCE"
    assert aggregate_state(["PAUSED", "MAINTENANCE"]) == "MAINTENANCE"
    assert aggregate_state(["UNKNOWN", "MAINTENANCE"]) == "UNKNOWN"
    assert aggregate_state(["DOWN", "MAINTENANCE"]) == "DOWN"
    assert state_badge_classes("MAINTENANCE") == "border lx-badge--maintenance"
    assert (
        state_color("MAINTENANCE")
        == "var(--lx-state-maintenance, var(--lx-state-paused))"
    )


def _base_state(name: str):
    from app.model.models import MonitorState

    return MonitorState(name)
