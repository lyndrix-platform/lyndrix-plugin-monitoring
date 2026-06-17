"""Dense table view — one row per monitor.

Mirrors the user's reference list shape: name | type | location |
status | uptime, with optional sparkline. Horizontal scroll on mobile
via ``overflow-x-auto`` + ``min-w-[720px]`` so the table never breaks
the surrounding layout.

TODO(scale): if monitor count grows past ~300, switch to server-side
pagination via a paginated ``svc.list_monitors_paginated(offset, limit)``
helper instead of dumping everything client-side.
"""
from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui
from core.api import UIStyles

from ..helpers import flatten_monitors
from ..prefs import MonitoringPrefs
from ..styles import state_badge_classes
from ..timeline import timeline_html


_DENSITY_ROW_CLS = {
    "compact": "text-xs",
    "cozy": "text-sm",
    "spacious": "text-sm py-2",
}


def _columns(prefs: MonitoringPrefs) -> list[dict]:
    cols: list[dict] = [
        {"name": "name",     "label": "Name",     "field": "name",     "align": "left",  "sortable": True},
        {"name": "type",     "label": "Type",     "field": "type",     "align": "left",  "sortable": True},
        {"name": "location", "label": "Location", "field": "location", "align": "left",  "sortable": True},
        {"name": "host",     "label": "Host",     "field": "host",     "align": "left",  "sortable": True},
        {"name": "state",    "label": "Status",   "field": "state",    "align": "left",  "sortable": True},
    ]
    if prefs.show_uptime_24h:
        cols.append({"name": "uptime_24h", "label": "24h",      "field": "uptime_24h", "align": "right", "sortable": True})
    if prefs.show_uptime_all:
        cols.append({"name": "uptime_all", "label": "All-time", "field": "uptime_all", "align": "right", "sortable": True})
    if prefs.show_timelines:
        cols.append({"name": "sparkline", "label": "Timeline", "field": "timeline", "align": "left", "sortable": False})
    return cols


def render_table_view(
    ctx,
    svc,
    monitors: List[Dict[str, Any]],
    histories: Dict[str, List[Dict[str, Any]]],
    prefs: MonitoringPrefs,
) -> None:
    rows = flatten_monitors(
        monitors,
        histories,
        include_paused=prefs.show_paused,
        include_unknown=prefs.show_unknown,
    )
    if not rows:
        with ui.card().classes(UIStyles.CARD_BASE + " w-full p-6 text-center"):
            ui.label("No monitors match the current filters.").classes(UIStyles.TITLE_H2)
        return

    # Pre-compute per-row HTML + badge classes so the Vue slot templates
    # below can bind them directly without calling back into Python.
    for row in rows:
        row["state_badge_cls"] = state_badge_classes(row["state"])
        row["sparkline_html"] = (
            timeline_html(row["timeline"], size="service") if prefs.show_timelines else ""
        )

    row_cls = _DENSITY_ROW_CLS.get(prefs.density, _DENSITY_ROW_CLS["cozy"])
    cols = _columns(prefs)

    with ui.element("div").classes("w-full overflow-x-auto"):
        tbl = (
            ui.table(
                columns=cols,
                rows=rows,
                row_key="monitor_id",
                pagination={"rowsPerPage": 50},
            )
            .classes(f"w-full min-w-[720px] {UIStyles.CARD_BASE} {row_cls}")
            .props("dense flat")
        )
        # Status cell — rendered as a coloured badge.
        tbl.add_slot(
            "body-cell-state",
            r"""
            <q-td :props="props">
                <span :class="'text-[10px] uppercase tracking-[0.18em] px-2 py-0.5 rounded-full ' + (props.row.state_badge_cls || '')">
                    {{ props.row.state }}
                </span>
            </q-td>
            """,
        )
        # Uptime cells — format as percent with one decimal.
        tbl.add_slot(
            "body-cell-uptime_24h",
            r"""
            <q-td :props="props" class="text-right font-mono">{{ Number(props.row.uptime_24h).toFixed(1) }}%</q-td>
            """,
        )
        tbl.add_slot(
            "body-cell-uptime_all",
            r"""
            <q-td :props="props" class="text-right font-mono">{{ Number(props.row.uptime_all).toFixed(1) }}%</q-td>
            """,
        )
        # Sparkline cell — render the same timeline HTML used by cards.
        tbl.add_slot(
            "body-cell-sparkline",
            r"""
            <q-td :props="props">
                <span v-html="props.row.sparkline_html"></span>
            </q-td>
            """,
        )
