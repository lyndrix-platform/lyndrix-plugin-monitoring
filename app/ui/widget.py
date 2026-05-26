from nicegui import ui

from ..controller.service import MonitoringService
from ..model.models import MonitorState


def render_dashboard_widget(ctx, service: MonitoringService | None):
    if service is None:
        with ui.card().classes("w-full p-4 bg-zinc-950/70 border border-zinc-800"):
            ui.label("State Monitoring").classes("text-sm font-bold text-zinc-400")
            ui.label("Unavailable").classes("text-xs text-zinc-500")
        return

    monitors = service.list_monitors()
    total = len(monitors)
    up = sum(1 for m in monitors if m.get("latest_state") == MonitorState.UP.value)
    down = sum(1 for m in monitors if m.get("latest_state") == MonitorState.DOWN.value)
    paused = sum(1 for m in monitors if m.get("latest_state") == MonitorState.PAUSED.value)
    unknown = total - up - down - paused

    overall = "DOWN" if down else ("UP" if up else ("PAUSED" if paused else "UNKNOWN"))
    pulse_color = {
        "UP": "#22c55e",
        "DOWN": "#f43f5e",
        "PAUSED": "#f59e0b",
        "UNKNOWN": "#71717a",
    }.get(overall, "#71717a")

    with ui.card().classes("w-full p-4 bg-gradient-to-br from-zinc-950 to-zinc-900 border border-zinc-800"):
        with ui.row().classes("w-full items-center justify-between mb-3"):
            with ui.row().classes("items-center gap-2"):
                ui.element("div").style(
                    f"width:10px;height:10px;border-radius:50%;background:{pulse_color};"
                    f"box-shadow:0 0 8px {pulse_color};"
                )
                ui.label("State Monitoring").classes("text-sm font-bold text-zinc-100")
            ui.label(overall).classes(
                "text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full "
                + (
                    "bg-emerald-500/15 text-emerald-400 border border-emerald-500/25"
                    if overall == "UP"
                    else "bg-rose-500/15 text-rose-400 border border-rose-500/25"
                    if overall == "DOWN"
                    else "bg-amber-500/15 text-amber-400 border border-amber-500/25"
                    if overall == "PAUSED"
                    else "bg-zinc-700/50 text-zinc-400 border border-zinc-600/50"
                )
            )
        with ui.row().classes("w-full justify-between"):
            with ui.column().classes("items-center gap-0"):
                ui.label(str(total)).classes("text-2xl font-black text-zinc-50")
                ui.label("Total").classes("text-[10px] text-zinc-500")
            with ui.column().classes("items-center gap-0"):
                ui.label(str(up)).classes("text-2xl font-black text-emerald-400")
                ui.label("Up").classes("text-[10px] text-zinc-500")
            with ui.column().classes("items-center gap-0"):
                ui.label(str(down)).classes("text-2xl font-black text-rose-400")
                ui.label("Down").classes("text-[10px] text-zinc-500")
            if paused:
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(paused)).classes("text-2xl font-black text-amber-400")
                    ui.label("Paused").classes("text-[10px] text-zinc-500")
            if unknown:
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(unknown)).classes("text-2xl font-black text-zinc-500")
                    ui.label("Unknown").classes("text-[10px] text-zinc-500")
