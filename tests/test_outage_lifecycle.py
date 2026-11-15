"""Outage lifecycle — MO-08: every path that drops a REPORTED outage emits
``monitoring:outage_recovered`` with a ``reason``. Fake clock, real service
against in-memory SQLite."""

from __future__ import annotations

import importlib.util
import pathlib

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
_persisted, _recovered = _o._persisted, _o._recovered


def _reported(svc, clock, mid="host:b", **extra):
    """A host with a reported (persisted) outage; returns its outage_id."""
    _host(svc, mid, ci_id=HOST_CI, **extra)
    _down(svc, mid)
    svc.maintenance.evaluate_outages(now=NOW + 11 * MIN)
    assert len(_persisted(svc)) == 1
    clock["now"] = NOW + 20 * MIN
    return _persisted(svc)[0]["outage_id"]


def _drop(svc, how, mid):
    from app.model.models import AdminOverride, InventorySyncPayload, MonitorType, MonitorUpsert

    if how == "pause":
        svc.apply_admin_override(AdminOverride(monitor_id=mid, action="pause"))
    elif how == "disable":
        svc.upsert_monitor(MonitorUpsert(
            monitor_id=mid, name=mid, monitor_type=MonitorType.SERVER, target="10.0.0.1", address="10.0.0.1",
            host_name="b", ci_id=HOST_CI, owner_source="orchestrator_service", enabled=False,
        ), sync_scheduler=False)
    elif how == "delete":
        svc.delete_monitors("all")
    elif how == "reset":
        svc.clear_states_db()
    elif how == "inventory":
        svc.ingest_inventory_snapshot(InventorySyncPayload(
            owner_source="orchestrator_service", hosts=[{"host_name": "other", "address": "10.0.0.2"}],
        ))


@pytest.mark.parametrize("how,reason", [
    ("pause", "paused"), ("disable", "paused"), ("delete", "monitor_removed"),
    ("inventory", "monitor_removed"), ("reset", "reset"),
])
def test_dropping_a_reported_outage_emits_recovered_with_reason(svc, clock, how, reason):
    outage_id = _reported(svc, clock, owner_source="orchestrator_service")
    _drop(svc, how, "host:b")
    assert _recovered(svc) == [{
        "outage_id": outage_id,
        "ci_id": HOST_CI,
        "since": "2026-10-20T12:00:00Z",
        "recovered_at": "2026-10-20T12:20:00Z",
        "duration_s": 20 * 60,
        "reason": reason,
    }]
    assert svc.maintenance.list_outages() == []
    # emitted locally like every outage event — never bridged
    assert [c for t, _, c in svc.ctx.emits if t == "monitoring:outage_recovered"] == [False]


@pytest.mark.parametrize("how", ["pause", "disable", "delete", "reset"])
def test_an_unreported_outage_is_dropped_silently(svc, clock, how):
    _host(svc, "host:b", ci_id=HOST_CI, owner_source="orchestrator_service")
    _down(svc, "host:b")  # open, not yet reported
    _drop(svc, how, "host:b")
    assert _recovered(svc) == [] and svc.maintenance.list_outages() == []


def test_only_the_affected_monitor_is_closed(svc, clock):
    from app.model.models import AdminOverride

    outage_id = _reported(svc, clock)
    _host(svc, "host:other", ci_id=SVC_CI)
    _down(svc, "host:other")
    svc.maintenance.evaluate_outages(now=NOW + 40 * MIN)
    assert len(_persisted(svc)) == 2
    svc.apply_admin_override(AdminOverride(monitor_id="host:b", action="pause"))
    assert [r["outage_id"] for r in _recovered(svc)] == [outage_id]
    assert [o["monitor_id"] for o in svc.maintenance.list_outages()] == ["host:other"]


def test_retried_delete_emits_exactly_one_recovered(svc, clock):
    """A 1020 retry of the delete transaction must not duplicate the event."""
    _reported(svc, clock)

    class _Conflict(Exception):
        orig = type("E", (), {"args": (1213, "Deadlock found")})()

    real, state = svc._retrying, {"armed": True}

    def flaky(session, fn, *a, **kw):
        def attempt():
            result = fn()  # queues the event, commits …
            if state["armed"] and session.info.get("monitoring_events"):
                state["armed"] = False
                raise _Conflict()  # … pretend the commit lost a deadlock
            return result

        return real(session, attempt, *a, **kw)

    # the first attempt must not have committed: make the commit a no-op once
    from sqlalchemy.orm import Session

    real_commit = Session.commit
    calls = {"n": 0}

    def commit_once_lost(self):
        if state["armed"] and self.info.get("monitoring_events"):
            calls["n"] += 1
            return None
        return real_commit(self)

    Session.commit = commit_once_lost
    svc._retrying = flaky
    try:
        svc.delete_monitors("all")
    finally:
        svc._retrying = real
        Session.commit = real_commit
    assert calls["n"] == 1 and len(_recovered(svc)) == 1
