"""
entrypoint.py — pure wiring layer for lyndrix-plugin-monitoring.

Contains only: manifest, plugin_state singleton, thin UI wrappers,
setup() lifecycle hook, and teardown() lifecycle hook.
All business logic, models, and UI code live under app/.
"""

import asyncio

from nicegui import app as nicegui_app
from nicegui import ui

from core.api import ModuleManifest

from .app.controller.api import build_router, register_api_routes
from .app.model.models import AdminOverride, InventorySyncPayload, MonitorUpsert, PassiveResult
from .app.controller.service import MonitoringService
from .app.ui.overview import render_overview_ui as _render_overview_ui
from .app.ui.settings import render_settings_ui as _render_settings_ui
from .app.ui.widget import render_dashboard_widget as _render_dashboard_widget
from .app.ui.page import render_monitoring_page

try:
    from ui.layout import main_layout
except ImportError:

    def main_layout(title, *, wide: bool = False):  # type: ignore
        def decorator(fn):
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Plugin manifest
# ---------------------------------------------------------------------------

manifest = ModuleManifest(
    id="lyndrix.plugin.state_monitoring",
    name="State Monitoring",
    version="0.0.7",
    description="Native infrastructure and service monitoring for Lyndrix.",
    author="Lyndrix",
    icon="monitor_heart",
    type="PLUGIN",
    min_core_version="0.1.1",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-monitoring",
    ui_route="/monitoring",
    permissions={
        "subscribe": [
            "db:connected",
            "monitoring:config_upsert",
            "monitoring:inventory_sync",
            "monitoring:passive_result",
            "monitoring:admin_override",
        ],
        "emit": ["monitoring:state_changed", "messaging:outbound"],
    },
)

# ---------------------------------------------------------------------------
# Plugin state (singleton per process)
# ---------------------------------------------------------------------------

plugin_state: dict = {"service": None}

# ---------------------------------------------------------------------------
# Public plugin API — thin wrappers required by lyndrix-core
# ---------------------------------------------------------------------------


def render_overview_ui(ctx):
    svc = plugin_state.get("service")
    if svc is None:
        ui.label("Monitoring service not ready.").classes("text-xs text-red-400")
        return
    _render_overview_ui(ctx, svc)


def render_settings_ui(ctx):
    svc = plugin_state.get("service")
    if svc is None:
        ui.label("Monitoring service not ready.").classes("text-xs text-red-400")
        return
    _render_settings_ui(ctx, svc)


async def render_dashboard_widget(ctx):
    await _render_dashboard_widget(ctx, plugin_state.get("service"))


# ---------------------------------------------------------------------------
# Setup — called once by lyndrix-core on plugin load
# ---------------------------------------------------------------------------


def setup(ctx):
    ctx.log.info("State Monitoring: starting setup...")

    service = MonitoringService(ctx)
    service.start()
    plugin_state["service"] = service

    from main import app as fastapi_app  # noqa: PLC0415 — runtime import like original

    router = build_router(service)
    register_api_routes(fastapi_app, router)
    service.queue_bootstrap()

    @nicegui_app.on_shutdown
    async def _on_shutdown():
        svc = plugin_state.get("service")
        if svc is not None:
            ctx.log.info("State Monitoring: shutdown hook triggered, stopping background tasks...")
            await svc.stop()
            plugin_state["service"] = None

    @ctx.subscribe("db:connected")
    async def on_db_connected(payload):
        service.queue_bootstrap()

    @ctx.subscribe("monitoring:config_upsert")
    async def on_config_upsert(payload):
        try:
            await asyncio.to_thread(service.upsert_monitor, MonitorUpsert(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: config upsert failed: {exc}")

    @ctx.subscribe("monitoring:passive_result")
    async def on_passive_result(payload):
        try:
            await asyncio.to_thread(service.ingest_passive_result, PassiveResult(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: passive result failed: {exc}")

    @ctx.subscribe("monitoring:admin_override")
    async def on_admin_override(payload):
        try:
            await asyncio.to_thread(service.apply_admin_override, AdminOverride(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: admin override failed: {exc}")

    @ctx.subscribe("monitoring:inventory_sync")
    async def on_inventory_sync(payload):
        try:
            service.queue_inventory_sync(InventorySyncPayload(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: inventory sync failed: {exc}")

    @ui.page("/monitoring")
    @main_layout("State Monitoring", wide=True)
    async def _page():
        svc = plugin_state.get("service")
        if svc is None:
            ui.label("Monitoring service not ready.").classes("text-xs text-red-400")
            return
        await render_monitoring_page(ctx, svc)

    ctx.log.info("State Monitoring: setup complete.")


# ---------------------------------------------------------------------------
# Teardown — called by lyndrix-core on plugin unload
# ---------------------------------------------------------------------------


async def teardown(ctx):
    svc = plugin_state.get("service")
    if svc is not None:
        ctx.log.info("State Monitoring: teardown called, stopping background tasks...")
        await svc.stop()
        plugin_state["service"] = None
    ctx.log.info("State Monitoring: teardown complete.")
