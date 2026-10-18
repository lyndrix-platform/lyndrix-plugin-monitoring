"""``ci_id`` / ``host_ci_id`` on monitors (1.7.0): taken from the orchestrator's
inventory-sync payload, part of the config hash, and the host → service
fallback of the maintenance cache. Real service against in-memory SQLite; run
inside lyndrix-core-dev like the other suites."""

from __future__ import annotations

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


HOST_CI = "11111111-1111-4111-8111-111111111111"
SVC_CI = "22222222-2222-4222-8222-222222222222"


def _record(svc, mid):
    from app.model.models import MonitorRecord

    with svc._session() as s:
        return s.get(MonitorRecord, mid)


def test_inventory_ingest_takes_ci_ids(svc):
    from app.model.models import InventorySyncPayload

    payload = InventorySyncPayload(
        owner_source="orchestrator_service",
        source_revision="r1",
        hosts=[{"host_name": "docker-hydra", "address": "10.0.0.5", "ci_id": HOST_CI}],
        services=[
            {
                "host_name": "docker-hydra",
                "service_name": "traefik",
                "url": "http://10.0.0.5:8080",
                "ci_id": SVC_CI,
                "host_ci_id": HOST_CI,
            }
        ],
    )
    assert svc.ingest_inventory_snapshot(payload)["upserts"] == 2
    host = svc.get_monitor("host:docker-hydra")
    service = svc.get_monitor("service:docker-hydra:traefik")
    assert host["ci_id"] == HOST_CI and host["host_ci_id"] is None
    assert service["ci_id"] == SVC_CI and service["host_ci_id"] == HOST_CI
    # the serialised monitor carries the maintenance fields too
    assert service["in_maintenance"] is False and service["outage_since"] is None


def test_inventory_without_ci_ids_still_works(svc):
    from app.model.models import InventorySyncPayload

    payload = InventorySyncPayload(
        hosts=[{"host_name": "legacy", "address": "10.0.0.6"}], services=[]
    )
    svc.ingest_inventory_snapshot(payload)
    assert svc.get_monitor("host:legacy")["ci_id"] is None


def test_ci_id_is_part_of_the_config_hash(svc):
    from app.model.models import MonitorType, MonitorUpsert

    base = dict(
        monitor_id="host:a",
        name="a",
        monitor_type=MonitorType.PING,
        target="10.0.0.1",
        address="10.0.0.1",
        host_name="a",
    )
    svc.upsert_monitor(MonitorUpsert(**base), sync_scheduler=False)
    hash_without = _record(svc, "host:a").config_hash

    svc.upsert_monitor(MonitorUpsert(**base, ci_id=HOST_CI), sync_scheduler=False)
    rec = _record(svc, "host:a")
    assert (
        rec.ci_id == HOST_CI and rec.config_hash != hash_without
    )  # a changed ci_id IS a change

    svc.upsert_monitor(MonitorUpsert(**base, ci_id=HOST_CI), sync_scheduler=False)
    assert _record(svc, "host:a").config_hash == rec.config_hash  # idempotent

    svc.upsert_monitor(MonitorUpsert(**base), sync_scheduler=False)
    assert _record(svc, "host:a").ci_id is None  # removed upstream → removed here


def test_service_without_ci_id_borrows_the_host_ci(svc):
    from app.model.models import MonitorType, MonitorUpsert

    _host(svc, "host:h1", ci_id=HOST_CI)
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id="service:h1:web",
            name="h1:web",
            monitor_type=MonitorType.HTTP,
            target="http://10.0.0.1",
            host_name="h1",
            service_name="web",
        ),
        sync_scheduler=False,
    )
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id="service:h1:db",
            name="h1:db",
            monitor_type=MonitorType.SERVICE,
            target="10.0.0.1:3306",
            host_name="h1",
            service_name="db",
            ci_id=SVC_CI,
        ),
        sync_scheduler=False,
    )
    svc.maintenance.reload()
    cache = svc.maintenance.cache
    assert cache.resolve_ci(_record(svc, "host:h1")) == (HOST_CI, None)
    # no own ci → the host's ci, and no separate host_ci_id (it IS the host)
    assert cache.resolve_ci(_record(svc, "service:h1:web")) == (HOST_CI, None)
    # own ci (P2 delivered it) → own ci + the host ci from the host monitor
    assert cache.resolve_ci(_record(svc, "service:h1:db")) == (SVC_CI, HOST_CI)
    assert cache.kind_for(_record(svc, "host:h1")) == "host"
    assert cache.kind_for(_record(svc, "service:h1:db")) == "service"


def test_grouped_overview_carries_maintenance_targets(svc):
    from app.controller.helpers import build_grouped_overview

    _host(svc, "host:h2", ci_id=HOST_CI)
    monitors = svc.list_monitors()
    groups = build_grouped_overview(monitors, {}, timelines={})
    host = groups[0]["hosts"][0]
    assert host["monitor_ids"] == ["host:h2"] and host["ci_ids"] == [HOST_CI]
    assert host["in_maintenance"] is False and host["maintenance_until"] is None
    assert host["host_monitor"]["ci_id"] == HOST_CI
