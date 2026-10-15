"""Cluster-readiness fixes (v1.5.0): inventory-sync resync, off-loop job sync,
scheduler resilience, idempotent daily aggregates, probe_roles, quiesce and
probe-all forwarding. Real service against in-memory SQLite; run inside
lyndrix-core-dev like the other suites.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


def _changed(svc):
    return [p for t, p in svc.ctx.events if t == "monitoring:monitors_changed"]


# ── #1 inventory-sync announces and schedules ────────────────────────────────

def test_inventory_sync_route_resyncs_and_notifies(svc):
    from app.model.models import InventorySyncPayload

    payload = InventorySyncPayload(
        owner_source="orchestrator_service", source_revision="r1",
        hosts=[{"host_name": "docker-hydra", "address": "10.0.0.5"}], services=[],
    )

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.3)
        stats = await svc.ingest_inventory(payload)
        assert stats["upserts"] == 1
        assert any(j.id == "monitor:host:docker-hydra" for j in svc.scheduler.get_jobs())
        await svc.stop_probing()

    asyncio.run(run())
    assert _changed(svc)[-1]["reason"] == "inventory" and _changed(svc)[-1]["count"] == 1


# ── #2 / #13 off-loop writes ──────────────────────────────────────────────────

def test_off_loop_upsert_schedules_job_and_notifies(svc):
    from app.model.models import MonitorType, MonitorUpsert

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.3)
        # The API/bus path: upsert in a worker thread with sync_scheduler=True.
        await asyncio.to_thread(svc.upsert_monitor, MonitorUpsert(
            monitor_id="host:thread", name="host:thread", monitor_type=MonitorType.PING,
            target="10.0.0.7", address="10.0.0.7", host_name="host:thread"))
        await asyncio.sleep(0.05)  # call_soon_threadsafe hop
        assert any(j.id == "monitor:host:thread" for j in svc.scheduler.get_jobs())
        await svc.stop_probing()

    asyncio.run(run())
    assert _changed(svc)[-1]["reason"] == "upsert"


def test_off_loop_sync_job_without_loop_does_not_raise(svc):
    svc._scheduler_started = True  # no loop known at all
    rec = SimpleNamespace(monitor_id="x", enabled=True, latest_state="UP", monitor_type="ping",
                          target="1.2.3.4", address=None, interval_seconds=60)
    svc._sync_job_for_monitor(rec)  # must not raise "no running event loop"
    assert svc.scheduler.get_jobs() == []


def test_admin_override_notifies(svc):
    from app.model.models import AdminOverride

    mid = _monitor(svc)
    svc.apply_admin_override(AdminOverride(monitor_id=mid, action="pause"))
    assert _changed(svc)[-1]["reason"] == "override"


def test_transition_emit_from_worker_thread_lands_on_loop(svc):
    from app.model.models import MonitorState

    mid = _monitor(svc)

    async def run():
        svc._remember_loop()
        await asyncio.to_thread(svc._save_probe_result, mid, MonitorState.UP, 3.0, None)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert any(t == "monitoring:state_changed" for t, _ in svc.ctx.events)


# ── #3 scheduler resilience ──────────────────────────────────────────────────

def test_scheduler_job_survives_exceptions():
    from app.controller.scheduler import SimpleAsyncScheduler

    calls = []
    logged = []
    log = SimpleNamespace(warning=lambda msg: logged.append(msg))

    async def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("db down")

    async def run():
        sched = SimpleAsyncScheduler(log=log)
        sched._failure_backoff = lambda failures: 0  # keep the test fast
        sched.add_interval_job(flaky, seconds=1, id="t", initial_delay=1)
        await asyncio.sleep(3.6)
        alive = sched.get_job("t") is not None
        sched.shutdown()
        return alive

    assert asyncio.run(run()) is True
    assert len(calls) >= 3 and len(logged) == 2


def test_failure_backoff_grows_and_caps():
    from app.controller.scheduler import SimpleAsyncScheduler

    s = SimpleAsyncScheduler()
    assert s._failure_backoff(0) == 0 and s._failure_backoff(1) == 0
    assert s._failure_backoff(2) == 2 and s._failure_backoff(3) == 4
    assert s._failure_backoff(50) == s.FAILURE_BACKOFF_CAP


# ── #10 daily aggregate idempotency ──────────────────────────────────────────

def test_daily_aggregate_is_one_row_per_day(svc):
    from app.model.models import MonitorDailyAggregate

    mid = _monitor(svc)
    for latency in (10.0, 20.0, None, 30.0):
        _beat(svc, mid, "UP", latency)
    _beat(svc, mid, "DOWN")
    with svc._session() as s:
        rows = s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == mid).all()
    assert len(rows) == 1
    agg = rows[0]
    assert (agg.total_samples, agg.up_samples, agg.down_samples) == (5, 4, 1)
    assert agg.latency_samples == 3 and agg.avg_latency_ms == 20.0


def test_daily_aggregate_insert_race_falls_back_to_update(svc, monkeypatch):
    """The portable path: a concurrent writer inserted today's row between our
    select and insert → the unique key fires, we increment the winner's row."""
    from app.model.models import MonitorDailyAggregate, MonitorState

    mid = _monitor(svc)
    _beat(svc, mid, "UP", 5.0)  # creates today's row
    real = type(svc)._get_daily_aggregate
    calls = {"n": 0}

    def first_miss(session, monitor_id, day):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(session, monitor_id, day)

    monkeypatch.setattr(type(svc), "_get_daily_aggregate", staticmethod(first_miss))
    svc._save_probe_result(mid, MonitorState.UP, 15.0, None)
    with svc._session() as s:
        rows = s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == mid).all()
    assert len(rows) == 1 and rows[0].total_samples == 2 and rows[0].avg_latency_ms == 10.0


