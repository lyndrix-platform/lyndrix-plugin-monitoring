"""MO-19 — the Python timeline helper buckets naive UTC heartbeat timestamps
as UTC, whatever the host time zone is (it used to read them as local time)."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import time
from datetime import datetime, timedelta, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


@pytest.fixture(params=["UTC", "Europe/Berlin", "America/New_York", "Asia/Kolkata"])
def host_tz(request):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_naive_utc_history_is_bucketed_as_utc_in_every_host_zone(host_tz, monkeypatch):
    import app.ui.timeline as timeline_mod

    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(timeline_mod, "_utc_now", lambda: now)
    history = [
        {"state": "DOWN", "timestamp": "2026-07-01T08:50:00"},        # naive UTC, as the DB returns it
        {"state": "UP", "timestamp": "2026-07-01T11:30:00+00:00"},    # explicit offset
        {"state": "UP", "timestamp": "2026-07-01T13:45:00+02:00"},    # = 11:45 UTC
        {"state": "PAUSED", "timestamp": "2026-07-01T09:10:00Z"},
        {"state": "UP", "timestamp": "not a timestamp"},
    ]
    tl = timeline_mod.timeline_from_history(history, 24)
    assert len(tl) == 24
    assert tl[-4] == "DOWN" and tl[-3] == "PAUSED" and tl[-1] == "UP"
    assert tl.count("UNKNOWN") == 21


def test_sql_and_python_timelines_agree_in_every_host_zone(host_tz, svc):
    """The baseline test, pinned to explicit zones (it failed wherever TZ != UTC)."""
    from app.model.models import _utc_now
    from app.ui.timeline import timeline_from_history

    mid = _monitor(svc)
    now = _utc_now()
    _beat(svc, mid, "UP", at=now - timedelta(minutes=30))
    _beat(svc, mid, "DOWN", at=now - timedelta(hours=3, minutes=10))
    _beat(svc, mid, "UP", at=now - timedelta(hours=30))
    tl = svc.get_timelines([mid], 24)[mid]
    assert tl[-1] == "UP" and tl[-4] == "DOWN" and tl.count("UNKNOWN") == 22
    assert tl == timeline_from_history(svc.get_history(mid, 24), 24)
    assert tl == timeline_from_history(svc.get_histories([mid], 24)[mid], 24)


def test_history_timestamps_carry_the_utc_offset(svc):
    mid = _monitor(svc)
    _beat(svc, mid, "UP", 1.0)
    stamp = svc.get_history(mid)[0]["timestamp"]
    assert stamp.endswith("+00:00") and datetime.fromisoformat(stamp).tzinfo is not None
    assert svc.get_histories([mid])[mid][0]["timestamp"] == stamp
