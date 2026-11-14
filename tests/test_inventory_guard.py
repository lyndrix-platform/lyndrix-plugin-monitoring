"""MO-15 — the inventory import never wipes the managed monitors because an
inventory arrived empty or implausibly small, and the removal of monitors that
really left the inventory is batched, retried and runs with the probe writers
stopped. Real service against in-memory SQLite."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_beat = _base._beat


def _payload(hosts, services=(), **extra):
    from app.model.models import InventorySyncPayload

    return InventorySyncPayload(
        owner_source="orchestrator_service",
        hosts=[{"host_name": h, "address": "10.0.0.1"} for h in hosts],
        services=[{"host_name": h, "service_name": s, "url": "http://10.0.0.1/"} for h, s in services],
        **extra,
    )


def _ids(svc):
    return {m["monitor_id"] for m in svc.list_monitors()}


def _fleet(svc, n=10):
    hosts = [f"h{i:02d}" for i in range(n)]
    svc.ingest_inventory_snapshot(_payload(hosts))
    assert len(_ids(svc)) == n
    return hosts


def _capture_notifications(svc):
    sent = []
    svc.ctx.notify = lambda endpoint, payload=None, **kw: sent.append((endpoint, payload, kw))
    return sent


def test_empty_inventory_deletes_nothing_and_notifies(svc):
    hosts = _fleet(svc)
    _beat(svc, "host:h00", "UP", 1.0)
    sent = _capture_notifications(svc)
    warnings = []
    svc.ctx.log.warning = lambda msg, *a, **k: warnings.append(msg)

    stats = svc.ingest_inventory_snapshot(_payload([]))

    assert stats["stale_deleted"] == 0 and stats["stale_skipped"] == len(hosts)
    assert "empty" in stats["stale_guard"]
    assert len(_ids(svc)) == len(hosts) and svc.get_history("host:h00")  # history kept
    assert sent and sent[0][0] == "inventory_guard" and sent[0][2]["severity"] == "warning"
    assert sent[0][2]["notification_id"] == "monitoring:inventory_guard"
    assert any("inventory is empty" in w for w in warnings)


def test_inventory_without_hosts_is_refused(svc):
    _fleet(svc, 3)
    stats = svc.ingest_inventory_snapshot(_payload([], services=[("h00", "web")]))
    assert stats["stale_skipped"] == 3 and "no hosts" in stats["stale_guard"]
    assert {"host:h00", "host:h01", "host:h02"} <= _ids(svc)


def test_shrink_beyond_threshold_needs_explicit_confirmation(svc):
    hosts = _fleet(svc, 20)
    keep = hosts[:10]  # 50 % would disappear, default limit 30 %
    stats = svc.ingest_inventory_snapshot(_payload(keep))
    assert stats["stale_deleted"] == 0 and stats["stale_skipped"] == 10
    assert "10 of 20" in stats["stale_guard"] and len(_ids(svc)) == 20

    stats = svc.ingest_inventory_snapshot(_payload(keep, allow_mass_delete=True))
    assert stats["stale_deleted"] == 10 and stats["stale_skipped"] == 0 and "stale_guard" not in stats
    assert _ids(svc) == {f"host:{h}" for h in keep}


def test_plausible_removal_is_deleted_with_history(svc):
    from app.model.models import MonitorDailyAggregate, MonitorHeartbeat, OutageRecord

    hosts = _fleet(svc, 20)
    for state in ("UP", "DOWN", "DOWN"):
        _beat(svc, "host:h19", state)
    stats = svc.ingest_inventory_snapshot(_payload(hosts[:-2]))  # 10 % removed
    assert stats["stale_deleted"] == 2 and stats["stale_skipped"] == 0
    assert "host:h19" not in _ids(svc) and len(_ids(svc)) == 18
    with svc._session() as s:
        assert s.query(MonitorHeartbeat).filter(MonitorHeartbeat.monitor_id == "host:h19").count() == 0
        assert s.query(MonitorDailyAggregate).filter(MonitorDailyAggregate.monitor_id == "host:h19").count() == 0
        assert s.get(OutageRecord, "host:h19") is None


def test_small_fleet_cleanup_is_not_blocked_by_the_percentage(svc):
    _fleet(svc, 4)
    stats = svc.ingest_inventory_snapshot(_payload(["h00", "h01"]))  # 50 %, but only 2 monitors
    assert stats["stale_deleted"] == 2


def test_threshold_setting_zero_and_hundred(svc):
    hosts = _fleet(svc, 10)
    svc.apply_settings({"inventory_max_stale_percent": "0"})
    assert svc._inventory_max_stale_percent == 0
    stats = svc.ingest_inventory_snapshot(_payload(hosts[:-1]))
    assert stats["stale_deleted"] == 0 and stats["stale_skipped"] == 1  # nothing without confirmation

    svc.apply_settings({"inventory_max_stale_percent": 100})
    stats = svc.ingest_inventory_snapshot(_payload(hosts[:1]))
    assert stats["stale_deleted"] == 9
    assert svc.ingest_inventory_snapshot(_payload([]))["stale_skipped"] == 1  # empty is always refused

    svc.apply_settings({})
    assert svc._inventory_max_stale_percent == 30  # default


def test_manual_monitors_are_never_part_of_the_stale_set(svc):
    from app.model.models import MonitorType, MonitorUpsert

    hosts = _fleet(svc, 10)
    svc.upsert_monitor(MonitorUpsert(monitor_id="host:manual", name="m", monitor_type=MonitorType.PING,
                                     target="10.0.0.9"), sync_scheduler=False)
    stats = svc.ingest_inventory_snapshot(_payload(hosts[:-1]))
    assert stats["stale_deleted"] == 1 and "host:manual" in _ids(svc)


def test_stale_delete_is_batched_and_retries_write_conflicts(svc):
    """Heartbeats go in PRUNE_BATCH statements, monitors in DELETE_CHUNK
    transactions, and a MariaDB 1020 from a concurrent probe write is retried."""
    hosts = _fleet(svc, 20)
    for _ in range(7):
        _beat(svc, "host:h19", "UP", 1.0)
        _beat(svc, "host:h18", "UP", 1.0)
    svc.PRUNE_BATCH = 3
    svc.DELETE_CHUNK = 1

    class _Conflict(Exception):
        orig = type("E", (), {"args": (1020, "Record has changed since last read")})()

    real, calls = svc._retrying, {"n": 0, "failed": 0}

    def flaky(session, fn, *a, **kw):
        def attempt():
            calls["n"] += 1
            if calls["n"] in (2, 5):  # a straggling probe write hits two of the batches
                calls["failed"] += 1
                raise _Conflict()
            return fn()

        return real(session, attempt, *a, **kw)

    svc._retrying = flaky
    try:
        stats = svc.ingest_inventory_snapshot(_payload(hosts[:-2]))
    finally:
        svc._retrying = real
    assert stats["stale_deleted"] == 2 and calls["failed"] == 2
    assert calls["n"] >= 2 * (3 + 1)  # ≥ 3 heartbeat batches + 1 monitor transaction per monitor
    assert svc.get_history("host:h19") == [] and len(_ids(svc)) == 18


def test_loop_driven_import_quiesces_the_probe_writers_of_removed_monitors(svc):
    hosts = _fleet(svc, 20)
    svc.QUIESCE_GRACE_SECONDS = 0

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.4)  # bootstrap loads the jobs
        assert svc.scheduler.get_job("monitor:host:h19") is not None
        seen = {}
        real = svc.delete_monitor_ids

        def spy(ids):
            # Runs in the worker thread while the writers are stopped.
            seen["quiesced"] = svc.is_quiesced
            seen["job"] = svc.scheduler._jobs.get("monitor:host:h19")
            return real(ids)

        svc.delete_monitor_ids = spy
        stats = await svc.ingest_inventory(_payload(hosts[:-1]))
        jobs = {j.id for j in svc.scheduler.get_jobs()}
        await svc.stop_probing()
        return stats, seen, jobs

    stats, seen, jobs = asyncio.run(run())
    assert stats["stale_deleted"] == 1 and "stale_ids" not in stats
    assert seen == {"quiesced": True, "job": None}
    assert "monitor:host:h19" not in jobs and "monitor:host:h00" in jobs
    quiesce = [p for t, p in svc.ctx.events if t == "monitoring:quiesce"]
    assert [p["on"] for p in quiesce] == [True, False] and not svc.is_quiesced
