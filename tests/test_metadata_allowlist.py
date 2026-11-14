"""MO-01 — ``metadata_json`` only ever holds the allowlisted, non-secret subset.

The inventory publisher used to send whole ``terraform`` / service ``config``
blocks; they were stored verbatim and served to every ``api:read`` holder.
Covered here: ingest, every write path, read-time filtering of old rows, the
idempotent startup scrub, and that no secret reaches the bus or the API.
All secret values are obviously fake. Real service against in-memory SQLite."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture

FAKE_SECRETS = (
    "dummy-pve-password-not-real",
    "dummy-pve-token-not-real",
    "dummy-db-password-not-real",
    "dummy-jwt-secret-not-real",
    "dummy-git-token-not-real",
)


def _inventory():
    from app.model.models import InventorySyncPayload

    return InventorySyncPayload(
        owner_source="orchestrator_service",
        source_revision="rev-1",
        hosts=[
            {
                "host_name": "pve",
                "address": "10.0.0.5",
                "stage": "prod",
                "groups": ["site_olympus", "docker_hosts"],
                "baseline_roles": ["docker"],
                "terraform": {
                    "node_name": "hydra",
                    "vm_id": 9001,
                    "password": FAKE_SECRETS[0],
                    "token": FAKE_SECRETS[1],
                    "provider": {"api_token": FAKE_SECRETS[1]},
                },
            }
        ],
        services=[
            {
                "host_name": "pve",
                "service_name": "db",
                "url": "http://10.0.0.5:8080/health",
                "address": "10.0.0.5",
                "site": "olympus",
                "deploy_type": "docker_compose",
                "git_repo": f"https://oauth2:{FAKE_SECRETS[4]}@git.example.invalid/aac-db.git",
                "git_version": "1.2.3",
                "config": {
                    "secrets": {"DB_PASSWORD": FAKE_SECRETS[2], "JWT_SECRET": FAKE_SECRETS[3]},
                    "env": {"TZ": "UTC"},
                },
            }
        ],
    )


def _raw_metadata(svc, monitor_id):
    from app.model.models import MonitorRecord

    with svc._session() as s:
        return s.get(MonitorRecord, monitor_id).metadata_json


def _assert_clean(blob) -> None:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    for secret in FAKE_SECRETS:
        assert secret not in text
    assert "DB_PASSWORD" not in text and "JWT_SECRET" not in text


def test_sanitize_metadata_keeps_only_allowlisted_scalars():
    from app.model.models import sanitize_metadata

    clean = sanitize_metadata(
        {
            "site": "olympus",
            "groups": ["a", {"nested": FAKE_SECRETS[0]}, 3],
            "stage": {"smuggled": FAKE_SECRETS[0]},  # allowlisted key, wrong shape
            "terraform": {"node_name": "hydra", "vm_id": 7, "password": FAKE_SECRETS[0]},
            "config": {"secrets": {"X": FAKE_SECRETS[2]}},
            "git_repo": f"https://x:{FAKE_SECRETS[4]}@git.example.invalid/r.git",
            "anything_else": FAKE_SECRETS[3],
        }
    )
    assert clean == {
        "site": "olympus",
        "groups": ["a", 3],
        "terraform": {"node_name": "hydra", "vm_id": 7},
    }
    assert sanitize_metadata("not a dict") == {} and sanitize_metadata(None) == {}


def test_inventory_secrets_never_reach_db_api_or_bus(svc):
    stats = svc.ingest_inventory_snapshot(_inventory())
    assert stats["upserts"] == 2

    # DB rows
    host_raw, svc_raw = _raw_metadata(svc, "host:pve"), _raw_metadata(svc, "service:pve:db")
    _assert_clean(host_raw)
    _assert_clean(svc_raw)
    assert json.loads(host_raw) == {
        "baseline_roles": ["docker"],
        "groups": ["site_olympus", "docker_hosts"],
        "profiles": [],
        "site": None,
        "stage": "prod",
        "terraform": {"node_name": "hydra", "vm_id": 9001},
    }
    assert json.loads(svc_raw) == {
        "deploy_type": "docker_compose",
        "desired_state": None,
        "git_version": "1.2.3",
        "groups": [],
        "site": "olympus",
        "stage": None,
    }

    # every read surface
    _assert_clean(svc.get_monitor("host:pve"))
    _assert_clean(svc.get_monitor("service:pve:db"))
    _assert_clean(svc.list_monitors())
    _assert_clean(asyncio.run(svc.dashboard()))
    _assert_clean(asyncio.run(svc.overview(group_by="site")))
    _assert_clean(svc._load_probe_record("service:pve:db"))

    # the grouping/probe logic still gets what it reads
    from app.controller.provider.docker import is_docker_host

    assert is_docker_host(svc._load_probe_record("host:pve")["metadata"])
    assert svc._load_probe_record("service:pve:db")["metadata"]["deploy_type"] == "docker_compose"

    # nothing secret was (re-)emitted on the bus
    _assert_clean(svc.ctx.events)


def test_async_ingest_route_path_emits_no_secret(svc):
    stats = asyncio.run(svc.ingest_inventory(_inventory()))
    _assert_clean(stats)
    _assert_clean(svc.ctx.events)
    _assert_clean(svc.list_monitors())


def test_write_api_responses_and_rows_are_filtered(svc):
    from app.model.models import AdminOverride, MonitorState, MonitorType, MonitorUpsert, PassiveResult

    created = svc.upsert_monitor(
        MonitorUpsert(
            monitor_id="manual:x",
            name="x",
            monitor_type=MonitorType.HTTP,
            target="http://10.0.0.9/",
            metadata={"location": "rack-1", "config": {"secrets": {"DB_PASSWORD": FAKE_SECRETS[2]}}},
        ),
        sync_scheduler=False,
    )
    assert created["metadata"] == {"location": "rack-1"}
    _assert_clean(_raw_metadata(svc, "manual:x"))

    patched = svc.apply_admin_override(
        AdminOverride(
            monitor_id="manual:x",
            action="update",
            patch={"metadata": {"stage": "test", "terraform": {"password": FAKE_SECRETS[0]}}},
        )
    )
    assert patched["metadata"] == {"location": "rack-1", "stage": "test"}
    _assert_clean(_raw_metadata(svc, "manual:x"))

    passive = svc.ingest_passive_result(PassiveResult(monitor_id="manual:x", state=MonitorState.UP))
    _assert_clean(passive)
    _assert_clean(svc.ctx.events)


def _plant_legacy_rows(svc, count=1):
    """Rows as 1.7.0 wrote them: the publisher's blocks verbatim."""
    from app.model.models import MonitorRecord

    legacy = json.dumps(
        {
            "stage": "prod",
            "groups": ["docker"],
            "terraform": {"node_name": "hydra", "password": FAKE_SECRETS[0], "token": FAKE_SECRETS[1]},
            "config": {"secrets": {"DB_PASSWORD": FAKE_SECRETS[2]}},
        },
        sort_keys=True,
    )
    with svc._session() as s:
        for i in range(count):
            s.add(
                MonitorRecord(
                    monitor_id=f"host:legacy-{i:04d}",
                    name=f"legacy-{i}",
                    monitor_type="server",
                    owner_source="orchestrator_service",
                    target="10.0.0.1",
                    address="10.0.0.1",
                    host_name=f"legacy-{i}",
                    config_hash="rev-0",
                    source_hash="rev-0",
                    metadata_json=legacy,
                )
            )
        s.commit()


