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
    version="1.4.0",
    description="Native infrastructure and service monitoring for Lyndrix.",
    author="Lyndrix",
    icon="monitor_heart",
    type="PLUGIN",
    # settings_schema kind="action" (maintenance buttons) needs the 1.6 settings API.
    min_core_version="1.6.0",
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
    # Operator settings — core's schema-driven settings dialog, stored in Vault
    # under plugins/<id>/settings, applied on vault:ready_for_data.
    settings_schema=[
        {"key": "heartbeat_retention_days", "label": "Heartbeat retention (days)", "kind": "int",
         "category": "Storage", "default": "7",
         "description": "Raw probe results older than this are pruned (hourly, in batches). Uptime beyond this window comes from daily aggregates."},
        {"key": "initial_probe_workers", "label": "Startup sweep workers", "kind": "int",
         "category": "Probing", "default": "8",
         "description": "Concurrent probes during the initial sweep after a (re)start."},
        {"key": "overview_cache_seconds", "label": "Overview cache (seconds)", "kind": "int",
         "category": "Dashboard", "default": "10",
         "description": "How long a computed overview is served to concurrent viewers before it is recomputed (0 = always recompute)."},
        # Maintenance jobs (core API >= 1.6, kind="action"): buttons in the same
        # dialog that call the write-gated /maintenance/* routes in app/controller/api.py.
        {"key": "action_prune", "label": "Prune old heartbeats now", "kind": "action",
         "action": "/maintenance/prune", "category": "Maintenance",
         "description": "Runs the hourly retention job right away (raw probe results older than the retention)."},
        {"key": "action_rebuild_stats", "label": "Rebuild statistics", "kind": "action",
         "action": "/maintenance/rebuild-stats", "category": "Maintenance",
         "description": "Recomputes the daily aggregates from the raw heartbeats and refreshes the uptime windows."},
        {"key": "action_probe_all", "label": "Probe all monitors now", "kind": "action",
         "action": "/maintenance/probe-all", "category": "Maintenance",
         "description": "Queues a sweep over every enabled monitor (not only stale or unknown ones). Progress shows in the plugin banner."},
        {"key": "action_reset_stats", "label": "Delete statistics", "kind": "action",
         "action": "/maintenance/reset-stats", "category": "Maintenance", "confirm": True, "danger": True,
         "description": "Deletes all heartbeats and daily aggregates and resets uptime to 100 %. Monitor states are kept."},
        {"key": "action_clear_states", "label": "Reset states and statistics", "kind": "action",
         "action": "/clear-states", "category": "Maintenance", "confirm": True, "danger": True,
         "description": "Like 'Delete statistics', plus every monitor goes back to UNKNOWN and is re-probed in a blocking startup sweep."},
        {"key": "action_delete_managed", "label": "Delete IaC-managed monitors", "kind": "action",
         "action": "/maintenance/delete-monitors/managed", "category": "Maintenance", "confirm": True, "danger": True,
         "description": "Removes every monitor the IaC orchestrator created from the inventory, with its history. The orchestrator recreates them on its next inventory publish; manually created monitors are kept."},
        {"key": "action_delete_all", "label": "Delete ALL monitors", "kind": "action",
         "action": "/maintenance/delete-monitors/all", "category": "Maintenance", "confirm": True, "danger": True,
         "description": "Removes every monitor including manually created ones, with all history."},
    ],
    permissions={
        "subscribe": [
            "db:connected",
            "vault:ready_for_data",
            "monitoring:config_upsert",
            "monitoring:passive_result",
            "monitoring:admin_override",
            # Cluster: a peer changed the monitor set → the prober reloads its jobs.
            "monitoring:monitors_changed",
        ],
        "emit": ["monitoring:state_changed", "monitoring:monitors_changed", "messaging:outbound"],
    },
    # Identity 2.0: roles auto-mapped onto groups. Permissions are written
    # FULLY QUALIFIED (plugin:<id>:api:read / :api:write) rather than the bare
    # "api:read" / "api:write" strings the router itself checks — a bare
    # entry here would be passed through verbatim by the role registry's
    # ``_qualify`` (anything already prefixed with "api:" is treated as
    # already-qualified) and land as the GLOBAL api:read/write permission,
    # granting access to every other plugin's bare-permission-gated routes
    # too. This plugin has no custom_permissions, so operator and admin share
    # the same permission set; only auto_map_groups differs.
    roles=[
        {
            "id": "viewer",
            "label": "State Monitoring Viewer",
            "permissions": ["plugin:lyndrix.plugin.state_monitoring:api:read"],
            "auto_map_groups": [],
            "description": "Read-only access to monitors, history, and dashboards.",
        },
        {
            "id": "operator",
            "label": "State Monitoring Operator",
            "permissions": [
                "plugin:lyndrix.plugin.state_monitoring:api:read",
                "plugin:lyndrix.plugin.state_monitoring:api:write",
            ],
            "auto_map_groups": [],
            "description": "Read/write access: configure monitors, submit results, apply overrides.",
        },
        {
            "id": "admin",
            "label": "State Monitoring Administrator",
            "permissions": [
                "plugin:lyndrix.plugin.state_monitoring:api:read",
                "plugin:lyndrix.plugin.state_monitoring:api:write",
            ],
            "auto_map_groups": ["INT_ADMIN"],
            "description": "Full control, auto-granted to internal administrators.",
        },
    ],
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

    The sync DB read is offloaded so the probe never blocks the event loop, and
    it is deliberately cheap (one GROUP BY on the monitor table): the core polls
    health() from every dashboard, so it must never touch the heartbeat table.
    """
    start = time.perf_counter()

    svc = plugin_state.get("service")
    if svc is None:
        return PluginHealthStatus(status="error", details={"reason": "service_not_initialized"})

    prober = bool(getattr(svc, "is_prober", True))
    jobs: list = []
    if prober:
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
    # Standby instance (cluster): no scheduler by design — another instance holds
    # the prober lease. Serving the API from the shared DB is this instance's job.

    if not db_instance.is_connected:
        return PluginHealthStatus(
            status="error",
            details={"reason": "db_unavailable", "scheduler_started": True, "scheduler_jobs": len(jobs)},
        )

    try:
        counts = await asyncio.to_thread(svc.status_counts)
    except Exception as exc:
        return PluginHealthStatus(
            status="error",
            details={"reason": "db_query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    latency = round((time.perf_counter() - start) * 1000, 1)
    details = {
        "db_connected": True,
        "prober": prober,
        "scheduler_started": bool(getattr(svc, "_scheduler_started", False)),
        "scheduler_jobs": len(jobs),
        "monitors_configured": counts.get("monitor_count", 0),
        "monitors_up": counts.get("up_count", 0),
        "monitors_down": counts.get("down_count", 0),
    }
    # Loop alive + DB reachable, but nothing is being watched yet.
    if not counts.get("monitor_count"):
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
    plugin_state["service"] = service

    # Mount the single authenticated router through core's registry so every
    # route inherits require_api_auth (and the per-route api:read/api:write
    # permission checks). The previous anonymous /api/monitoring mount via
    # `from main import app` has been removed — it exposed mutating routes
    # unauthenticated. Routes run on EVERY instance (reads from the shared DB).
    ctx.register_routes(build_plugin_router(service))

    # Cluster placement (core API ≥ 1.7): exactly one instance probes. The lease
    # is granted immediately in single mode; on a cluster the holder's death
    # hands it over within the lease TTL. Older cores: start probing directly.
    on_lease = getattr(ctx, "on_lease", None)
    if callable(on_lease):
        on_lease("monitoring:prober", acquired=service.start_probing, lost=service.stop_probing)
        service.queue_bootstrap()  # tables for the API; sweeps wait for the lease
    else:
        service.start_probing()

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

    @ctx.subscribe("monitoring:monitors_changed")
    async def on_monitors_changed(payload):
        # Our own changes already resynced locally; a peer's change (bridged
        # event, ``_origin`` set) means the prober must reload its jobs.
        if (payload or {}).get("_origin"):
            service.request_resync()

    @ctx.subscribe("vault:ready_for_data")
    async def on_vault_ready(payload):
        try:
            values = await asyncio.to_thread(ctx.get_all_settings)  # Vault I/O is blocking
            service.apply_settings(values or {})
        except Exception as exc:
            ctx.log.warning(f"State Monitoring: could not apply settings — {exc}")

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
