"""Outage rule (1.7.0): the 60 s evaluator persists an outage once the effective
downtime (after the last window end) reaches ``offline_minutes``, emits
``monitoring:outage_persisted`` exactly once and ``monitoring:outage_recovered``
after the commit of the recovering heartbeat. Fake clock via monkeypatch.
Real service against in-memory SQLite; run inside lyndrix-core-dev."""

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
_monitor = _base._monitor


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
SVC_CI = "22222222-2222-4222-8222-222222222222"
MIN = timedelta(minutes=1)


@pytest.fixture()
def clock(monkeypatch):
    import app.controller.service as service_mod
    import app.model.models as models_mod
    from core.api import maintenance as mw

    state = {"now": NOW}
    monkeypatch.setattr(service_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(models_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(mw, "utcnow", lambda: state["now"])
    return state


def _down(svc, mid, error="connection refused"):
    from app.model.models import MonitorState

    svc._save_probe_result(mid, MonitorState.DOWN, None, error)


def _up(svc, mid):
    from app.model.models import MonitorState

    svc._save_probe_result(mid, MonitorState.UP, 3.0, None)


def _publish(svc, *items, source="iac-orchestrator"):
    from core.api import maintenance as mw

    svc.maintenance.on_windows_published(
        {
            "source": source,
            "published_at": mw.iso(NOW),
            "replaces": True,
            "items": list(items),
        }
    )


def _item(ci_id, kind="host", windows=(), **extra):
    return {
        "ci_id": ci_id,
        "kind": kind,
        "name": f"ci-{kind}",
        "windows": list(windows),
        "inherited_from": None,
        **extra,
    }


def _once(start, end):
    from core.api import maintenance as mw

    return {"type": "once", "start": mw.iso(start), "end": mw.iso(end)}


def _persisted(svc):
    return [p for t, p in svc.ctx.events if t == "monitoring:outage_persisted"]


def _recovered(svc):
    return [p for t, p in svc.ctx.events if t == "monitoring:outage_recovered"]


def _service(svc, mid, host, ci_id=None, host_ci_id=None):
    from app.model.models import MonitorType, MonitorUpsert

    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id=mid,
            name=mid,
            monitor_type=MonitorType.HTTP,
            target="http://10.0.0.1",
            host_name=host,
            service_name=mid.split(":")[-1],
            ci_id=ci_id,
            host_ci_id=host_ci_id,
        ),
        sync_scheduler=False,
    )
    return mid


# ── persistence after offline_minutes ────────────────────────────────────────


def test_persists_after_offline_minutes_outside_any_window(svc, clock):
    mid = _host(svc, "host:a", ci_id=HOST_CI)
    _down(svc, mid)
    mnt = svc.maintenance
    assert (
        mnt.evaluate_outages(now=NOW + 5 * MIN)["persisted"] == 0
        and _persisted(svc) == []
    )
    stats = mnt.evaluate_outages(now=NOW + 10 * MIN)
    assert stats["persisted"] == 1 and len(_persisted(svc)) == 1

    p = _persisted(svc)[0]
    assert set(p) == {
        "outage_id",
        "ci_id",
        "kind",
        "name",
        "host_ci_id",
        "monitor_id",
        "since",
        "persisted_at",
        "duration_s",
        "rule",
        "error",
    }
    assert p["outage_id"] == f"{HOST_CI}:2026-10-20T12:00:00Z" and p["ci_id"] == HOST_CI
    assert (
        p["kind"] == "host"
        and p["name"] == "host:a"
        and p["host_ci_id"] is None
        and p["monitor_id"] == mid
    )
    assert (
        p["since"] == "2026-10-20T12:00:00Z"
        and p["persisted_at"] == "2026-10-20T12:10:00Z"
    )
    assert p["duration_s"] == 600 and p["error"] == "connection refused"
    assert p["rule"] == {
        "offline_minutes": 10,
        "severity": "high",
        "auto_resolve": True,
    }
    # emitted locally — never bridged
    assert [c for t, _, c in svc.ctx.emits if t == "monitoring:outage_persisted"] == [
        False
    ]

    out = mnt.list_outages()
    assert (
        out[0]["state"] == "persisted"
        and out[0]["outage_id"] == p["outage_id"]
        and out[0]["rule"]["offline_minutes"] == 10
    )