def test_legacy_row_is_filtered_at_read_time_before_any_scrub(svc):
    _plant_legacy_rows(svc)
    assert FAKE_SECRETS[0] in _raw_metadata(svc, "host:legacy-0000")  # precondition
    item = svc.get_monitor("host:legacy-0000")
    _assert_clean(item)
    assert item["metadata"] == {"stage": "prod", "groups": ["docker"], "terraform": {"node_name": "hydra"}}
    _assert_clean(svc.list_monitors())
    _assert_clean(svc._load_probe_record("host:legacy-0000"))


def test_startup_scrub_rewrites_legacy_rows_in_chunks_and_is_idempotent(svc):
    svc.METADATA_SCRUB_CHUNK = 7  # several chunks
    _plant_legacy_rows(svc, count=20)
    svc.ensure_tables()  # the bootstrap path (under the cross-instance lock in production)
    for i in range(20):
        raw = _raw_metadata(svc, f"host:legacy-{i:04d}")
        _assert_clean(raw)
        assert json.loads(raw) == {"stage": "prod", "groups": ["docker"], "terraform": {"node_name": "hydra"}}
    assert svc.scrub_monitor_metadata() == 0  # second run: nothing left to rewrite


def test_scrub_retries_a_concurrent_write_conflict(svc):
    """MariaDB 1020 (row changed by a probe writer) on a chunk is retried."""
    _plant_legacy_rows(svc, count=3)

    class _Conflict(Exception):
        orig = type("E", (), {"args": (1020, "Record has changed since last read")})()

    real, calls = svc._retrying, {"n": 0}

    def flaky(session, fn, *a, **kw):
        def once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Conflict()
            return fn()

        return real(session, once, *a, **kw)

    svc._retrying = flaky
    try:
        assert svc.scrub_monitor_metadata() == 3
    finally:
        svc._retrying = real
    assert calls["n"] == 2
    _assert_clean(_raw_metadata(svc, "host:legacy-0001"))


def test_same_revision_upsert_scrubs_a_legacy_row(svc):
    """A re-publish of an unchanged revision takes the config-hash shortcut —
    the stored secrets must still go."""
    from app.model.models import MonitorType, MonitorUpsert

    _plant_legacy_rows(svc)
    svc.upsert_monitor(
        MonitorUpsert(
            monitor_id="host:legacy-0000",
            name="legacy-0",
            monitor_type=MonitorType.SERVER,
            owner_source="orchestrator_service",
            target="10.0.0.1",
            address="10.0.0.1",
            host_name="legacy-0",
            source_hash="rev-0",
        ),
        sync_scheduler=False,
    )
    _assert_clean(_raw_metadata(svc, "host:legacy-0000"))


def test_validation_errors_are_logged_without_input_values():
    from pydantic import ValidationError

    from app.model.models import MonitorUpsert, safe_error_text

    try:
        MonitorUpsert(monitor_id="m", name="n", monitor_type="http", metadata=FAKE_SECRETS[0])
    except ValidationError as exc:
        assert FAKE_SECRETS[0] in str(exc)  # what the old log line printed
        assert FAKE_SECRETS[0] not in safe_error_text(exc) and "metadata" in safe_error_text(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a validation error")
