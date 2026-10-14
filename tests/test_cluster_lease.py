"""Cluster placement: only the instance holding the ``monitoring:prober`` lease
runs scheduler/sweeps; every instance serves the API. Run inside
lyndrix-core-dev like the other suites.
"""
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
_monitor = _base._monitor


def test_fresh_service_is_standby_until_the_lease_arrives(svc):
    assert svc.is_prober is False
    assert svc._scheduler_started is False
    svc.enqueue_background_probe_refresh()
    assert svc._background_probe_task is None  # standby never sweeps


def test_start_probing_is_idempotent_and_stop_probing_releases(svc):
    async def run():
        svc.start_probing()
        svc.start_probing()
        assert svc.is_prober is True and svc._scheduler_started is True
        assert svc._bootstrap_task is not None
        await asyncio.sleep(0.3)  # bootstrap (tables, jobs, rollups, sweep) runs
        await svc.stop_probing()
        assert svc.is_prober is False and svc._scheduler_started is False
        await svc.stop_probing()  # no-op
        # Re-acquire: http client was closed by stop() and must come back.
        svc.start_probing()
        assert svc.is_prober is True and not svc.http_client.is_closed
        await svc.stop_probing()

    asyncio.run(run())


def test_standby_bootstrap_only_prepares_tables(svc):
    async def run():
        svc.queue_bootstrap()
        await asyncio.sleep(0.3)
        assert svc._background_probe_task is None and svc._scheduler_started is False
        svc.request_resync()  # standby ignores resync requests
        assert svc._resync_task is None

    asyncio.run(run())


def test_monitor_changes_are_announced_to_peers(svc):
    from app.model.models import MonitorType, MonitorUpsert

    async def run():
        svc.start_probing()
        await asyncio.sleep(0.3)
        # A single upsert through the write API (sync_scheduler=True) announces
        # the change; the fake ctx has no ``cluster`` kwarg → the old-core
        # fallback path is exercised too.
        svc.upsert_monitor(MonitorUpsert(monitor_id="host:new", name="host:new", monitor_type=MonitorType.PING,
                                         target="10.0.0.9", address="10.0.0.9", host_name="host:new"))
        await svc.stop_probing()

    asyncio.run(run())
    announced = [p for t, p in svc.ctx.events if t == "monitoring:monitors_changed"]
    assert announced and announced[-1]["reason"] == "upsert" and announced[-1]["count"] == 1
    svc._notify_monitors_changed("maintenance", count=3)
    assert svc.ctx.events[-1] == ("monitoring:monitors_changed", {"reason": "maintenance", "count": 3, "instance_id": None})


def test_prober_resync_reloads_jobs(svc):
    async def run():
        svc.start_probing()
        await asyncio.sleep(0.3)
        _monitor(svc, "host:late-joiner")
        svc.request_resync()
        assert svc._resync_task is not None
        await asyncio.sleep(0.5)
        assert any(j.id == "monitor:host:late-joiner" for j in svc.scheduler.get_jobs())
        await svc.stop_probing()

    asyncio.run(run())