def test_not_inside_window_and_clock_restarts_after_window_end(svc, clock):
    mid = _host(svc, "host:b", ci_id=HOST_CI)
    _publish(svc, _item(HOST_CI, windows=[_once(NOW - 60 * MIN, NOW + 60 * MIN)]))
    _down(svc, mid)  # stored as MAINTENANCE, clock row suppressed until 13:00
    mnt = svc.maintenance
    assert mnt.evaluate_outages(now=NOW + 30 * MIN)["suppressed"] == 1
    # 5 min after the window ended: effective downtime = 5 min < 10 → nothing yet
    assert mnt.evaluate_outages(now=NOW + 65 * MIN)["persisted"] == 0
    # 10 min after the window ended → reported; `since` is the real start of the outage
    assert mnt.evaluate_outages(now=NOW + 70 * MIN)["persisted"] == 1
    p = _persisted(svc)[0]
    assert p["since"] == "2026-10-20T12:00:00Z" and p["duration_s"] == 70 * 60
    assert (
        mnt.list_outages()
        and mnt.list_outages()[0]["effective_since"] == "2026-10-20T13:00:00Z"
    )


def test_override_published_later_extends_the_suppression(svc, clock):
    from core.api import maintenance as mw

    mid = _host(svc, "host:c", ci_id=HOST_CI)
    _down(svc, mid)  # outside any window at 12:00
    mnt = svc.maintenance
    # a deploy starts at 12:05 → override until 12:45 → the evaluator extends suppressed_until
    mnt.on_override_started(
        {
            "override_id": "job-1",
            "ci_ids": [HOST_CI],
            "until": mw.iso(NOW + 45 * MIN),
            "source": "iac-orchestrator",
            "started_at": mw.iso(NOW + 5 * MIN),
            "linger_minutes": 15,
        }
    )
    assert (
        mnt.evaluate_outages(now=NOW + 10 * MIN)["suppressed"] == 1
        and _persisted(svc) == []
    )
    assert (
        mnt.evaluate_outages(now=NOW + 50 * MIN)["persisted"] == 0
    )  # 5 min after the override
    assert mnt.evaluate_outages(now=NOW + 55 * MIN)["persisted"] == 1


# ── rule cascade ─────────────────────────────────────────────────────────────


def test_rule_cascade_service_ci_then_host_ci_then_settings(svc, clock):
    _host(svc, "host:h", ci_id=HOST_CI)
    own = _service(svc, "service:h:own", "h", ci_id=SVC_CI, host_ci_id=HOST_CI)
    inherit = _service(
        svc,
        "service:h:inherit",
        "h",
        ci_id="44444444-4444-4444-8444-444444444444",
        host_ci_id=HOST_CI,
    )
    _host(svc, "host:o")  # a host nobody published a rule for
    default = _service(
        svc, "service:o:default", "o", ci_id="55555555-5555-4555-8555-555555555555"
    )
    _publish(
        svc,
        _item(
            HOST_CI,
            incidents={
                "enabled": True,
                "offline_minutes": 3,
                "severity": "medium",
                "auto_resolve": False,
            },
        ),
        _item(
            SVC_CI,
            kind="service",
            incidents={"enabled": True, "offline_minutes": 1, "severity": "critical"},
        ),
        _item("44444444-4444-4444-8444-444444444444", kind="service"),
    )
    for mid in (own, inherit, default):
        _down(svc, mid)
    mnt = svc.maintenance
    mnt.evaluate_outages(now=NOW + 1 * MIN)
    assert [p["monitor_id"] for p in _persisted(svc)] == [own]
    p = _persisted(svc)[0]
    assert p["kind"] == "service" and p["host_ci_id"] == HOST_CI
    assert p["rule"] == {
        "offline_minutes": 1,
        "severity": "critical",
        "auto_resolve": True,
    }
    mnt.evaluate_outages(now=NOW + 3 * MIN)
    assert [p["monitor_id"] for p in _persisted(svc)] == [own, inherit]
    assert _persisted(svc)[1]["rule"] == {
        "offline_minutes": 3,
        "severity": "medium",
        "auto_resolve": False,
    }
    mnt.evaluate_outages(now=NOW + 10 * MIN)
    assert [p["monitor_id"] for p in _persisted(svc)] == [own, inherit, default]
    assert _persisted(svc)[2]["rule"]["offline_minutes"] == 10  # settings default