def test_migration_dedupes_and_adds_unique_key(svc):
    from sqlalchemy import inspect, text
    from core.api import db_instance
    from app.model.models import MonitorDailyAggregate, _day_start, _utc_now

    engine = db_instance.engine
    with engine.begin() as conn:
        conn.execute(text(f"DROP INDEX {svc.DAILY_AGG_UNIQUE}"))
    day = _day_start(_utc_now())
    with svc._session() as s:
        s.add_all([
            MonitorDailyAggregate(monitor_id="m", day=day, total_samples=2, up_samples=2, down_samples=0,
                                  paused_samples=0, avg_latency_ms=10.0, latency_samples=2),
            MonitorDailyAggregate(monitor_id="m", day=day, total_samples=3, up_samples=1, down_samples=2,
                                  paused_samples=0, avg_latency_ms=40.0, latency_samples=1),
        ])
        s.commit()
    svc.ensure_tables()
    svc.ensure_tables()  # idempotent
    with svc._session() as s:
        rows = s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == "m").all()
    assert len(rows) == 1
    r = rows[0]
    assert (r.total_samples, r.up_samples, r.down_samples, r.latency_samples) == (5, 3, 2, 3)
    assert r.avg_latency_ms == 20.0
    names = {ix["name"] for ix in inspect(engine).get_indexes("monitoring_daily_aggregates")}
    assert svc.DAILY_AGG_UNIQUE in names


# ── #9 rollups survive and stay correct in chunks ────────────────────────────

def test_rollups_are_chunked(svc, monkeypatch):
    monkeypatch.setattr(type(svc), "ROLLUP_CHUNK", 2)
    ids = [_monitor(svc, f"host:r{i}") for i in range(5)]
    for mid in ids:
        _beat(svc, mid, "UP", 1.0)
        _beat(svc, mid, "DOWN")
    svc._refresh_rollups_sync()
    rows = {m["monitor_id"]: m for m in svc.list_monitors()}
    assert all(rows[mid]["uptime_24h"] == 50.0 for mid in ids)


# ── #8 clear_states quiesces, batches and notifies ───────────────────────────

def test_clear_states_resets_and_notifies(svc):
    mid = _monitor(svc)
    _beat(svc, mid, "UP", 2.0)
    svc.QUIESCE_GRACE_SECONDS = 0  # standby path without the real grace period

    async def run():
        return await svc.clear_states()

    assert asyncio.run(run()) == 1
    assert svc.get_monitor(mid)["latest_state"] == "UNKNOWN"
    topics = [(t, p.get("on", p.get("reason"))) for t, p in svc.ctx.events
              if t in ("monitoring:quiesce", "monitoring:monitors_changed")]
    assert topics == [("monitoring:quiesce", True), ("monitoring:quiesce", False),
                      ("monitoring:monitors_changed", "maintenance")]
    assert svc.is_quiesced is False


# ── #7 quiesce from a peer ───────────────────────────────────────────────────

def test_peer_quiesce_pauses_probes(svc):
    mid = _monitor(svc)
    svc.on_quiesce({"on": True})  # own emit → ignored
    assert svc.is_quiesced is False
    svc.on_quiesce({"on": True, "ttl": 60, "_origin": "core-b"})
    assert svc.is_quiesced is True

    async def run():
        await svc.run_scheduled_probe(mid)  # skipped, no write

    asyncio.run(run())
    assert svc.get_monitor(mid)["latest_state"] == "UNKNOWN"
    svc.on_quiesce({"on": False, "_origin": "core-b"})
    assert svc.is_quiesced is False


# ── #6 probe-all forwarding ──────────────────────────────────────────────────

