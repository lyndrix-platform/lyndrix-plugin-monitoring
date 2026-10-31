"""
tests/test_service.py — smoke tests for lyndrix-plugin-monitoring.

These tests verify that the module structure and key symbols are importable
without requiring a live database or running event loop.
"""

import importlib


def test_monitoring_service_importable():
    """MonitoringService must be importable from app.controller.service."""
    # Use importlib to avoid needing the full plugin package on sys.path
    spec = importlib.util.find_spec("app.controller.service")
    assert spec is not None, "app.controller.service module not found on sys.path"

    module = importlib.import_module("app.controller.service")
    assert hasattr(module, "MonitoringService"), (
        "MonitoringService not found in app.controller.service"
    )


def test_plugin_state_initial_value():
    """plugin_state['service'] must be None before setup() is called."""
    from monitoring_plugin import entrypoint

    assert entrypoint.plugin_state.get("service") is None, (
        "plugin_state['service'] should be None before setup() is called"
    )


def test_manifest_fields():
    """Manifest must carry the correct version, repo_url, and id."""
    from monitoring_plugin import entrypoint

    m = entrypoint.manifest
    # Keep these in sync with the manifest in entrypoint.py / the git tag.
    assert m.version == "1.7.0"
    assert m.repo_url == "https://github.com/lyndrix-platform/lyndrix-plugin-monitoring"
    assert m.id == "lyndrix.plugin.state_monitoring"
    assert m.min_core_version == "1.13.0"


def test_manifest_declares_maintenance_topics():
    """Bus contract (00-ergebnis-p0.md §3): consume the maintenance events, emit
    our manual windows/overrides and the outage events — all declared, otherwise
    ``ctx.emit`` raises PermissionError at runtime."""
    from monitoring_plugin import entrypoint

    perms = entrypoint.manifest.permissions
    assert {"maintenance:windows_published", "maintenance:override_started", "maintenance:override_ended"} <= set(
        perms.subscribe
    )
    assert {
        "maintenance:windows_published", "maintenance:override_started", "maintenance:override_ended",
        "monitoring:outage_persisted", "monitoring:outage_recovered",
    } <= set(perms.emit)
    keys = {f.key for f in entrypoint.manifest.settings_schema}
    assert {
        "outage_rules_enabled", "outage_rules_enabled_host", "outage_rules_enabled_service",
        "outage_rules_enabled_hypervisor", "outage_rules_enabled_container", "outage_rules_disabled_kinds",
        "outage_default_offline_minutes", "outage_default_severity", "outage_default_auto_resolve",
        "override_default_minutes", "override_max_minutes", "override_linger_minutes",
    } <= keys
