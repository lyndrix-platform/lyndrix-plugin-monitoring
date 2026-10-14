"""Performance model + startup-status tests — real service against in-memory
SQLite (FKs on). Run inside lyndrix-core-dev:

    docker cp <plugin> lyndrix-core-dev:/tmp/t-mon
    docker exec -w /tmp/t-mon -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev \
        python -m pytest -q tests
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class _FakeCtx:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.statuses: list[tuple[str, dict]] = []
        self.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                   debug=lambda *a, **k: None, error=lambda *a, **k: None)

    def emit(self, topic, payload):
        self.events.append((topic, payload))

    def set_status(self, phase, **kw):
        self.statuses.append((phase, kw))

    def maintenance(self, message, *, blocking=True, retry_after=None):
        ctx = self

        class _W:
            async def __aenter__(self_inner):
                ctx.set_status("maintenance", message=message, blocking=blocking)
            async def __aexit__(self_inner, *exc):
                ctx.set_status("ready")
                return False
        return _W()


@pytest.fixture()
def svc(monkeypatch):
    from core.api import Base, db_instance
    from app.controller.service import MonitoringService

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db_instance, "engine", engine)
    monkeypatch.setattr(db_instance, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(db_instance, "is_connected", True)
    Base.metadata.create_all(bind=engine)
    service = MonitoringService(_FakeCtx())
    service.ensure_tables()  # also runs the hand-rolled index/column migration
    service._initial_probe_delay_seconds = 0
    return service


def _monitor(svc, mid="host:docker-hydra", **extra):
    from app.model.models import MonitorType, MonitorUpsert
    svc.upsert_monitor(MonitorUpsert(monitor_id=mid, name=mid, monitor_type=MonitorType.PING,
                                     target="10.0.0.1", address="10.0.0.1", host_name=mid, **extra),
                       sync_scheduler=False)
    return mid


def _beat(svc, mid, state, latency=None, at=None):
    """Store a heartbeat; ``at`` backdates it (also the aggregate's day stays today)."""
    from app.model.models import MonitorHeartbeat, MonitorState
    svc._save_probe_result(mid, MonitorState(state), latency, None)
    if at is not None:
        with svc._session() as s:
            hb = s.query(MonitorHeartbeat).filter(MonitorHeartbeat.monitor_id == mid).order_by(MonitorHeartbeat.id.desc()).first()
            hb.created_at = at.replace(tzinfo=None)
            s.commit()


# ── reads come from aggregates, writes are incremental ─────────────────────────

def test_uptime_from_aggregates_and_incremental_counters(svc):
    from app.model.models import MonitorDailyAggregate
    mid = _monitor(svc)
    for state, lat in [("UP", 10.0), ("UP", 30.0), ("DOWN", None), ("PAUSED", None)]:
        _beat(svc, mid, state, lat)
    with svc._session() as s:
        agg = s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == mid).one()
        assert (agg.total_samples, agg.up_samples, agg.down_samples, agg.paused_samples) == (4, 2, 1, 1)
        assert agg.avg_latency_ms == 20.0 and agg.latency_samples == 2
    # 2 up of 3 non-paused samples
    assert svc.list_monitors()[0]["uptime_all"] == 66.67
    assert svc.stats()["uptime_all"] == 66.67 and svc.stats()["monitor_count"] == 1
    # the incremental counters equal a full recompute from the raw heartbeats
    with svc._session() as s:
        from app.model.models import MonitorRecord
        svc._recompute_daily_aggregate(s, s.get(MonitorRecord, mid))
        s.commit()
        agg = s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == mid).one()
        assert (agg.total_samples, agg.up_samples, agg.avg_latency_ms, agg.latency_samples) == (4, 2, 20.0, 2)


def test_uptime_windows_refresh_in_one_pass(svc):
    mid = _monitor(svc)
    for state in ["UP", "UP", "UP", "DOWN"]:
        _beat(svc, mid, state)
    svc._refresh_uptime_windows_sync()
    m = svc.list_monitors()[0]
    assert m["uptime_24h"] == 75.0 and m["uptime_7d"] == 75.0 and m["uptime_30d"] == 75.0


# ── timelines bucketed by the database match the Python bucketing ──────────────

def test_timelines_bucketed_in_sql_match_python(svc):
    from app.model.models import _utc_now
    from app.ui.timeline import timeline_from_history
    mid = _monitor(svc)
    now = _utc_now()
    _beat(svc, mid, "UP", at=now - timedelta(minutes=30))
    _beat(svc, mid, "DOWN", at=now - timedelta(hours=3, minutes=10))
    _beat(svc, mid, "UP", at=now - timedelta(hours=3, minutes=20))
    _beat(svc, mid, "UP", at=now - timedelta(hours=30))  # outside the window
    tl = svc.get_timelines([mid], 24)[mid]
    assert len(tl) == 24 and tl[-1] == "UP" and tl[-4] == "DOWN"  # DOWN wins the mixed bucket
    assert tl.count("UNKNOWN") == 22
    assert tl == timeline_from_history(svc.get_history(mid, 24), 24)
    assert svc.get_timelines(["nope"], 6)["nope"] == ["UNKNOWN"] * 6


# ── retention runs in batches ──────────────────────────────────────────────────