def test_rule_disabled_or_zero_minutes_means_no_event(svc, clock):
    mid = _host(svc, "host:z", ci_id=HOST_CI)
    _publish(svc, _item(HOST_CI, incidents={"enabled": False}))
    _down(svc, mid)
    assert (
        svc.maintenance.evaluate_outages(now=NOW + 60 * MIN)["skipped"] == 1
        and _persisted(svc) == []
    )
    _publish(svc, _item(HOST_CI, incidents={"enabled": True, "offline_minutes": 0}))
    assert (
        svc.maintenance.evaluate_outages(now=NOW + 60 * MIN)["skipped"] == 1
        and _persisted(svc) == []
    )


# ── emergency switches per kind ───────────────────────────────────────────────


def test_kind_switches_and_disabled_kinds(svc, clock):
    host = _host(svc, "host:k", ci_id=HOST_CI)
    net = _host(svc, "host:net", ci_id="66666666-6666-4666-8666-666666666666")
    gen = _host(svc, "host:gen", ci_id="77777777-7777-4777-8777-777777777777")
    _publish(
        svc,
        _item(HOST_CI),
        _item("66666666-6666-4666-8666-666666666666", kind="network"),
        _item("77777777-7777-4777-8777-777777777777", kind="generic"),
    )
    svc.apply_settings(
        {"outage_rules_enabled_host": "false", "outage_rules_disabled_kinds": "network"}
    )
    for mid in (host, net, gen):
        _down(svc, mid)
    stats = svc.maintenance.evaluate_outages(now=NOW + 15 * MIN)
    assert stats["skipped"] == 2 and [p["monitor_id"] for p in _persisted(svc)] == [
        gen
    ]  # unknown kind without entry → reported
    svc.apply_settings({"outage_rules_enabled": "false"})
    _host(svc, "host:k2", ci_id="88888888-8888-4888-8888-888888888888")
    _down(svc, "host:k2")
    svc.maintenance.evaluate_outages(now=NOW + 30 * MIN)
    assert len(_persisted(svc)) == 1  # master switch off → nothing new


# ── identity ─────────────────────────────────────────────────────────────────


def test_without_ci_id_nothing_is_emitted(svc, clock):
    mid = _host(svc, "host:noci")
    _down(svc, mid)
    stats = svc.maintenance.evaluate_outages(now=NOW + 30 * MIN)
    assert stats["skipped"] == 1 and _persisted(svc) == []
    svc.maintenance.evaluate_outages(now=NOW + 31 * MIN)  # logged once, still no event
    assert _persisted(svc) == [] and "host:noci" in svc.maintenance._warned_no_ci
    # the clock row still exists — the outage shows in the UI
    assert svc.maintenance.list_outages()[0]["monitor_id"] == mid


def test_service_without_own_ci_reports_under_the_host_ci(svc, clock):
    _host(svc, "host:s", ci_id=HOST_CI)
    mid = _service(svc, "service:s:web", "s")
    svc.maintenance.reload()
    _down(svc, mid)
    svc.maintenance.evaluate_outages(now=NOW + 10 * MIN)
    p = _persisted(svc)[0]
    # kind: nothing published for the CI → derived from the monitor type (plan §3)
    assert p["ci_id"] == HOST_CI and p["host_ci_id"] is None and p["kind"] == "service"


# ── exactly once ─────────────────────────────────────────────────────────────


def test_double_evaluation_emits_exactly_once(svc, clock):
    mid = _host(svc, "host:once", ci_id=HOST_CI)
    _down(svc, mid)
    mnt = svc.maintenance
    mnt.evaluate_outages(now=NOW + 10 * MIN)
    mnt.evaluate_outages(now=NOW + 10 * MIN)
    mnt.evaluate_outages(now=NOW + 20 * MIN)
    assert len(_persisted(svc)) == 1
    # a second DOWN after the report keeps the row persisted (no second clock)
    clock["now"] = NOW + 21 * MIN
    _down(svc, mid)
    assert mnt.list_outages()[0]["state"] == "persisted"
    assert svc.get_monitor(mid)["outage_state"] == "persisted"


