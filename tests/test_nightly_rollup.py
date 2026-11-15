"""MO-21: the 00:05 rollup heals the day that just ended, not the new one."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
from datetime import timedelta

_spec = importlib.util.spec_from_file_location(
    "_perf_base_nr", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


def _agg_for(svc, mid, days_back):
    from app.controller.service import _day_start
    from app.model.models import MonitorDailyAggregate, _utc_now

    day = _day_start(_utc_now() - timedelta(days=days_back))
    with svc._session() as s:
        row = svc._get_daily_aggregate(s, mid, day)
        return None if row is None else (row.total_samples, row.up_samples, row.down_samples)


def test_nightly_rollup_recomputes_yesterday_and_leaves_today_alone(svc):
    from app.model.models import _utc_now

    mid = _monitor(svc)
    yesterday_noon = (_utc_now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    _beat(svc, mid, "UP", 1.0, at=yesterday_noon)
    _beat(svc, mid, "UP", 1.0, at=yesterday_noon + timedelta(minutes=1))
    _beat(svc, mid, "DOWN", None, at=yesterday_noon + timedelta(minutes=2))
    # the incremental counters booked all three on today's row; yesterday has none
    assert _agg_for(svc, mid, 1) is None
    today_before = _agg_for(svc, mid, 0)
    assert today_before == (3, 2, 1)

    asyncio.run(svc._nightly_rollup())

    assert _agg_for(svc, mid, 1) == (3, 2, 1)  # the finished day was rebuilt from the heartbeats
    assert _agg_for(svc, mid, 0) == today_before  # the new day was not rewritten


def test_default_rebuild_still_recomputes_today(svc):
    mid = _monitor(svc)
    _beat(svc, mid, "UP", 1.0)
    with svc._session() as s:  # corrupt today's counters
        from app.controller.service import _day_start
        from app.model.models import _utc_now

        row = svc._get_daily_aggregate(s, mid, _day_start(_utc_now()))
        row.total_samples, row.up_samples = 99, 99
        s.commit()
    svc.rebuild_statistics()
    assert _agg_for(svc, mid, 0) == (1, 1, 0)