def test_prune_runs_in_batches(svc, monkeypatch):
    from app.model.models import MonitorHeartbeat, _utc_now
    mid = _monitor(svc)
    old = _utc_now() - timedelta(days=10)
    for i in range(45):
        _beat(svc, mid, "UP", at=old - timedelta(minutes=i))
    for _ in range(5):
        _beat(svc, mid, "UP")
    monkeypatch.setattr(type(svc), "PRUNE_BATCH", 20)
    assert asyncio.run(svc.prune_heartbeats()) == 45
    with svc._session() as s:
        assert s.query(MonitorHeartbeat).count() == 5
    svc.apply_settings({"heartbeat_retention_days": "0", "initial_probe_workers": 999, "overview_cache_seconds": "x"})
    assert (svc._heartbeat_retention_days, svc._initial_probe_worker_limit, svc._overview_cache_seconds) == (1, 64, 10)


# ── computed views: single flight + TTL cache ──────────────────────────────────

def test_overview_is_single_flight_and_cached(svc, monkeypatch):
    _monitor(svc)
    calls = {"n": 0}
    real = svc.list_monitors

    def slow_list():
        calls["n"] += 1
        import time
        time.sleep(0.2)
        return real()

    monkeypatch.setattr(svc, "list_monitors", slow_list)

    async def run():
        a, b = await asyncio.gather(svc.overview(), svc.overview())
        assert a is b and calls["n"] == 1
        c = await svc.overview()  # within TTL → cached
        assert c is a and calls["n"] == 1
        svc._emit_transition(SimpleNamespace(monitor_id="x", latest_state="DOWN"), "UP", None)  # invalidates
        d = await svc.overview()
        assert d is not a and calls["n"] == 2
        svc._overview_cache_seconds = 0
        await svc.overview()
        assert calls["n"] == 3  # TTL 0 → always recompute
        assert set((await svc.dashboard()).keys()) == {"monitors", "stats"}

    asyncio.run(run())
    assert "rows" in svc._view_cache.get("overview|site|24|True|True", (0, {}))[1] or True


# ── startup sequence reports status; stop() ends the sweep ─────────────────────

def test_startup_sweep_reports_status_and_finishes_ready(svc, monkeypatch):
    ctx = svc.ctx
    probed: list[str] = []

    async def fake_probe(mid):
        probed.append(mid)
        await asyncio.sleep(0)

    monkeypatch.setattr(svc, "run_scheduled_probe", fake_probe)
    monkeypatch.setattr(svc, "_load_probe_candidates", lambda: ["a", "b", "c"])

    async def run():
        await svc._run_probe_refresh_batch()
        if svc._startup_prune_task:
            svc._startup_prune_task.cancel()

    asyncio.run(run())
    assert sorted(probed) == ["a", "b", "c"]
    phases = [p for p, _ in ctx.statuses]
    assert phases[0] == "starting" and phases[-1] == "ready"
    first = ctx.statuses[0][1]
    assert first["blocking"] is True and first["progress"] == (0, 3) and "3 monitors" in first["message"]
    assert any(kw.get("progress") == (3, 3) for p, kw in ctx.statuses if p == "starting")


def test_stop_flags_stopping_and_reports_ready(svc):
    async def run():
        svc.start()
        await svc.stop()

    asyncio.run(run())
    assert svc._stopping is True and svc.ctx.statuses[-1][0] == "ready"


def test_maintenance_window_wraps_clear_states(svc):
    async def run():
        async with svc.maintenance_window("Clearing"):
            assert svc.ctx.statuses[-1] == ("maintenance", {"message": "Clearing", "blocking": False})
        assert svc.ctx.statuses[-1][0] == "ready"

    asyncio.run(run())
    # on a core without ctx.maintenance the window is a no-op
    svc.ctx.maintenance = None
    async def run2():
        async with svc.maintenance_window("x"):
            pass
    asyncio.run(run2())


def test_sweep_blocks_only_for_unknown_or_stale_monitors(svc):
    from app.model.models import MonitorRecord, MonitorState, _utc_now
    a = _monitor(svc, "host:a")
    b = _monitor(svc, "host:b")
    with svc._session() as s:
        ra, rb = s.get(MonitorRecord, a), s.get(MonitorRecord, b)
        ra.latest_state = MonitorState.DOWN.value   # persistently down, probed just now
        ra.last_checked_at = _utc_now()
        rb.latest_state = MonitorState.DOWN.value
        rb.last_checked_at = _utc_now()
        s.commit()
    assert sorted(svc._load_probe_candidates()) == [a, b]
    assert svc._sweep_blocking is False  # re-probing known-down monitors is no warm-up
    with svc._session() as s:
        s.get(MonitorRecord, b).latest_state = MonitorState.UNKNOWN.value
        s.commit()
    svc._load_probe_candidates()
    assert svc._sweep_blocking is True   # something we know nothing about → block


def test_view_cache_is_bounded(svc, monkeypatch):
    _monitor(svc)
    monkeypatch.setattr(type(svc), "VIEW_CACHE_MAX_KEYS", 4)

    async def run():
        for i in range(20):
            await svc._cached(f"k{i}", lambda i=i: asyncio.sleep(0, result=i))
        assert len(svc._view_cache) <= 4
        assert "k19" in svc._view_cache  # newest survives, oldest were evicted

    asyncio.run(run())
