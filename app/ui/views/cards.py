"""Cards view — full-width auto-fill grid of host cards.

Mimics the IaC Orchestrator Assignments tab
(lyndrix-plugin-iac-orchestrator/app/ui/dashboard.py:341-363):
``ui.grid(columns='repeat(auto-fill, minmax(350px, 1fr))')`` fills the
viewport edge-to-edge so 4k monitors are no longer wasted.
"""
from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui
from core.api import UIStyles

from .._components.group_header import render_group_header
from .._components.host_card import render_host_card
from ..helpers import build_grouped_overview
from ..prefs import MonitoringPrefs


def render_cards_view(
    ctx,
    svc,
    monitors: List[Dict[str, Any]],
    histories: Dict[str, List[Dict[str, Any]]],
    prefs: MonitoringPrefs,
) -> None:
    groups = build_grouped_overview(
        monitors,
        histories,
        group_by=prefs.group_by,
        include_paused=prefs.show_paused,
        include_unknown=prefs.show_unknown,
    )
    if not groups:
        with ui.card().classes(UIStyles.CARD_BASE + " w-full p-6 sm:p-8 text-center"):
            ui.icon("monitor_heart", size="48px").classes(UIStyles.ICON_MUTED + " mb-2")
            ui.label("No monitors match the current filters.").classes(UIStyles.TITLE_H2)
            ui.label(
                "Try enabling paused/unknown items or check the monitor registry."
            ).classes(UIStyles.TEXT_MUTED + " mt-1")
        return

    with ui.column().classes("w-full gap-6"):
        for group in groups:
            render_group_header(group, prefs)
            with ui.grid(
                columns="repeat(auto-fill, minmax(350px, 1fr))"
            ).classes("w-full gap-4"):
                for host in group["hosts"]:
                    render_host_card(host, prefs)
