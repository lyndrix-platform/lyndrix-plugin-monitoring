"""
entrypoint.py — slim plugin entry point for lyndrix-state-monitoring.

All business logic, models, providers, and UI modules live in their
own files.  This file only glues them together and satisfies the
lyndrix-core plugin API.
"""

import asyncio

from nicegui import app as nicegui_app
from nicegui import ui

from core.api import ModuleManifest

from .api import build_router, register_api_routes
from .models import AdminOverride, InventorySyncPayload, MonitorUpsert, PassiveResult
from .service import MonitoringService
from .ui_overview import render_overview_ui as _render_overview_ui
from .ui_settings import render_settings_ui as _render_settings_ui
from .ui_widget import render_dashboard_widget as _render_dashboard_widget

try:
    from ui.layout import main_layout
except ImportError:

    def main_layout(title):  # type: ignore
        def decorator(fn):
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Plugin manifest
# ---------------------------------------------------------------------------

manifest = ModuleManifest(
    id="lyndrix.plugin.state_monitoring",
    name="State Monitoring",
    version="0.0.3",
    description="Native infrastructure and service monitoring for Lyndrix.",
    author="Lyndrix",
    icon="monitor_heart",
    type="PLUGIN",
    min_core_version="0.0.1",
    auto_enable_on_install=False,
    repo_url="https://github.com/marvin1309/lyndrix-state-monitoring",
    ui_route="/monitoring",
    permissions={
        "subscribe": [
            "db:connected",
            "monitoring:config_upsert",
            "monitoring:inventory_sync",
            "monitoring:passive_result",
            "monitoring:admin_override",
        ],
        "emit": ["monitoring:state_changed"],
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


def render_dashboard_widget(ctx):
    _render_dashboard_widget(ctx, plugin_state.get("service"))


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
            service.upsert_monitor(MonitorUpsert(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: config upsert failed: {exc}")

    @ctx.subscribe("monitoring:passive_result")
    async def on_passive_result(payload):
        try:
            service.ingest_passive_result(PassiveResult(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: passive result failed: {exc}")

    @ctx.subscribe("monitoring:admin_override")
    async def on_admin_override(payload):
        try:
            service.apply_admin_override(AdminOverride(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: admin override failed: {exc}")

    @ctx.subscribe("monitoring:inventory_sync")
    async def on_inventory_sync(payload):
        try:
            service.queue_inventory_sync(InventorySyncPayload(**payload))
        except Exception as exc:
            ctx.log.error(f"State Monitoring: inventory sync failed: {exc}")

    @ui.page("/monitoring")
    @main_layout("State Monitoring")
    async def monitoring_page():
        svc = plugin_state["service"]

        # Pre-fetch all DB data off the event loop so the UI never hangs.
        stats_map = await asyncio.to_thread(svc.stats)
        monitors_data = await asyncio.to_thread(svc.list_monitors)
        histories_data = await asyncio.to_thread(
            svc.get_histories, [m["monitor_id"] for m in monitors_data], 24
        )

        with ui.column().classes(
            "w-full max-w-[calc(100vw-2.5rem)] 2xl:max-w-[calc(100vw-3rem)] mx-auto gap-6 px-2"
        ):
            # Header card with live stats
            with ui.card().classes(
                "w-full p-0 overflow-hidden bg-gradient-to-br from-zinc-950 via-zinc-900 to-slate-950 border border-zinc-800"
            ):
                ui.element("div").classes(
                    "h-1 w-full bg-gradient-to-r from-cyan-400 via-emerald-400 to-lime-400"
                )
                with ui.column().classes("w-full p-6 gap-4"):
                    ui.label("State Monitoring").classes("text-3xl font-black text-zinc-50")
                    ui.label(
                        "Persistent monitoring for servers and services with grouped status "
                        "timelines and optional IaC inventory sync."
                    ).classes("text-sm text-zinc-400")

                    stat_labels: dict = {}
                    # Responsive grid: 2 cols on mobile → 3 on sm → 5 on lg
                    with ui.element("div").classes(
                        "grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 w-full gap-3"
                    ):
                        for label, key, cls in [
                            ("Monitors", "monitor_count", "text-cyan-300"),
                            ("Up", "up_count", "text-emerald-300"),
                            ("Down", "down_count", "text-rose-300"),
                            ("Paused", "paused_count", "text-amber-300"),
                            ("Uptime", "uptime_all", "text-sky-300"),
                        ]:
                            with ui.card().classes(
                                "p-4 bg-zinc-950/70 border border-zinc-800 rounded-xl "
                                "flex flex-col items-center justify-center text-center gap-1"
                            ):
                                ui.label(label).classes("text-xs uppercase tracking-widest text-zinc-500")
                                value = (
                                    f"{stats_map[key]:.1f}%" if key == "uptime_all" else str(stats_map[key])
                                )
                                stat_labels[key] = ui.label(value).classes(
                                    f"text-3xl font-black {cls} leading-none"
                                )

            overview_container = ui.column().classes("w-full gap-6")

            async def refresh_dashboard():
                # All DB work runs in threads — the event loop stays free.
                latest, new_monitors = await asyncio.gather(
                    asyncio.to_thread(svc.stats),
                    asyncio.to_thread(svc.list_monitors),
                )
                new_histories = await asyncio.to_thread(
                    svc.get_histories, [m["monitor_id"] for m in new_monitors], 24
                )
                for key, lbl in stat_labels.items():
                    v = latest.get(key, 0)
                    lbl.set_text(f"{v:.1f}%" if key == "uptime_all" else str(v))
                overview_container.clear()
                with overview_container:
                    _render_overview_ui(ctx, svc, monitors=new_monitors, histories=new_histories)

            with overview_container:
                _render_overview_ui(ctx, svc, monitors=monitors_data, histories=histories_data)

            # 15 s refresh keeps the UI responsive; monitoring runs independently.
            ui.timer(15.0, refresh_dashboard)

    ctx.log.info("State Monitoring: setup complete.")

