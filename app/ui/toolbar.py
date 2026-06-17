"""Customization toolbar for the monitoring dashboard.

Renders a single control strip with view-mode buttons, group-by select,
density select, a visibility menu (one switch per toggleable section),
and a refresh button. Each control mutates ``prefs`` in place, writes
the change to ``app.storage.user`` via ``update_pref``, then invokes
``on_change(prefs)`` so the page can re-render the data area without a
fetch.
"""
from __future__ import annotations

from typing import Callable

from nicegui import ui
from core.api import UIStyles

from .prefs import MonitoringPrefs, update_pref


_VIEW_MODE_LABELS = [
    ("cards", "Cards",  "dashboard"),
    ("table", "Table",  "table_rows"),
    ("split", "Split",  "splitscreen"),
]
_GROUP_BY_OPTIONS = {
    "site":     "Site",
    "stage":    "Stage",
    "location": "Location",
    "status":   "Status",
    "flat":     "Flat (no grouping)",
}
_DENSITY_OPTIONS = {
    "compact":  "Compact",
    "cozy":     "Cozy",
    "spacious": "Spacious",
}


def render_toolbar(
    prefs: MonitoringPrefs,
    on_change: Callable[[MonitoringPrefs], None],
    on_refresh: Callable[[], None],
) -> None:
    """Wrap the call site in a ``max-w-7xl mx-auto`` row to keep the
    controls centered with the header card above."""

    def _set(field: str, value):
        setattr(prefs, field, value)
        update_pref(field, value)
        on_change(prefs)

    with ui.row().classes(
        "w-full items-center gap-2 sm:gap-3 flex-wrap py-1"
    ):
        # ── View mode ────────────────────────────────────────────────
        view_buttons: dict[str, ui.button] = {}

        def _view_btn_cls(value: str) -> str:
            is_active = prefs.view_mode == value
            return (
                "px-2.5 py-1 rounded-md text-xs font-bold "
                + ("bg-cyan-500/15 text-cyan-300 border border-cyan-500/30"
                   if is_active else "text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-100")
            )

        def _select_view(value: str) -> None:
            _set("view_mode", value)
            # Restyle every button so the active highlight follows the
            # selection — the toolbar itself is not re-rendered on change.
            for v, btn in view_buttons.items():
                btn.classes(replace=_view_btn_cls(v))

        with ui.row().classes("items-center gap-1 p-1 rounded-lg "
                              "bg-slate-100 dark:bg-zinc-900/60 "
                              "border border-slate-200 dark:border-zinc-800"):
            for value, label, icon in _VIEW_MODE_LABELS:
                view_buttons[value] = ui.button(
                    label, icon=icon,
                    on_click=lambda _, v=value: _select_view(v)) \
                    .props("flat dense no-caps") \
                    .classes(_view_btn_cls(value))

        # ── Group-by ─────────────────────────────────────────────────
        ui.select(
            options=_GROUP_BY_OPTIONS,
            value=prefs.group_by,
            label="Group by",
            on_change=lambda e: _set("group_by", e.value),
        ).props("dense outlined").classes("min-w-[140px]")

        # ── Density ──────────────────────────────────────────────────
        ui.select(
            options=_DENSITY_OPTIONS,
            value=prefs.density,
            label="Density",
            on_change=lambda e: _set("density", e.value),
        ).props("dense outlined").classes("min-w-[130px]")

        ui.space()

        # ── Visibility menu ──────────────────────────────────────────
        with ui.button(icon="visibility").props("flat round dense") \
                .tooltip("Show / hide sections"):
            with ui.menu().classes("p-2"):
                with ui.column().classes("gap-1 min-w-[220px]"):
                    ui.label("Show columns & sections").classes(UIStyles.LABEL_MINI)
                    _switch_row("Timelines",         prefs, "show_timelines",        _set)
                    _switch_row("24h uptime",        prefs, "show_uptime_24h",       _set)
                    _switch_row("All-time uptime",   prefs, "show_uptime_all",       _set)
                    _switch_row("Services in host",  prefs, "show_services_in_host", _set)
                    ui.separator()
                    _switch_row("Paused monitors",   prefs, "show_paused",           _set)
                    _switch_row("Unknown monitors",  prefs, "show_unknown",          _set)

        ui.button(icon="refresh", on_click=lambda: on_refresh()) \
            .props("flat round dense").tooltip("Refresh now")


def _switch_row(label: str, prefs: MonitoringPrefs, field: str, setter: Callable[[str, object], None]) -> None:
    with ui.row().classes("w-full items-center justify-between gap-3"):
        ui.label(label).classes("text-xs text-slate-700 dark:text-zinc-200")
        ui.switch(value=bool(getattr(prefs, field)),
                  on_change=lambda e, f=field: setter(f, bool(e.value))) \
            .props("dense color=cyan")
