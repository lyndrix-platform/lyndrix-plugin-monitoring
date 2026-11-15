"""MO-18: a long destructive job keeps the cluster-wide quiesce alive."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "_perf_base_qr", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture


def _quiesce_emits(svc):
    return [p for t, p, *_ in svc.ctx.emits if t == svc.QUIESCE_TOPIC]


def test_quiesce_is_renewed_while_the_job_runs_and_released_after(svc, monkeypatch):
    monkeypatch.setattr(svc, "QUIESCE_TTL_SECONDS", 0.2)  # renewal every 0.1 s
    monkeypatch.setattr(svc, "QUIESCE_GRACE_SECONDS", 0)
    svc.ctx.emits.clear()

    async def run():
        async with svc.quiesced([]):
            await asyncio.sleep(0.55)
            during = list(_quiesce_emits(svc))
            deadline_ok = svc.is_quiesced
        return during, deadline_ok

    during, still_quiesced = asyncio.run(run())
    assert still_quiesced
    assert sum(1 for p in during if p.get("on")) >= 4  # initial + several renewals
    after = _quiesce_emits(svc)
    assert after[-1] == {"on": False}
    assert not svc.is_quiesced


def test_no_renewal_task_outlives_the_job(svc, monkeypatch):
    monkeypatch.setattr(svc, "QUIESCE_GRACE_SECONDS", 0)

    async def run():
        async with svc.quiesced([]):
            pass
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t.get_name() == "monitoring:quiesce_renew"]

    assert asyncio.run(run()) == []
