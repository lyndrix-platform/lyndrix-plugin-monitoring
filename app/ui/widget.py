from nicegui import ui

from core.api import UIStyles

from ..controller.service import MonitoringService
from ..model.models import MonitorState
from .styles import state_badge_classes, state_color


def render_dashboard_widget(ctx, service: MonitoringService | None):
    if service is None:
        ui.label("State Monitoring").classes(UIStyles.TITLE_H3)
        ui.label("Unavailable").classes(UIStyles.TEXT_MUTED)
        return

    monitors = service.list_monitors()
    total   = len(monitors)
    up      = sum(1 for m in monitors if m.get("latest_state") == MonitorState.UP.value)
    down    = sum(1 for m in monitors if m.get("latest_state") == MonitorState.DOWN.value)
    paused  = sum(1 for m in monitors if m.get("latest_state") == MonitorState.PAUSED.value)
    unknown = total - up - down - paused

    overall     = "DOWN" if down else ("UP" if up else ("PAUSED" if paused else "UNKNOWN"))
    pulse_color = state_color(overall)

    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center gap-2"):
            ui.element("div").style(
                f"width:10px;height:10px;border-radius:50%;background:{pulse_color};"
                f"box-shadow:0 0 8px {pulse_color};"
            )
            ui.label("State Monitoring").classes(UIStyles.TITLE_H3)
        ui.label(overall).classes(
            "text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full "
            + state_badge_classes(overall)
        )

    ui.separator().classes("my-1 opacity-20")

    with ui.row().classes("w-full justify-between"):
        with ui.column().classes("items-center gap-0"):
            ui.label(str(total)).classes("text-2xl font-black text-slate-900 dark:text-zinc-50")
            ui.label("Total").classes(UIStyles.LABEL_MINI)
        with ui.column().classes("items-center gap-0"):
            ui.label(str(up)).classes("text-2xl font-black text-emerald-400")
            ui.label("Up").classes(UIStyles.LABEL_MINI)
        with ui.column().classes("items-center gap-0"):
            ui.label(str(down)).classes("text-2xl font-black text-rose-400")
            ui.label("Down").classes(UIStyles.LABEL_MINI)
        if paused:
            with ui.column().classes("items-center gap-0"):
                ui.label(str(paused)).classes("text-2xl font-black text-amber-400")
                ui.label("Paused").classes(UIStyles.LABEL_MINI)
        if unknown:
            with ui.column().classes("items-center gap-0"):
                ui.label(str(unknown)).classes("text-2xl font-black text-zinc-400")
                ui.label("Unknown").classes(UIStyles.LABEL_MINI)
