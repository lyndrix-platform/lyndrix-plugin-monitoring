"""
app/ui/page.py — Full monitoring page extracted from entrypoint.py.

render_monitoring_page(ctx, svc) renders the complete /monitoring route body.
"""

import asyncio

from nicegui import ui

from .overview import render_overview_ui


async def render_monitoring_page(ctx, svc):
    with ui.column().classes(
        "w-full max-w-[calc(100vw-2.5rem)] 2xl:max-w-[calc(100vw-3rem)] mx-auto gap-6 px-2"
    ):
        # ── Header card (skeleton stats shown immediately) ──────────────
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
                            stat_labels[key] = ui.label("—").classes(
                                f"text-3xl font-black {cls} leading-none"
                            )

        # ── Overview area — spinner until data loads ────────────────────
        overview_container = ui.column().classes("w-full gap-6")

        with overview_container:
            with ui.column().classes("w-full items-center justify-center py-24 gap-4"):
                ui.spinner("dots", size="xl").classes("text-cyan-400")
                ui.label("Loading monitors…").classes("text-sm text-zinc-400")

        # ── Load data off the event loop, then populate the UI ──────────
        async def _initial_load():
            stats_map, monitors_data = await asyncio.gather(
                asyncio.to_thread(svc.stats),
                asyncio.to_thread(svc.list_monitors),
            )
            histories_data = await asyncio.to_thread(
                svc.get_histories, [m["monitor_id"] for m in monitors_data], 24
            )
            for key, lbl in stat_labels.items():
                v = stats_map.get(key, 0)
                lbl.set_text(f"{v:.1f}%" if key == "uptime_all" else str(v))
            overview_container.clear()
            with overview_container:
                render_overview_ui(ctx, svc, monitors=monitors_data, histories=histories_data)

        async def refresh_dashboard():
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
                render_overview_ui(ctx, svc, monitors=new_monitors, histories=new_histories)

        # First load runs immediately after page is delivered to browser.
        ui.timer(0.05, _initial_load, once=True)
        # Periodic refresh every 15 s.
        ui.timer(15.0, refresh_dashboard)
