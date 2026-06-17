"""Reusable host-card renderers.

Used by the Cards view (compact, services rendered inline) and by the
Split view's detail pane (large, services as a vertical list, full-size
timeline). Card chrome mimics the IaC Orchestrator Assignments card
(top accent stripe + body column).

TODO(api-surface): If/when core.api re-exports IaC's tile() /
section_header() helpers from
``lyndrix-plugin-iac-orchestrator/app/ui/components.py``, replace the
inline chrome below with them. core.api today re-exports only UIStyles,
so duplicating ~6 lines of chrome is preferable to cross-plugin imports.
"""
from __future__ import annotations

from typing import Any, Dict

from nicegui import ui
from core.api import UIStyles

from ..prefs import MonitoringPrefs
from ..styles import state_badge_classes, state_card_classes, state_strip_style
from ..timeline import timeline_html, timeline_scale_html

# Density → inner padding/gap classes
_PAD = {
    "compact": "p-2 gap-1.5",
    "cozy": "p-4 gap-3",
    "spacious": "p-5 gap-4",
}
_TITLE_SIZE = {
    "compact": "text-sm",
    "cozy": "text-base",
    "spacious": "text-lg",
}


def _pad(density: str) -> str:
    return _PAD.get(density, _PAD["cozy"])


def _title_size(density: str) -> str:
    return _TITLE_SIZE.get(density, _TITLE_SIZE["cozy"])


def _header_row(host: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    with ui.row().classes("w-full items-start justify-between gap-2"):
        with ui.column().classes("gap-0 min-w-0 flex-1"):
            ui.label(host["name"]).classes(
                f"{_title_size(prefs.density)} font-black "
                "text-slate-900 dark:text-zinc-50 truncate"
            )
            with ui.row().classes("items-center gap-2 min-w-0"):
                if host.get("address"):
                    ui.label(host["address"]).classes(
                        UIStyles.TEXT_MUTED + " font-mono text-xs truncate"
                    )
                if host.get("stage") and host["stage"] != "General":
                    ui.label(host["stage"]).classes(
                        "text-[10px] uppercase tracking-[0.18em] px-1.5 py-0.5 rounded-full "
                        "bg-slate-500/10 text-slate-500 dark:text-slate-300 "
                        "border border-slate-500/20 shrink-0"
                    )
        ui.label(host["state"]).classes(
            f"text-[10px] uppercase tracking-[0.2em] px-2 py-0.5 rounded-full shrink-0 "
            f"{state_badge_classes(host['state'])}"
        )


def _meta_row(host: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    has_services = bool(host.get("services"))
    parts_left = (
        "Standalone host"
        if not has_services
        else f"{host['service_count']} service" + ("s" if host['service_count'] != 1 else "")
    )
    parts_right = []
    if prefs.show_uptime_24h:
        parts_right.append(f"24h {host['uptime_24h']:.1f}%")
    if prefs.show_uptime_all:
        parts_right.append(f"all-time {host['uptime_all']:.1f}%")
    with ui.row().classes("w-full justify-between items-center"):
        ui.label(parts_left).classes(UIStyles.TEXT_MUTED + " text-xs")
        if parts_right:
            ui.label(" · ".join(parts_right)).classes(UIStyles.TEXT_MUTED + " text-xs")


def _services_inline(host: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    if not host.get("services"):
        return
    with ui.column().classes("w-full gap-1.5"):
        for svc in host["services"]:
            with ui.row().classes(
                "w-full items-center gap-2 border-t border-slate-200/40 "
                "dark:border-zinc-800/60 pt-1.5"
            ):
                ui.label(svc["display_name"]).classes(
                    "text-xs font-bold text-slate-800 dark:text-zinc-100 truncate flex-1"
                )
                if prefs.show_uptime_24h:
                    ui.label(f"{svc['uptime_24h']:.1f}%").classes(
                        UIStyles.TEXT_MUTED + " text-[11px] font-mono shrink-0"
                    )
                ui.label(svc["state"]).classes(
                    f"text-[9px] uppercase tracking-[0.18em] px-1.5 py-0.5 rounded-full shrink-0 "
                    f"{state_badge_classes(svc['state'])}"
                )


def render_host_card(host: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    """Compact host card for the Cards view."""
    with ui.card().classes(
        f"flex flex-col gap-0 border {state_card_classes(host['state'])} "
        "bg-white/60 dark:bg-white/[0.03] backdrop-blur-sm "
        "hover:border-indigo-500/40 transition-all"
    ).style("padding:0; flex-wrap:nowrap; min-width:0"):
        ui.element("div").style(state_strip_style(host["state"]))
        with ui.column().classes(f"w-full flex-grow {_pad(prefs.density)}"):
            _header_row(host, prefs)
            _meta_row(host, prefs)
            if prefs.show_timelines:
                ui.html(timeline_html(host["timeline"], size="host")).classes(
                    "block w-full"
                )
                ui.html(timeline_scale_html(size="host")).classes("block w-full")
            if prefs.show_services_in_host:
                _services_inline(host, prefs)


def render_host_card_detail(host: Dict[str, Any], prefs: MonitoringPrefs) -> None:
    """Expanded host card for the Split view's right pane (full-size timeline + services)."""
    with ui.card().props('id="lx-mon-detail"').classes(
        f"flex flex-col gap-0 border {state_card_classes(host['state'])} "
        "bg-white/60 dark:bg-white/[0.03] backdrop-blur-sm w-full"
    ).style("padding:0; flex-wrap:nowrap; min-width:0"):
        ui.element("div").style(state_strip_style(host["state"]))
        with ui.column().classes("w-full flex-grow p-5 gap-4"):
            _header_row(host, prefs)
            _meta_row(host, prefs)
            ui.html(timeline_html(host["timeline"], size="full")).classes(
                "block w-full"
            )
            ui.html(timeline_scale_html(size="full")).classes("block w-full")
            if host.get("host_monitor"):
                hm = host["host_monitor"]
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Host monitor").classes(UIStyles.LABEL_MINI)
                    ui.label(hm["state"]).classes(
                        f"text-[10px] uppercase tracking-[0.18em] px-1.5 py-0.5 rounded-full "
                        f"{state_badge_classes(hm['state'])}"
                    )
                    if hm.get("target"):
                        ui.label(str(hm["target"])).classes(
                            UIStyles.TEXT_MUTED + " text-xs font-mono truncate"
                        )
            if host.get("services"):
                ui.separator().classes("border-slate-200/60 dark:border-white/5")
                _services_inline(host, prefs)
