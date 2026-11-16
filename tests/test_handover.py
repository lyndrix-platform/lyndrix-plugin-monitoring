"""MO-14 (warm-up blocks only for the genuine first sweep) and MO-20 (a lease
loss stops probing, nothing else; stale probe results are fenced off)."""

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


def _sweep_statuses(svc, monkeypatch):
    async def fake_probe(mid, force=False):
        await asyncio.sleep(0)

    monkeypatch.setattr(svc, "run_scheduled_probe", fake_probe)
    svc._probing = True
    svc.ctx.statuses.clear()

    async def run():
        await svc._run_probe_refresh_batch()
        if svc._startup_prune_task:
            svc._startup_prune_task.cancel()

    asyncio.run(run())
    return [kw.get("blocking") for phase, kw in svc.ctx.statuses if phase == "starting"]


def _make_stale(svc, mid):
    from app.model.models import MonitorRecord, _utc_now

    with svc._session() as s:
        s.get(MonitorRecord, mid).last_checked_at = _utc_now() - timedelta(hours=1)
        s.commit()


# ── MO-14 ────────────────────────────────────────────────────────────────────


def test_first_sweep_of_an_unprobed_fleet_blocks(svc, monkeypatch):
    for i in range(3):
        _monitor(svc, f"host:h{i}")
    blocking = _sweep_statuses(svc, monkeypatch)
    assert blocking and all(b is True for b in blocking)


def test_sweep_after_a_lease_handover_does_not_block(svc, monkeypatch):
    """The previous prober died: every state is stale, but there IS data to show."""
    for i in range(3):
        mid = _monitor(svc, f"host:h{i}")
        _beat(svc, mid, "UP", 1.0)
        _make_stale(svc, mid)
    blocking = _sweep_statuses(svc, monkeypatch)
    assert blocking and all(b is False for b in blocking)


def test_inventory_import_adding_monitors_does_not_block(svc, monkeypatch):
    for i in range(3):
        _beat(svc, _monitor(svc, f"host:h{i}"), "UP", 1.0)
    _monitor(svc, "host:new")  # UNKNOWN, never probed
    blocking = _sweep_statuses(svc, monkeypatch)
    assert blocking and all(b is False for b in blocking)


def test_reset_states_is_a_genuine_first_sweep_again(svc, monkeypatch):
    for i in range(3):
        _beat(svc, _monitor(svc, f"host:h{i}"), "UP", 1.0)
    svc.clear_states_db()
    blocking = _sweep_statuses(svc, monkeypatch)
    assert blocking and all(b is True for b in blocking)


# ── MO-20 ────────────────────────────────────────────────────────────────────


def test_lease_loss_keeps_a_running_inventory_import_and_api_status(svc):
    async def run():
        svc.start_probing()
        await asyncio.sleep(0.05)
        finished = []

        async def slow_import():
            await asyncio.sleep(0.3)
            finished.append("import")

        async def reload_cache():
            await asyncio.sleep(0.3)
            finished.append("reload")

        svc._inventory_sync_task = asyncio.create_task(slow_import(), name="monitoring:inventory_sync")
        reload_task = asyncio.create_task(reload_cache(), name="monitoring:maintenance_reload")
        svc.ctx.set_status("maintenance", message="Deleting statistics", blocking=True)  # an API job here
        svc.ctx.statuses.clear()
        jobs_before = len(svc.scheduler.get_jobs())

        await svc.stop_probing()

        jobs_after = len(svc.scheduler.get_jobs())
        await asyncio.gather(svc._inventory_sync_task, reload_task)
        return finished, jobs_before, jobs_after, list(svc.ctx.statuses)

    finished, jobs_before, jobs_after, statuses = asyncio.run(run())
    assert sorted(finished) == ["import", "reload"]  # neither was cancelled
    assert jobs_before > 0 and jobs_after == 0  # probing itself did stop
    assert statuses == []  # the API job's blocking maintenance status was not lifted
    assert svc._stopping is False and svc.is_prober is False


def test_lease_loss_does_not_wait_for_inflight_probes(svc, monkeypatch):
    mid = _monitor(svc)
    release = asyncio.Event

    async def run():
        gate = release()

        async def slow_dispatch(rd):
            await gate.wait()
            from app.model.models import MonitorState

            return MonitorState.DOWN, None, "written by the old prober"

        monkeypatch.setattr(svc, "_dispatch_probe", slow_dispatch)
        svc.start_probing()
        await asyncio.sleep(0.05)
        probe = asyncio.create_task(svc.run_scheduled_probe(mid, force=True))
        await asyncio.sleep(0.05)
        assert mid in svc._inflight_probes
        loop = asyncio.get_running_loop()
        started = loop.time()
        await svc.stop_probing()
        waited = loop.time() - started
        gate.set()
        result = await probe
        return waited, result

    waited, result = asyncio.run(run())
    assert waited < 1.5  # no 5 s drain inside the bounded `lost` callback
    assert result is None  # the late result was discarded …
    item = svc.get_monitor(mid)
    assert item["latest_state"] == "UNKNOWN" and svc.get_history(mid) == []  # … and never stored


def test_probe_result_is_rejected_when_the_core_lease_token_changed(svc, monkeypatch):
    from app.model.models import MonitorState

    mid = _monitor(svc)
    token = {"value": 7}
    asked = []

    def lease_token(name):
        asked.append(name)
        return token["value"]

    svc.ctx.lease_token = lease_token

    async def dispatch(rd):
        if token.get("flip"):
            token["value"] = 8  # a peer took the lease while this probe was running
        return MonitorState.UP, 1.0, None

    monkeypatch.setattr(svc, "_dispatch_probe", dispatch)
    svc._probing = True
    assert asyncio.run(svc.run_scheduled_probe(mid)) == MonitorState.UP
    assert asked and set(asked) == {"monitoring:prober"} and len(svc.get_history(mid)) == 1
    token["flip"] = True
    assert asyncio.run(svc.run_scheduled_probe(mid)) is None
    assert len(svc.get_history(mid)) == 1  # the stale write was rejected

    # cores without ctx.lease_token: unchanged behaviour
    del svc.ctx.lease_token
    assert asyncio.run(svc.run_scheduled_probe(mid)) == MonitorState.UP


def test_full_stop_still_cancels_everything_and_reports_ready(svc):
    async def run():
        svc.start_probing()
        await asyncio.sleep(0.05)
        task = asyncio.create_task(asyncio.sleep(30), name="monitoring:inventory_sync")
        svc._inventory_sync_task = task
        await svc.stop()
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(run()) is True
    assert svc.ctx.statuses[-1][0] == "ready" and svc._stopping is True
