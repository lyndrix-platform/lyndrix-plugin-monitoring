"""Shared group-header row used by every monitoring view."""
from __future__ import annotations

from typing import Any, Dict

from nicegui import ui
from core.api import UIStyles

from ..prefs import MonitoringPrefs
from ..styles import state_badge_classes


def render_group_header(group: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    """One full-width row: icon + title + state badge + uptime suffix.

    No-ops when ``group['name'] is None`` (flat-mode synthetic group).
    """
    if group.get("name") is None:
        return
    with ui.row().classes(
        "w-full items-center gap-3 border-b border-slate-200/60 dark:border-zinc-800 pb-2"
    ):
        ui.icon(group.get("icon", "domain"), size="22px").classes("text-slate-400")
        ui.label(str(group["name"])).classes(
            "text-lg font-black tracking-wider text-slate-800 dark:text-slate-200 truncate"
        )
        ui.label(
            f"{group['host_count']} hosts · {group['service_count']} services"
        ).classes(UIStyles.TEXT_MUTED + " text-xs")
        ui.space()
        ui.label(group["state"]).classes(
            f"text-[11px] uppercase tracking-[0.2em] px-2.5 py-0.5 rounded-full "
            f"{state_badge_classes(group['state'])}"
        )
        if prefs.show_uptime_24h or prefs.show_uptime_all:
            parts = []
            if prefs.show_uptime_24h:
                parts.append(f"24h {group['uptime_24h']:.1f}%")
            if prefs.show_uptime_all:
                parts.append(f"all-time {group['uptime_all']:.1f}%")
            ui.label(" · ".join(parts)).classes(UIStyles.TEXT_MUTED + " text-xs")
