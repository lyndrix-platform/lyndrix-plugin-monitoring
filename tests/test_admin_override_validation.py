"""MO-16: admin-override `update` applies the same bounds and types as an upsert."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "_perf_base_ao", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor = _base._monitor


def _update(svc, mid, **patch):
    from app.model.models import AdminOverride

    return svc.apply_admin_override(AdminOverride(monitor_id=mid, action="update", patch=patch))


@pytest.mark.parametrize("patch", [
    {"interval_seconds": 1},
    {"interval_seconds": "abc"},
    {"timeout_seconds": 0},
    {"timeout_seconds": 600},
    {"enabled": "maybe"},
    {"name": None},
])
def test_invalid_patch_is_rejected_and_nothing_is_stored(svc, patch):
    mid = _monitor(svc)
    before = svc.get_monitor(mid)
    with pytest.raises(ValueError, match="Invalid patch"):
        _update(svc, mid, **patch)
    after = svc.get_monitor(mid)
    for key in ("interval_seconds", "timeout_seconds", "enabled", "name"):
        assert after[key] == before[key]


def test_valid_patch_is_stored_coerced_to_the_declared_types(svc):
    mid = _monitor(svc)
    item = _update(svc, mid, interval_seconds="30", timeout_seconds=5, name="renamed", unknown_key="ignored")
    assert item["interval_seconds"] == 30 and isinstance(item["interval_seconds"], int)
    assert item["timeout_seconds"] == 5 and item["name"] == "renamed"


def test_out_of_range_value_already_stored_does_not_block_an_unrelated_patch(svc):
    from app.model.models import MonitorRecord

    mid = _monitor(svc)
    with svc._session() as s:
        s.get(MonitorRecord, mid).interval_seconds = 5  # written before the bounds existed
        s.commit()
    assert _update(svc, mid, name="still patchable")["name"] == "still patchable"


def test_api_answers_400(svc):
    import asyncio

    from fastapi import HTTPException

    from app.controller.api import build_plugin_router
    from app.model.models import AdminOverride

    router = build_plugin_router(svc)
    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/admin-override"))
    mid = _monitor(svc)
    with pytest.raises(HTTPException) as err:
        asyncio.run(route.endpoint(AdminOverride(monitor_id=mid, action="update", patch={"interval_seconds": 1}), identity=None))
    assert err.value.status_code == 400
