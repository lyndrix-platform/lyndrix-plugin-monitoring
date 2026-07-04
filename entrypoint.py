"""
entrypoint.py — pure wiring layer for lyndrix-plugin-monitoring.

Contains only: manifest, plugin_state singleton, thin UI wrappers,
setup() lifecycle hook, and teardown() lifecycle hook.
All business logic, models, and UI code live under app/.
"""

import asyncio
import time

from nicegui import app as nicegui_app
from nicegui import ui

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.controller.api import build_plugin_router
from .app.model.models import AdminOverride, MonitorUpsert, PassiveResult
from .app.controller.service import MonitoringService
from .app.ui.overview import render_overview_ui as _render_overview_ui
from .app.ui.settings import render_settings_ui as _render_settings_ui
from .app.ui.widget import render_dashboard_widget as _render_dashboard_widget
from .app.ui.page import render_monitoring_page

# TODO(agent): canonical-anatomy move deferred to a dedicated refactor PR
#   (PLUGIN-MONITORING-007): src/ui -> app/ui/react, app/ui/*.py -> app/ui/nicegui,
#   app/controller -> app/logic, Vite outDir -> app/ui/static. Large mechanical
#   change requiring a bundle rebuild; staged separately to keep this diff reviewable.
# TODO(agent): main_layout is not yet exported from core.api (PLUGIN-MONITORING-012);
#   switch to the stable surface once it exists. Fallback below keeps the plugin loadable.
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
    version="1.0.0",
    description="Native infrastructure and service monitoring for Lyndrix.",
    author="Lyndrix",
    icon="monitor_heart",
    type="PLUGIN",
    min_core_version="0.1.1",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-monitoring",
    i18n_namespace="monitoring",
    ui_route="/monitoring",
    react_ui=True,
    react_routes=[
        {
            "path": "/monitoring",
            "label": "State Monitoring",
            "icon": "monitor_heart",
            "sidebar_visible": True,
        }
    ],
    permissions={
        "subscribe": [
            "db:connected",
            "monitoring:config_upsert",
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


async def render_overview_ui(ctx):
    svc = plugin_state.get("service")
    if svc is None:
        ui.label("Monitoring service not ready.").classes("text-xs text-red-400")
        return
    await _render_overview_ui(ctx, svc)


def render_settings_ui(ctx):
    svc = plugin_state.get("service")
    if svc is None:
        ui.label("Monitoring service not ready.").classes("text-xs text-red-400")
        return
    _render_settings_ui(ctx, svc)


async def render_dashboard_widget(ctx):
    await _render_dashboard_widget(ctx, plugin_state.get("service"))


# ---------------------------------------------------------------------------
# Health — functional liveness probe
# ---------------------------------------------------------------------------


async def health(ctx) -> PluginHealthStatus:
    """Functional health probe.

    A monitoring plugin is only "healthy" if it is actually *monitoring*. So we
    verify the live runtime, not just that ``setup()`` ran:

    * the service singleton exists and its scheduler was started,
    * the scheduler still holds jobs — ``start()`` always registers the daily
      maintenance jobs, so an empty job table while "started" means the loop is
      dead,
    * the DB (where probe results are persisted) is reachable, and
    * the monitor table is queryable.

    The sync DB read is offloaded so the probe never blocks the event loop.
    """
    start = time.perf_counter()

    svc = plugin_state.get("service")
    if svc is None:
        return PluginHealthStatus(status="error", details={"reason": "service_not_initialized"})

    if not getattr(svc, "_scheduler_started", False):
        return PluginHealthStatus(status="error", details={"reason": "scheduler_not_started"})

    try:
        jobs = list(svc.scheduler.get_jobs())
    except Exception as exc:
        return PluginHealthStatus(
            status="error",
            details={"reason": "scheduler_unavailable", "error": str(exc)},
        )
    if not jobs:
        # Started but no jobs at all → the scheduler loop is not alive.
        return PluginHealthStatus(
            status="error",
            details={"reason": "scheduler_no_jobs", "scheduler_started": True},
        )

    if not db_instance.is_connected:
        return PluginHealthStatus(
            status="error",
            details={"reason": "db_unavailable", "scheduler_started": True, "scheduler_jobs": len(jobs)},
        )

    try:
        monitors = await asyncio.to_thread(svc.list_monitors)
    except Exception as exc:
        return PluginHealthStatus(
            status="error",
            details={"reason": "db_query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    latency = round((time.perf_counter() - start) * 1000, 1)
    details = {
        "db_connected": True,
        "scheduler_started": True,
        "scheduler_jobs": len(jobs),
        "monitors_configured": len(monitors),
    }
    # Loop alive + DB reachable, but nothing is being watched yet.
    if not monitors:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "no_monitors_configured"},
            latency_ms=latency,
        )
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)


# ---------------------------------------------------------------------------
# Setup — called once by lyndrix-core on plugin load
# ---------------------------------------------------------------------------


def setup(ctx):
    ctx.log.info("State Monitoring: starting setup...")

    service = MonitoringService(ctx)
    service.start()
    plugin_state["service"] = service

    # Mount the single authenticated router through core's registry so every
    # route inherits require_api_auth (and the per-route api:read/api:write
    # permission checks). The previous anonymous /api/monitoring mount via
    # `from main import app` has been removed — it exposed mutating routes
    # unauthenticated.
    ctx.register_routes(build_plugin_router(service))
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
