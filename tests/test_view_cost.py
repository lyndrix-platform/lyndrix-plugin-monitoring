"""MO-13: the timeline aggregate is computed once per span, not once per view variant."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "_perf_base_vc", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor, _beat = _base._monitor, _base._beat


def test_hours_snap_up_to_the_allowed_spans(svc):
    norm = svc.normalize_timeline_hours
    assert [norm(h) for h in (1, 24, 25, 72, 100, 168, 9999, "abc", None)] == [24, 24, 72, 72, 168, 168, 168, 24, 24]


def test_overview_group_by_variants_share_one_timeline_aggregate(svc, monkeypatch):
    from app.controller import service as service_mod

    _beat(svc, _monitor(svc, "host:a"), "UP", 1.0)
    calls = []
    real = service_mod._hourly_state_counts

    def counting(session, ids, hours):
        calls.append(hours)
        return real(session, ids, hours)

    monkeypatch.setattr(service_mod, "_hourly_state_counts", counting)

    async def run():
        for group_by in ("site", "stage", "type"):
            await svc.overview(group_by=group_by, hours=24)
        await svc.overview(group_by="site", hours=24, include_paused=False)
        await svc.overview(group_by="site", hours=5)  # snaps to 24 → same aggregate

    asyncio.run(run())
    assert calls == [24]


def test_each_allowed_span_is_its_own_aggregate_and_the_overview_reports_it(svc):
    _beat(svc, _monitor(svc, "host:a"), "UP", 1.0)

    async def run():
        return [await svc.overview(hours=h) for h in (24, 50, 168)]

    views = asyncio.run(run())
    assert [v["hours"] for v in views] == [24, 72, 168]
    assert [len(v["rows"][0]["timeline"]) for v in views] == [24, 72, 168]


def test_nicegui_page_does_not_load_raw_heartbeats():
    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "ui"
    for name in ("page.py", "overview.py"):
        assert "get_histories" not in (root / name).read_text(), name
