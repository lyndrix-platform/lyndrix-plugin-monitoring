from nicegui import ui

from ..controller.service import MonitoringService
from .helpers import build_grouped_overview, host_tile_span_classes, service_grid_classes
from .styles import state_badge_classes, state_card_classes, state_strip_style
from .timeline import timeline_html, timeline_scale_html


def render_overview_ui(ctx, service: MonitoringService, *, monitors=None, histories=None):
    if monitors is None:
        monitors = service.list_monitors()
    if histories is None:
        histories = service.get_histories([m["monitor_id"] for m in monitors], limit_hours=24)
    grouped_sites = build_grouped_overview(monitors, histories)

    if not grouped_sites:
        with ui.card().classes("w-full p-8 bg-zinc-950/70 border border-zinc-800 text-center"):
            ui.label("No monitors available yet.").classes("text-lg font-bold text-zinc-100")
            ui.label("Add monitors in plugin settings or wait for inventory sync events.").classes("text-sm text-zinc-400")
        return

    with ui.column().classes("w-full gap-8"):
        for site in grouped_sites:
            with ui.column().classes("w-full gap-4"):
                # Site header
                with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label(site["name"]).classes("text-2xl font-black text-zinc-50")
                        ui.label(f"{site['host_count']} hosts · {site['service_count']} services").classes("text-sm text-zinc-400")
                    with ui.column().classes("items-end gap-2"):
                        ui.label(site["state"]).classes(f"text-xs uppercase tracking-[0.25em] px-3 py-1 rounded-full {state_badge_classes(site['state'])}")
                        ui.label(f"24h {site['uptime_24h']:.1f}% · all-time {site['uptime_all']:.1f}%").classes("text-sm text-zinc-400")
                # Site timeline — full width, full size
                ui.html(timeline_html(site["timeline"], size="full")).classes("block w-full")
                ui.html(timeline_scale_html(size="full")).classes("block w-full")
                ui.separator().classes("opacity-15")

                for stage in site["stages"]:
                    with ui.column().classes("w-full gap-3"):
                        # Stage header
                        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                            with ui.row().classes("items-center gap-3"):
                                ui.label(stage["name"]).classes("text-lg font-bold text-zinc-100")
                                ui.label(stage["state"]).classes(f"text-[11px] uppercase tracking-[0.22em] px-2.5 py-1 rounded-full {state_badge_classes(stage['state'])}")
                            ui.label(
                                f"{stage['host_count']} hosts · {stage['service_count']} services · "
                                f"24h {stage['uptime_24h']:.1f}% · all-time {stage['uptime_all']:.1f}%"
                            ).classes("text-sm text-zinc-400")
                        # Stage timeline — full width, full size
                        ui.html(timeline_html(stage["timeline"], size="full")).classes("block w-full")
                        ui.html(timeline_scale_html(size="full")).classes("block w-full")

                        # Host cards grid
                        with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-4 w-full"):
                            for host in stage["hosts"]:
                                host_has_services = bool(host["services"])
                                span_cls = host_tile_span_classes(host["service_count"])
                                card_bg = (
                                    "bg-black/20 backdrop-blur-sm"
                                    if host_has_services
                                    else "bg-gradient-to-br from-black/30 via-black/20 to-slate-950/40 backdrop-blur-sm"
                                )
                                with ui.card().classes(
                                    f"p-0 overflow-hidden {card_bg} border {state_card_classes(host['state'])} gap-0 {span_cls}".strip()
                                ):
                                    ui.element("div").style(state_strip_style(host["state"]))
                                    with ui.column().classes("w-full p-5 gap-3"):
                                        # Host title row
                                        with ui.row().classes("w-full items-start justify-between gap-3"):
                                            with ui.column().classes("gap-0"):
                                                ui.label(host["name"]).classes("text-lg font-black text-zinc-50")
                                                if host.get("address"):
                                                    ui.label(host["address"]).classes("text-sm text-zinc-500")
                                            ui.label(host["state"]).classes(f"text-[11px] uppercase tracking-[0.22em] px-2.5 py-1 rounded-full {state_badge_classes(host['state'])}")
                                        with ui.row().classes("w-full justify-between text-xs text-zinc-400"):
                                            ui.label("Standalone host" if not host_has_services else f"{host['service_count']} services")
                                            ui.label(f"24h {host['uptime_24h']:.1f}% · all-time {host['uptime_all']:.1f}%")
                                        # Host timeline — host size, full width
                                        ui.html(timeline_html(host["timeline"], size="host")).classes("block w-full")
                                        ui.html(timeline_scale_html(size="host")).classes("block w-full")

                                        if not host_has_services:
                                            monitor = host.get("host_monitor")
                                            if monitor and monitor.get("target"):
                                                ui.label(str(monitor["target"])).classes("text-xs text-zinc-500 break-all")
                                            if monitor and monitor.get("latest_error"):
                                                ui.label(str(monitor["latest_error"])).classes("text-xs text-rose-300")
                                            continue

                                        # Host monitor card (ping/icmp health)
                                        if host.get("host_monitor"):
                                            hm = host["host_monitor"]
                                            with ui.card().classes(f"w-full p-0 overflow-hidden bg-white/5 border {state_card_classes(hm['state'])}"):
                                                ui.element("div").style(state_strip_style(hm["state"]))
                                                with ui.column().classes("w-full p-3 gap-2"):
                                                    with ui.row().classes("w-full items-center justify-between"):
                                                        ui.label("Host Monitor").classes("text-xs uppercase tracking-widest text-zinc-400")
                                                        ui.label(hm["state"]).classes(f"text-[10px] uppercase tracking-[0.2em] px-2 py-1 rounded-full {state_badge_classes(hm['state'])}")
                                                    if hm.get("target"):
                                                        ui.label(str(hm["target"])).classes("text-xs text-zinc-500 break-all")
                                                    if hm.get("latest_error"):
                                                        ui.label(str(hm["latest_error"])[:64]).classes("text-xs text-rose-300")
                                                    # Service-size timeline, full width
                                                    ui.html(timeline_html(hm["timeline"], size="service")).classes("block w-full")
                                                    ui.html(timeline_scale_html(size="service")).classes("block w-full")

                                        # Service cards
                                        with ui.element("div").classes(service_grid_classes(host["service_count"])):
                                            for svc in host["services"]:
                                                with ui.card().classes(f"w-full p-0 overflow-hidden bg-white/5 border {state_card_classes(svc['state'])}"):
                                                    ui.element("div").style(state_strip_style(svc["state"]))
                                                    with ui.column().classes("w-full p-3 gap-2"):
                                                        with ui.row().classes("w-full items-start justify-between gap-2"):
                                                            with ui.column().classes("gap-0"):
                                                                ui.label(svc["display_name"]).classes("text-sm font-bold text-zinc-100")
                                                                target = svc.get("target") or svc.get("type") or "No target"
                                                                ui.label(str(target)).classes("text-xs text-zinc-500 break-all")
                                                            ui.label(svc["state"]).classes(f"text-[10px] uppercase tracking-[0.2em] px-2 py-1 rounded-full {state_badge_classes(svc['state'])}")
                                                        with ui.row().classes("w-full justify-between text-[11px] text-zinc-400"):
                                                            ui.label(f"24h {svc['uptime_24h']:.1f}%")
                                                            ui.label(f"all-time {svc['uptime_all']:.1f}%")
                                                        if svc.get("latest_error"):
                                                            ui.label(str(svc["latest_error"])[:56]).classes("text-xs text-rose-300")
                                                        # Service-size timeline, full width
                                                        ui.html(timeline_html(svc["timeline"], size="service")).classes("block w-full")
                                                        ui.html(timeline_scale_html(size="service")).classes("block w-full")
