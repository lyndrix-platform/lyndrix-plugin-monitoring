"""Split view — compact entity list on the left, detail card on the right.

Stacks vertically below the ``lg`` breakpoint so mobile gets a single
column with the detail panel scrolled into view on selection.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from nicegui import ui
from core.api import UIStyles

from .._components.host_card import render_host_card_detail
from ..helpers import build_grouped_overview, flatten_monitors
from ..prefs import MonitoringPrefs
from ..styles import state_badge_classes


def _find_host(groups: List[Dict[str, Any]], host_key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not host_key:
        return None
    for g in groups:
        for h in g["hosts"]:
            if h.get("name") == host_key or h.get("address") == host_key:
                return h
    return None


def render_split_view(
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
    groups = build_grouped_overview(
        monitors,
        histories,
        group_by=prefs.group_by,
        include_paused=prefs.show_paused,
        include_unknown=prefs.show_unknown,
    )
    if not rows:
        with ui.card().classes(UIStyles.CARD_BASE + " w-full p-6 text-center"):
            ui.label("No monitors match the current filters.").classes(UIStyles.TITLE_H2)
        return

    for row in rows:
        row["state_badge_cls"] = state_badge_classes(row["state"])
        row["uptime_24h_str"] = f"{row['uptime_24h']:.1f}%"

    initial = rows[0]["host_key"]

    with ui.element("div").classes(
        "w-full grid grid-cols-1 lg:grid-cols-[420px_1fr] gap-4"
    ):
        # ── Left pane: compact entity list ─────────────────────────────
        with ui.element("div").classes("w-full overflow-x-auto"):
            list_cols = [
                {"name": "name",   "label": "Name",   "field": "name",   "align": "left", "sortable": True},
                {"name": "host",   "label": "Host",   "field": "host",   "align": "left", "sortable": True},
                {"name": "state",  "label": "Status", "field": "state",  "align": "left", "sortable": True},
                {"name": "uptime_24h", "label": "24h", "field": "uptime_24h", "align": "right", "sortable": True},
            ]
            tbl = (
                ui.table(
                    columns=list_cols,
                    rows=rows,
                    row_key="monitor_id",
                    pagination={"rowsPerPage": 50},
                    selection="single",
                )
                .classes(f"w-full min-w-[420px] {UIStyles.CARD_BASE}")
                .props("dense flat")
            )
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
            tbl.add_slot(
                "body-cell-uptime_24h",
                r"""
                <q-td :props="props" class="text-right font-mono">{{ props.row.uptime_24h_str }}</q-td>
                """,
            )

        # ── Right pane: detail card ───────────────────────────────────
        detail_slot = ui.column().classes("w-full")

        def _render_detail(host_key: Optional[str]) -> None:
            detail_slot.clear()
            host = _find_host(groups, host_key)
            with detail_slot:
                if host is None:
                    with ui.card().classes(UIStyles.CARD_BASE + " w-full p-8 text-center"):
                        ui.icon("touch_app", size="48px").classes(UIStyles.ICON_MUTED + " mb-2")
                        ui.label("Select a row").classes(UIStyles.TITLE_H2)
                        ui.label("Click an entry on the left to see its timeline + services.").classes(
                            UIStyles.TEXT_MUTED + " mt-1"
                        )
                    return
                render_host_card_detail(host, prefs)

        def _on_select(e):
            picked = e.args[1] if len(e.args) > 1 else None
            host_key = (picked or {}).get("host_key") if isinstance(picked, dict) else None
            _render_detail(host_key)
            # Scroll the detail into view on mobile (below lg breakpoint).
            ui.run_javascript(
                "(() => { const el = document.getElementById('lx-mon-detail');"
                " if (el) el.scrollIntoView({behavior:'smooth', block:'start'}); })()"
            )

        tbl.on("rowClick", _on_select)
        _render_detail(initial)
