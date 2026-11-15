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


# ── MO-09: outbox — an outage event survives a failed emit or a crash ─────────


def _outbox(svc):
    from app.model.models import OutageEventRecord

    with svc._session() as s:
        return [(r.topic.split(":")[1], r.attempts) for r in s.query(OutageEventRecord).order_by(OutageEventRecord.id)]


def _failing_emit(svc, failures):
    """Make ``ctx.emit`` raise for the next ``failures`` outage events."""
    real = svc.ctx.emit
    left = {"n": failures}

    def emit(topic, payload, cluster=False):
        if topic.startswith("monitoring:outage_") and left["n"] > 0:
            left["n"] -= 1
            raise RuntimeError("bus unavailable")
        return real(topic, payload, cluster=cluster)

    svc.ctx.emit = emit
    return left


def test_failed_emit_is_redelivered_on_a_later_evaluator_pass(svc, clock):
    mid = _host(svc, "host:o", ci_id=HOST_CI)
    _down(svc, mid)
    _failing_emit(svc, 1)
    mnt = svc.maintenance
    assert mnt.evaluate_outages(now=NOW + 10 * MIN)["persisted"] == 1
    assert _persisted(svc) == [] and _outbox(svc) == [("outage_persisted", 1)]
    assert mnt.list_outages()[0]["state"] == "persisted"  # the state change itself is durable

    mnt.evaluate_outages(now=NOW + 10 * MIN)  # same instant: the failed attempt still holds its claim
    assert _persisted(svc) == []
    mnt.evaluate_outages(now=NOW + 12 * MIN)
    assert len(_persisted(svc)) == 1 and _outbox(svc) == []
    mnt.evaluate_outages(now=NOW + 14 * MIN)
    assert len(_persisted(svc)) == 1  # delivered once, not again


def test_event_of_a_process_that_died_after_the_commit_is_delivered_by_the_evaluator(svc, clock, monkeypatch):
    outage_id = _reported(svc, clock)
    mnt = svc.maintenance
    with monkeypatch.context() as died:  # the process "dies" before emitting
        died.setattr(mnt, "flush_session_events", lambda session, now=None: 0)
        _up(svc, "host:b")
    assert _recovered(svc) == [] and _outbox(svc) == [("outage_recovered", 0)]
    clock["now"] = NOW + 21 * MIN
    mnt.outage_housekeeping()
    assert [r["outage_id"] for r in _recovered(svc)] == [outage_id] and _outbox(svc) == []


def test_recovered_never_overtakes_its_undelivered_persisted(svc, clock):
    mid = _host(svc, "host:p", ci_id=HOST_CI)
    _down(svc, mid)
    left = _failing_emit(svc, 1)
    mnt = svc.maintenance
    mnt.evaluate_outages(now=NOW + 10 * MIN)  # persisted: emit fails, stays in the outbox
    clock["now"] = NOW + 10 * MIN
    _up(svc, mid)  # recovery right behind it
    assert left["n"] == 0 and _persisted(svc) == [] and _recovered(svc) == []
    assert [t for t, _ in _outbox(svc)] == ["outage_persisted", "outage_recovered"]

    mnt.outage_housekeeping(NOW + 12 * MIN)
    order = [t for t, _ in svc.ctx.events if t.startswith("monitoring:outage_")]
    assert order == ["monitoring:outage_persisted", "monitoring:outage_recovered"] and _outbox(svc) == []


def test_a_row_claimed_by_another_instance_is_left_alone_until_the_claim_expires(svc, clock):
    from app.model.models import OutageEventRecord

    mid = _host(svc, "host:q", ci_id=HOST_CI)
    _down(svc, mid)
    _failing_emit(svc, 1)
    mnt = svc.maintenance
    mnt.evaluate_outages(now=NOW + 10 * MIN)
    with svc._session() as s:  # a peer is delivering it right now
        s.query(OutageEventRecord).update({OutageEventRecord.claimed_until: (NOW + 30 * MIN).replace(tzinfo=None)})
        s.commit()
    assert mnt.deliver_outbox(now=NOW + 20 * MIN) == 0 and _persisted(svc) == []
    assert mnt.deliver_outbox(now=NOW + 31 * MIN) == 1 and len(_persisted(svc)) == 1


def test_rolled_back_state_change_leaves_no_outbox_row(svc, clock):
    from app.model.models import MonitorRecord, MonitorState

    _reported(svc, clock)
    session = svc._rc_session()
    try:
        svc._store_heartbeat(session, session.get(MonitorRecord, "host:b"), MonitorState.UP, 1.0, None, "active_probe")
        session.rollback()
    finally:
        session.close()
    assert _outbox(svc) == [] and _recovered(svc) == []


def test_scheduled_job_delivers_the_outbox_even_when_nothing_is_due(svc, clock, monkeypatch):
    import asyncio

    outage_id = _reported(svc, clock)
    with monkeypatch.context() as died:
        died.setattr(svc.maintenance, "flush_session_events", lambda session, now=None: 0)
        _up(svc, "host:b")
    svc._probing = True
    clock["now"] = NOW + 25 * MIN
    asyncio.run(svc.evaluate_outages())  # emits from a worker thread, marshalled onto the loop
    assert [r["outage_id"] for r in _recovered(svc)] == [outage_id] and _outbox(svc) == []


def test_permanently_failing_event_is_dropped_after_the_attempt_limit(svc, clock):
    mid = _host(svc, "host:x", ci_id=HOST_CI)
    _down(svc, mid)
    _failing_emit(svc, 10 ** 6)
    mnt = svc.maintenance
    mnt.OUTBOX_MAX_ATTEMPTS = 3
    mnt.evaluate_outages(now=NOW + 10 * MIN)
    for minute in (12, 14, 16):
        mnt.deliver_outbox(now=NOW + minute * MIN)
    assert _outbox(svc) == [] and _persisted(svc) == []