def test_conditional_update_loser_does_not_emit(svc, clock):
    """Two instances evaluate the same open row: the one whose UPDATE … WHERE
    state='open' hits 0 rows (the peer won) stays silent."""
    from app.model.models import OutageRecord

    mid = _host(svc, "host:race", ci_id=HOST_CI)
    _down(svc, mid)
    mnt = svc.maintenance
    real_retrying = svc._retrying

    def peer_wins_first(session, fn, *a, **kw):
        # Simulate the peer's commit landing right before our conditional UPDATE.
        with svc._session() as s:
            s.query(OutageRecord).filter(OutageRecord.monitor_id == mid).update(
                {OutageRecord.state: "persisted", OutageRecord.outage_id: "peer"},
                synchronize_session=False,
            )
            s.commit()
        return real_retrying(session, fn, *a, **kw)

    svc._retrying = peer_wins_first
    try:
        assert mnt.evaluate_outages(now=NOW + 10 * MIN)["persisted"] == 0
    finally:
        svc._retrying = real_retrying
    assert _persisted(svc) == []


# ── recovery ─────────────────────────────────────────────────────────────────


def test_recovery_emits_after_commit_with_duration(svc, clock):
    from app.model.models import OutageRecord

    mid = _host(svc, "host:rec", ci_id=HOST_CI)
    _down(svc, mid)
    svc.maintenance.evaluate_outages(now=NOW + 10 * MIN)
    outage_id = _persisted(svc)[0]["outage_id"]

    clock["now"] = NOW + 25 * MIN
    _up(svc, mid)
    rec = _recovered(svc)
    assert len(rec) == 1
    assert rec[0] == {
        "outage_id": outage_id,
        "ci_id": HOST_CI,
        "since": "2026-10-20T12:00:00Z",
        "recovered_at": "2026-10-20T12:25:00Z",
        "duration_s": 25 * 60,
    }
    assert [c for t, _, c in svc.ctx.emits if t == "monitoring:outage_recovered"] == [
        False
    ]
    with svc._session() as s:
        assert s.get(OutageRecord, mid) is None  # row gone after the recovery
    assert svc.maintenance.list_outages() == []

    # a new outage after recovery gets its own clock and id
    clock["now"] = NOW + 40 * MIN
    _down(svc, mid)
    svc.maintenance.evaluate_outages(now=NOW + 50 * MIN)
    assert _persisted(svc)[-1]["outage_id"] == f"{HOST_CI}:2026-10-20T12:40:00Z"


def test_recovery_event_is_queued_until_commit(svc, clock):
    """The event sits on the session until the caller committed — a rolled-back
    heartbeat never produces a phantom recovery."""
    from app.model.models import MonitorRecord, MonitorState

    mid = _host(svc, "host:q", ci_id=HOST_CI)
    _down(svc, mid)
    svc.maintenance.evaluate_outages(now=NOW + 10 * MIN)
    session = svc._rc_session()
    try:
        record = session.get(MonitorRecord, mid)
        svc._store_heartbeat(
            session, record, MonitorState.UP, 1.0, None, "active_probe"
        )
        assert _recovered(svc) == [] and len(session.info["monitoring_events"]) == 1
        session.rollback()
    finally:
        session.close()
    assert _recovered(svc) == []
    assert (
        svc.maintenance.list_outages()[0]["state"] == "persisted"
    )  # still open for business


# ── evaluator wiring ─────────────────────────────────────────────────────────


def test_outage_job_is_registered_on_the_prober_only(svc):
    import asyncio

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.05)
        ids = {j.id for j in svc.scheduler.get_jobs()}
        await svc.stop_probing()
        return ids

    assert "monitoring:outages" in asyncio.run(run())
    assert asyncio.run(svc.evaluate_outages()) == {}  # standby: no evaluation


def test_evaluator_skips_rows_of_monitors_it_does_not_own(svc, clock):
    mid = _host(svc, "host:shard", ci_id=HOST_CI)
    _down(svc, mid)
    svc._probe_mode = "sharded"
    svc._shard = set()  # another worker owns this monitor …
    svc._shard_universe = {mid}  # … and it is known to the shard
    assert svc.maintenance.evaluate_outages(now=NOW + 30 * MIN)["checked"] == 0
    svc._shard_universe = (
        set()
    )  # orphan after a shard change → housekeeping worker takes it
    assert svc.maintenance.evaluate_outages(now=NOW + 30 * MIN)["checked"] == 1
