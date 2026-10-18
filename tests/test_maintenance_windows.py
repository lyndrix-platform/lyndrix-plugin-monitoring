"""Maintenance windows and overrides (1.7.0): bus handlers, store semantics,
cache reload, manual windows/overrides from the API. Real service against
in-memory SQLite; run inside lyndrix-core-dev like the other suites.

(``tests/test_maintenance.py`` covers the plugin's own housekeeping actions.)"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor = _base._monitor


def _host(svc, mid, ci_id=None, host_name=None, **extra):
    """A real host monitor (type ``server``, ``host_name`` = the host) — the shape
    the inventory sync produces; ``_monitor`` makes PING monitors."""
    from app.model.models import MonitorType, MonitorUpsert

    hn = host_name or mid.split(":", 1)[-1]
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id=mid,
            name=mid,
            monitor_type=MonitorType.SERVER,
            target="10.0.0.1",
            address="10.0.0.1",
            host_name=hn,
            ci_id=ci_id,
            **extra,
        ),
        sync_scheduler=False,
    )
    return mid


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
HOST_CI = "11111111-1111-4111-8111-111111111111"
OTHER_CI = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Freeze every clock (service, models, core.api.maintenance) at ``clock["now"]``."""
    import app.controller.service as service_mod
    import app.model.models as models_mod
    from core.api import maintenance as mw

    state = {"now": NOW}
    monkeypatch.setattr(service_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(models_mod, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(mw, "utcnow", lambda: state["now"])
    return state


def _once(start: datetime, end: datetime, note=None) -> dict:
    from core.api import maintenance as mw

    w = {"type": "once", "start": mw.iso(start), "end": mw.iso(end)}
    if note:
        w["note"] = note
    return w


def _item(ci_id, windows, kind="host", **extra) -> dict:
    return {
        "ci_id": ci_id,
        "kind": kind,
        "name": f"ci-{ci_id[:4]}",
        "windows": windows,
        "inherited_from": None,
        **extra,
    }


def _published(source, *items, replaces=True) -> dict:
    from core.api import maintenance as mw

    return {
        "source": source,
        "published_at": mw.iso(NOW),
        "replaces": replaces,
        "items": list(items),
    }


def _rows(svc, source=None):
    return svc.maintenance.store.list_windows(source=source)


def _changed(svc):
    return [(p, c) for t, p, c in svc.ctx.emits if t == "monitoring:monitors_changed"]


# ── windows_published ─────────────────────────────────────────────────────────


def test_windows_published_replaces_per_source(svc):
    mid = _host(svc, "host:a", ci_id=HOST_CI)
    mnt = svc.maintenance
    active = _once(NOW - timedelta(hours=1), NOW + timedelta(hours=1))

    assert (
        mnt.on_windows_published(
            _published(
                "iac-orchestrator", _item(HOST_CI, [active]), _item(OTHER_CI, [active])
            )
        )
        == 2
    )
    assert (
        mnt.on_windows_published(
            _published("docker-manager", _item(OTHER_CI, [active], kind="container"))
        )
        == 1
    )
    assert len(_rows(svc)) == 3

    # replaces: true → only the publishing source's rows are swapped
    assert (
        mnt.on_windows_published(
            _published("iac-orchestrator", _item(HOST_CI, [active]))
        )
        == 1
    )
    sources = sorted((r["source"], r["ci_id"]) for r in _rows(svc))
    assert sources == [("docker-manager", OTHER_CI), ("iac-orchestrator", HOST_CI)]

    # the cache sees it at once; the record resolves via its ci_id
    with svc._session() as s:
        from app.model.models import MonitorRecord

        rec = s.get(MonitorRecord, mid)
        state = mnt.cache.state_for(rec, NOW)
    assert (
        state.active
        and state.source == "window"
        and state.until == NOW + timedelta(hours=1)
    )
    assert mnt.cache.kind_by_ci(OTHER_CI) == "container"

    # peers are told with OUR cluster broadcast, the maintenance event itself stays local
    payload, cluster = _changed(svc)[-1]
    assert payload["reason"] == "maintenance_windows" and cluster is True
    assert all(c is False for t, _, c in svc.ctx.emits if t.startswith("maintenance:"))


def test_windows_published_skips_bad_items_and_keeps_the_rest(svc):
    mnt = svc.maintenance
    good = _item(HOST_CI, [_once(NOW, NOW + timedelta(hours=1))])
    no_ci = {"kind": "host", "name": "x", "windows": []}
    bad_kind = _item(OTHER_CI, [], kind="Not A Kind")
    bad_window = _item(
        OTHER_CI, [{"type": "weekly", "days": [], "start": "02:00", "end": "04:00"}]
    )
    with_rule = _item(
        OTHER_CI,
        [],
        incidents={"enabled": True, "offline_minutes": 3, "severity": "critical"},
    )
    assert (
        mnt.on_windows_published(
            _published("src", good, no_ci, bad_kind, bad_window, with_rule)
        )
        == 2
    )
    rows = {r["ci_id"]: r for r in _rows(svc)}
    assert set(rows) == {HOST_CI, OTHER_CI}
    assert rows[OTHER_CI]["incidents"] == {
        "enabled": True,
        "offline_minutes": 3,
        "severity": "critical",
        "auto_resolve": True,
    }
    # any valid slug is accepted — kinds are open (decision A)
    assert (
        mnt.on_windows_published(
            _published("src2", _item(OTHER_CI, [], kind="network_segment"))
        )
        == 1
    )


def test_windows_published_without_replaces_upserts(svc):
    mnt = svc.maintenance
    mnt.on_windows_published(_published("src", _item(HOST_CI, []), _item(OTHER_CI, [])))
    mnt.on_windows_published(
        _published(
            "src",
            _item(HOST_CI, [_once(NOW, NOW + timedelta(hours=1))]),
            replaces=False,
        )
    )
    rows = {r["ci_id"]: r for r in _rows(svc, "src")}
    assert set(rows) == {HOST_CI, OTHER_CI} and len(rows[HOST_CI]["windows"]) == 1


def test_own_publish_is_ignored_by_the_handler(svc):
    mnt = svc.maintenance
    assert mnt.on_windows_published(_published("monitoring", _item(HOST_CI, []))) == 0
    assert _rows(svc) == []
    assert (
        mnt.on_windows_published({"items": [_item(HOST_CI, [])]}) == 0
    )  # no source → ignored


# ── overrides ────────────────────────────────────────────────────────────────


def test_override_start_end_linger_and_expiry(svc):
    from core.api import maintenance as mw

    mid = _host(svc, "host:b", ci_id=HOST_CI)
    mnt = svc.maintenance
    started = {
        "override_id": "job-42",
        "ci_ids": [HOST_CI],
        "until": mw.iso(NOW + timedelta(hours=4)),
        "reason": "deploy aac-grafana",
        "source": "iac-orchestrator",
        "started_at": mw.iso(NOW),
        "linger_minutes": 15,
    }
    assert mnt.on_override_started(started) is True
    assert mnt.on_override_started(started) is True  # idempotent replay

    with svc._session() as s:
        from app.model.models import MonitorRecord

        rec = s.get(MonitorRecord, mid)
    assert mnt.cache.state_for(rec, NOW).active
    assert (
        mnt.cache.state_for(rec, NOW - timedelta(minutes=1)).active is False
    )  # not before started_at
    st = mnt.cache.state_for(rec, NOW + timedelta(minutes=5))
    assert (
        st.source == "override"
        and st.until == NOW + timedelta(hours=4)
        and st.note == "deploy aac-grafana"
    )

    ended_at = NOW + timedelta(minutes=10)
    assert (
        mnt.on_override_ended(
            {
                "override_id": "job-42",
                "source": "iac-orchestrator",
                "ended_at": mw.iso(ended_at),
                "linger_minutes": 15,
            }
        )
        is True
    )
    assert mnt.cache.state_for(rec, ended_at + timedelta(minutes=14)).active  # lingers
    assert mnt.cache.state_for(rec, ended_at + timedelta(minutes=16)).active is False
    assert mnt.on_override_ended({"override_id": "nope", "source": "x"}) is False

    listed = mnt.list_overrides()["items"]
    assert (
        listed[0]["until_effective"] == mw.iso(ended_at + timedelta(minutes=15))
        and listed[0]["own"] is False
    )
    assert (
        mnt.store.purge_expired(ended_at + timedelta(hours=1)) == 0
    )  # within the 1-day grace
    assert mnt.store.purge_expired(ended_at + timedelta(days=2)) == 1
    assert mnt.list_overrides()["items"] == []


def test_override_payload_without_required_fields_raises(svc):
    from core.api import maintenance as mw

    with pytest.raises(mw.MaintenanceError):
        svc.maintenance.on_override_started({"ci_ids": [HOST_CI], "source": "x"})


# ── cache invalidation ───────────────────────────────────────────────────────


def test_peer_change_reloads_the_cache_without_a_resync(svc):
    mid = _host(svc, "host:c", ci_id=HOST_CI)
    mnt = svc.maintenance
    # a peer wrote the row (shared DB) — this instance's cache does not know it yet
    mnt.store.replace_windows(
        "peer",
        [
            {
                "ci_id": HOST_CI,
                "kind": "host",
                "name": "c",
                "windows": [_once(NOW - timedelta(hours=1), NOW + timedelta(hours=1))],
            }
        ],
    )
    with svc._session() as s:
        from app.model.models import MonitorRecord

        rec = s.get(MonitorRecord, mid)
    assert mnt.cache.state_for(rec, NOW).active is False
    svc.on_monitors_changed(
        {"reason": "maintenance_windows", "_origin": "core-b"}
    )  # no loop → sync reload
    assert mnt.cache.state_for(rec, NOW).active is True
    assert svc._resync_task is None  # not a probe-job resync
    # our own emit (no _origin) is ignored; "maintenance" (destructive housekeeping) keeps its meaning
    gen = mnt.cache.generation
    svc.on_monitors_changed({"reason": "maintenance_windows"})
    svc.on_monitors_changed({"reason": "maintenance", "_origin": "core-b"})
    assert mnt.cache.generation == gen


def test_cache_reload_if_stale(svc):
    cache = svc.maintenance.cache
    cache.reload()
    assert cache.reload_if_stale(3600) is False
    assert cache.reload_if_stale(0) is True


# ── manual windows (API) ─────────────────────────────────────────────────────


def test_manual_window_is_published_and_ignored_by_own_handler(svc):
    from app.model.models import WindowCreate
    from core.api import maintenance as mw

    _host(svc, "host:d", ci_id=HOST_CI)
    mnt = svc.maintenance
    svc.ctx.emits.clear()
    row = mnt.create_manual_window(
        WindowCreate(
            monitor_id="host:d",
            note="storage move",
            windows=[
                {
                    "once": {
                        "start": mw.iso(NOW),
                        "end": mw.iso(NOW + timedelta(hours=2)),
                    }
                }
            ],
        )
    )
    assert (
        row["source"] == "monitoring"
        and row["ci_id"] == HOST_CI
        and row["monitor_id"] == "host:d"
    )
    assert row["kind"] == "host" and row["windows"][0]["type"] == "once"

    published = [p for t, p, c in svc.ctx.emits if t == mw.TOPIC_WINDOWS_PUBLISHED]
    assert len(published) == 1
    pub = published[0]
    assert pub["source"] == "monitoring" and pub["replaces"] is True
    assert (
        pub["items"][0]["ci_id"] == HOST_CI
        and pub["items"][0]["windows"] == row["windows"]
    )
    assert [c for t, _, c in svc.ctx.emits if t == mw.TOPIC_WINDOWS_PUBLISHED] == [
        False
    ]  # local emit

    # fed back through the bus the handler must not write a second copy
    assert mnt.on_windows_published(pub) == 0
    assert len(_rows(svc)) == 1

    # a second window for the same target appends to the row
    mnt.create_manual_window(
        WindowCreate(
            monitor_id="host:d",
            windows=[
                {
                    "weekly": {
                        "days": ["sun"],
                        "start": "02:00",
                        "end": "04:00",
                        "tz": "Europe/Berlin",
                    }
                }
            ],
        )
    )
    rows = _rows(svc)
    assert (
        len(rows) == 1
        and len(rows[0]["windows"]) == 2
        and rows[0]["note"] == "storage move"
    )

    # effective state per row
    listed = mnt.list_windows(monitor_id="host:d")
    assert listed[0]["effective"]["active"] is True and listed[0]["effective"][
        "until"
    ] == mw.iso(NOW + timedelta(hours=2))
    assert (
        listed[0]["effective"]["source"] == "window"
        and "next_start" in listed[0]["effective"]
    )


def test_manual_window_validation(svc):
    from app.model.models import WindowCreate

    _host(svc, "host:e")
    mnt = svc.maintenance
    with pytest.raises(ValueError, match="windows\\[0\\]"):
        mnt.create_manual_window(
            WindowCreate(
                monitor_id="host:e",
                windows=[{"weekly": {"days": [], "start": "02:00", "end": "04:00"}}],
            )
        )
    with pytest.raises(ValueError, match="ci_id or monitor_id"):
        mnt.create_manual_window(
            WindowCreate(
                windows=[
                    {"once": {"start": "2026-11-02T22:00Z", "end": "2026-11-03T02:00Z"}}
                ]
            )
        )
    with pytest.raises(KeyError):
        mnt.create_manual_window(
            WindowCreate(
                monitor_id="host:missing",
                windows=[
                    {"once": {"start": "2026-11-02T22:00Z", "end": "2026-11-03T02:00Z"}}
                ],
            )
        )
    # pydantic rejects an empty list before the service sees it
    with pytest.raises(Exception):
        WindowCreate(monitor_id="host:e", windows=[])
    assert _rows(svc) == []


def test_delete_window_only_for_manual_rows(svc):
    from app.model.models import WindowCreate

    _host(svc, "host:f", ci_id=HOST_CI)
    mnt = svc.maintenance
    mnt.on_windows_published(_published("iac-orchestrator", _item(HOST_CI, [])))
    foreign_id = _rows(svc, "iac-orchestrator")[0]["id"]
    own = mnt.create_manual_window(
        WindowCreate(
            ci_id=OTHER_CI,
            windows=[{"cron": {"expr": "0 2 1 * *", "duration_minutes": 180}}],
        )
    )
    assert own["kind"] == "generic" and own["monitor_id"] is None
    assert mnt.delete_manual_window(foreign_id) == "foreign"
    assert mnt.delete_manual_window(999_999) == "missing"
    assert mnt.delete_manual_window(own["id"]) == "deleted"
    assert [r["source"] for r in _rows(svc)] == ["iac-orchestrator"]


# ── manual overrides (API) ───────────────────────────────────────────────────


def test_manual_override_lifecycle(svc):
    from app.model.models import MonitorType, MonitorUpsert, OverrideCreate
    from core.api import maintenance as mw

    _host(svc, "host:g", ci_id=HOST_CI)
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id="service:g:web",
            name="g:web",
            monitor_type=MonitorType.HTTP,
            target="http://10.0.0.1",
            host_name="g",
            service_name="web",
        ),
        sync_scheduler=False,
    )
    svc.maintenance.reload()
    mnt = svc.maintenance
    mnt.apply_settings(
        {
            "override_default_minutes": "45",
            "override_max_minutes": "60",
            "override_linger_minutes": "5",
        }
    )
    svc.ctx.emits.clear()

    created = mnt.create_override(
        OverrideCreate(
            monitor_ids=["host:g", "service:g:web"], minutes=600, reason="kernel"
        )
    )
    assert (
        created["override_id"].startswith("mon-") and created["source"] == "monitoring"
    )
    assert created["ci_ids"] == [
        HOST_CI
    ]  # resolved from the monitors (service borrows the host ci)
    assert created["monitor_ids"] == ["host:g", "service:g:web"]
    started = mw.parse_iso(created["started_at"])
    assert mw.parse_iso(created["until"]) - started == timedelta(
        minutes=60
    )  # capped at override_max_minutes

    topics = [(t, c) for t, _, c in svc.ctx.emits]
    assert (mw.TOPIC_OVERRIDE_STARTED, False) in topics
    payload = next(p for t, p, _ in svc.ctx.emits if t == mw.TOPIC_OVERRIDE_STARTED)
    assert set(payload) == {
        "override_id",
        "ci_ids",
        "until",
        "reason",
        "source",
        "started_at",
        "linger_minutes",
    }
    assert payload["linger_minutes"] == 5 and payload["source"] == "monitoring"
    assert mnt.on_override_started(payload) is False  # own event → not stored twice

    with svc._session() as s:
        from app.model.models import MonitorRecord

        web = s.get(MonitorRecord, "service:g:web")
    assert mnt.cache.state_for(web, started + timedelta(minutes=1)).active

    active = mnt.list_overrides(active_only=True)["items"]
    assert (
        len(active) == 1
        and active[0]["own"] is True
        and active[0]["ci_names"] == [HOST_CI]
    )
    assert mnt.list_overrides()["defaults"] == {
        "default_minutes": 45,
        "max_minutes": 60,
        "linger_minutes": 5,
    }

    assert mnt.end_manual_override("nope") == "missing"
    assert mnt.end_manual_override(created["override_id"]) == "ended"
    ended = next(p for t, p, _ in svc.ctx.emits if t == mw.TOPIC_OVERRIDE_ENDED)
    assert (
        set(ended) == {"override_id", "source", "ended_at", "linger_minutes"}
        and ended["linger_minutes"] == 5
    )
    assert (
        mnt.cache.state_for(
            web, mw.parse_iso(ended["ended_at"]) + timedelta(minutes=6)
        ).active
        is False
    )

    with pytest.raises(ValueError):
        mnt.create_override(OverrideCreate())
    with pytest.raises(KeyError):
        mnt.create_override(OverrideCreate(monitor_ids=["host:missing"]))

    # a foreign override cannot be ended from here
    mnt.on_override_started(
        {
            "override_id": "job-1",
            "ci_ids": [HOST_CI],
            "until": mw.iso(NOW + timedelta(hours=1)),
            "source": "iac-orchestrator",
            "started_at": mw.iso(NOW),
        }
    )
    assert mnt.end_manual_override("job-1") == "foreign"


# ── settings ─────────────────────────────────────────────────────────────────


def test_settings_parse_switches_and_kinds(svc):
    svc.apply_settings(
        {
            "outage_rules_enabled": "true",
            "outage_rules_enabled_container": "true",
            "outage_rules_enabled_host": "off",
            "outage_rules_disabled_kinds": "network, Generic,bad kind",
            "outage_default_offline_minutes": "7",
            "outage_default_severity": "nonsense",
            "outage_default_auto_resolve": "no",
            "override_default_minutes": "500",
            "override_max_minutes": "120",
        }
    )
    s = svc.maintenance.settings
    assert (
        s.kind_enabled("container")
        and not s.kind_enabled("host")
        and s.kind_enabled("service")
    )
    assert (
        not s.kind_enabled("network")
        and not s.kind_enabled("generic")
        and s.kind_enabled("itsm_record")
    )
    rule = s.default_rule()
    assert (rule.offline_minutes, rule.severity, rule.auto_resolve) == (
        7,
        "high",
        False,
    )
    assert s.override_default_minutes == 120 and s.override_max_minutes == 120
    svc.apply_settings({"outage_rules_enabled": "false"})
    assert not svc.maintenance.settings.kind_enabled("service")
