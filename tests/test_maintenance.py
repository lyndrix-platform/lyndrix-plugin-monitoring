"""Maintenance actions (settings dialog buttons) — real service against in-memory
SQLite. Run inside lyndrix-core-dev like tests/test_perf_and_status.py.
"""
from __future__ import annotations

import pytest

import importlib.util
import pathlib

# Reuse the SQLite service fixture + helpers of the perf tests. Loaded by path so
# it works under every pytest import mode (the tests dir is not a package).
_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture (discovered as a module attribute)
_monitor, _beat = _base._monitor, _base._beat


def _managed(svc, mid):
    return _monitor(svc, mid, owner_source="orchestrator_service")


def _rows(svc):
    from app.model.models import MonitorDailyAggregate, MonitorHeartbeat, MonitorRecord
    with svc._session() as s:
        return (
            s.query(MonitorRecord).count(),
            s.query(MonitorHeartbeat).count(),
            s.query(MonitorDailyAggregate).count(),
        )


def test_delete_managed_keeps_manual_monitors(svc):
    a = _managed(svc, "host:docker-hydra")
    b = _managed(svc, "service:docker-hydra:traefik")
    manual = _monitor(svc, "manual:printer")  # owner_source defaults to ui_admin
    for mid in (a, b, manual):
        _beat(svc, mid, "UP", 5.0)
        _beat(svc, mid, "DOWN")
    assert _rows(svc) == (3, 6, 3)

    result = svc.delete_monitors("managed")
    assert result == {"monitors": 2, "heartbeats": 4, "aggregates": 2}
    assert _rows(svc) == (1, 2, 1)
    assert [m["monitor_id"] for m in svc.list_monitors()] == [manual]


def test_delete_all_removes_everything(svc):
    _managed(svc, "host:a")
    _monitor(svc, "manual:b")
    _beat(svc, "host:a", "UP")
    svc.PRUNE_BATCH = 1  # exercise the batched heartbeat delete loop
    assert svc.delete_monitors("all") == {"monitors": 2, "heartbeats": 1, "aggregates": 1}
    assert _rows(svc) == (0, 0, 0)
    with pytest.raises(ValueError):
        svc.delete_monitors("bogus")


def test_reset_statistics_keeps_states(svc):
    mid = _monitor(svc)
    for state in ("UP", "UP", "DOWN"):
        _beat(svc, mid, state, 7.0)
    svc._refresh_uptime_windows_sync()
    before = svc.list_monitors()[0]
    assert before["latest_state"] == "DOWN" and before["uptime_24h"] < 100.0

    assert svc.reset_statistics() == {"heartbeats": 3, "aggregates": 1}
    after = svc.list_monitors()[0]
    assert _rows(svc) == (1, 0, 0)
    assert after["latest_state"] == "DOWN"          # state kept …
    assert after["uptime_24h"] == 100.0             # … statistics gone
    assert after["uptime_all"] == 100.0


def test_rebuild_statistics_recomputes_from_heartbeats(svc):
    from app.model.models import MonitorDailyAggregate
    mid = _monitor(svc)
    for state in ("UP", "DOWN", "UP", "UP"):
        _beat(svc, mid, state)
    with svc._session() as s:
        s.query(MonitorDailyAggregate).delete()
        s.commit()
    assert svc.stats()["uptime_all"] == 100.0  # no aggregates → reads as 100 %

    assert svc.rebuild_statistics() == {"monitors": 1}
    assert svc.stats()["uptime_all"] == 75.0
    assert svc.list_monitors()[0]["uptime_24h"] == 75.0


def test_request_full_probe_forces_every_monitor_without_blocking(svc):
    mid = _monitor(svc)
    _beat(svc, mid, "UP")  # fresh + UP → not a candidate for the normal sweep
    assert svc._load_probe_candidates() == []

    svc._probing = True  # only the prober sweeps (a standby forwards via the bus)
    svc.request_full_probe()
    assert svc._load_probe_candidates() == [mid]
    assert svc._sweep_blocking is False
    # one-shot: the flag is consumed
    assert svc._load_probe_candidates() == []


def test_reset_probe_state_clears_bookkeeping(svc):
    svc._consec_failures["x"] = 3
    svc._host_failures["h"] = 2
    svc._host_circuit_until["h"] = 1e12
    svc._view_cache["k"] = (0.0, {})
    svc.reset_probe_state()
    assert not svc._consec_failures and not svc._host_failures
    assert not svc._host_circuit_until and not svc._view_cache


def test_manifest_declares_maintenance_actions():
    import monitoring_plugin.entrypoint as ep
    actions = {f.key: f for f in ep.manifest.settings_schema if f.kind == "action"}
    assert set(actions) == {
        "action_prune", "action_rebuild_stats", "action_probe_all", "action_reset_stats",
        "action_clear_states", "action_delete_managed", "action_delete_all",
    }
    assert all(f.category == "Maintenance" for f in actions.values())
    destructive = {"action_reset_stats", "action_clear_states", "action_delete_managed", "action_delete_all"}
    assert all(actions[k].confirm and actions[k].danger for k in destructive)
    assert actions["action_delete_managed"].action == "/maintenance/delete-monitors/managed"