def test_probe_all_on_standby_forwards_to_prober(svc):
    svc.request_full_probe_cluster()
    assert svc._force_full_sweep is False  # a standby never sweeps itself
    assert _changed(svc)[-1]["reason"] == "probe_all"
    # The prober receives the bridged event and arms the full sweep.
    svc._probing = True
    svc.on_monitors_changed({"reason": "probe_all", "_origin": "core-a"})
    assert svc._force_full_sweep is True


# ── #11 / #12 settings and probe_roles ───────────────────────────────────────

class _LeaseCtx(_base._FakeCtx):
    def __init__(self, roles, settings=None):
        super().__init__()
        self.roles = roles
        self.wanted = []
        self._settings = settings or {}

    def on_lease(self, name, *, acquired=None, lost=None, ttl=None):
        self.wanted.append(name)

    def has_live_role(self, role):
        return role in self.roles

    def get_all_settings(self):
        return dict(self._settings)


def _svc_with(svc, ctx):
    svc.ctx = ctx
    return svc


def test_probe_roles_gate_the_lease(svc):
    s = _svc_with(svc, _LeaseCtx(["api", "leader"], {"probe_roles": "runner"}))
    asyncio.run(s.init_placement())
    assert s.ctx.wanted == [] and s.may_probe() is False


def test_probe_roles_match_contends(svc):
    s = _svc_with(svc, _LeaseCtx(["api", "runner"], {"probe_roles": " runner , edge"}))
    asyncio.run(s.init_placement())
    assert s.ctx.wanted == ["monitoring:prober"]
    s.claim_prober_lease()  # idempotent
    assert s.ctx.wanted == ["monitoring:prober"]


def test_empty_probe_roles_means_any_instance(svc):
    s = _svc_with(svc, _LeaseCtx(["api"], {}))
    asyncio.run(s.init_placement())
    assert s.ctx.wanted == ["monitoring:prober"]


def test_settings_changed_reapplies_and_widens_roles(svc):
    ctx = _LeaseCtx(["api", "runner"], {"probe_roles": "edge"})
    s = _svc_with(svc, ctx)
    asyncio.run(s.init_placement())
    assert ctx.wanted == []
    ctx._settings = {"probe_roles": "runner", "heartbeat_retention_days": "3"}
    asyncio.run(s.reload_settings())  # what the plugin:settings_changed handler calls
    assert s._heartbeat_retention_days == 3 and ctx.wanted == ["monitoring:prober"]


class _ReleaseCtx(_LeaseCtx):
    """Core API >= 1.9: the context can give a wanted lease up again."""

    def __init__(self, roles, settings=None):
        super().__init__(roles, settings)
        self.released = []
        self._lost = None

    def on_lease(self, name, *, acquired=None, lost=None, ttl=None):
        super().on_lease(name, acquired=acquired, lost=lost, ttl=ttl)
        self._lost = lost

    def release_lease(self, name):
        self.released.append(name)
        self.wanted.remove(name)
        if self._lost is not None:  # the core fires lost for a held lease
            res = self._lost()
            if asyncio.iscoroutine(res):
                asyncio.get_event_loop().run_until_complete(res) if not asyncio.get_event_loop().is_running() else asyncio.ensure_future(res)
        return True


def test_settings_changed_narrows_roles_and_releases_the_lease(svc):
    ctx = _ReleaseCtx(["api", "leader"], {"probe_roles": ""})
    s = _svc_with(svc, ctx)
    asyncio.run(s.init_placement())
    assert ctx.wanted == ["monitoring:prober"] and s._lease_wanted

    async def run():
        ctx._settings = {"probe_roles": "runner"}  # this instance has no runner role
        await s.reload_settings()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert ctx.released == ["monitoring:prober"] and ctx.wanted == [] and not s._lease_wanted
    # widened again → contends again
    ctx._settings = {"probe_roles": ""}
    asyncio.run(s.reload_settings())
    assert ctx.wanted == ["monitoring:prober"] and s._lease_wanted


def test_settings_changed_narrows_roles_on_old_core_only_warns(svc):
    ctx = _LeaseCtx(["api"], {"probe_roles": ""})   # no release_lease
    s = _svc_with(svc, ctx)
    asyncio.run(s.init_placement())
    ctx._settings = {"probe_roles": "runner"}
    asyncio.run(s.reload_settings())
    assert ctx.wanted == ["monitoring:prober"] and s._lease_wanted  # kept until reload


# ── #14 stop_probing leaves the API usable ───────────────────────────────────

def test_stop_probing_resets_stopping(svc):
    async def run():
        svc.start_probing()
        await asyncio.sleep(0.1)
        await svc.stop_probing()

    asyncio.run(run())
    assert svc._stopping is False
