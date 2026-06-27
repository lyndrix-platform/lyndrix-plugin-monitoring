"""
tests/test_service.py — smoke tests for lyndrix-plugin-monitoring.

These tests verify that the module structure and key symbols are importable
without requiring a live database or running event loop.
"""

import importlib
import sys


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
    import entrypoint

    assert entrypoint.plugin_state.get("service") is None, (
        "plugin_state['service'] should be None before setup() is called"
    )


def test_manifest_fields():
    """Manifest must carry the correct version, repo_url, and id."""
    import entrypoint

    m = entrypoint.manifest
    # Keep these in sync with the manifest in entrypoint.py / the git tag.
    assert m.version == "0.0.8"
    assert m.repo_url == "https://github.com/lyndrix-platform/lyndrix-plugin-monitoring"
    assert m.id == "lyndrix.plugin.state_monitoring"
    assert m.min_core_version == "0.1.1"
