"""app/ui/page.py — Full monitoring page (wide layout + customization)."""

import asyncio

from nicegui import ui
from core.api import UIStyles

from .prefs import load_prefs
from .toolbar import render_toolbar
from .views.cards import render_cards_view
from .views.split import render_split_view
from .views.table import render_table_view


async def render_monitoring_page(ctx, svc):
    prefs = load_prefs()
    # Shared cache so prefs changes can re-render without re-fetching.
    data_cache: dict = {"monitors": [], "histories": {}, "loaded": False}

    # ── Centered header strip (max-w-7xl) ───────────────────────────────
    # The header card + stats + toolbar stay in a centered column so
    # control density doesn't feel lost on ultra-wide monitors.
    with ui.column().classes(
        "w-full max-w-7xl mx-auto gap-4 px-3 sm:px-4 lg:px-6"
    ):
        with ui.card().classes(UIStyles.CARD_GLASS + " w-full").style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(UIStyles.GRAD_BAR_ACCENT)
            with ui.column().classes("w-full p-4 sm:p-6 gap-3 sm:gap-4"):
                with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-0.5"):
                        ui.label("State Monitoring").classes(UIStyles.TITLE_H1)
                        ui.label(
                            "Persistent monitoring for servers and services with grouped status "
                            "timelines and optional IaC inventory sync."
                        ).classes(UIStyles.TEXT_MUTED)

                # Stats grid — 2 cols on mobile, 3 on sm, 5 on lg.
                stat_labels: dict = {}
                with ui.element("div").classes(
                    "grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 w-full gap-2 sm:gap-3"
                ):
                    for label, key, accent_cls in [
                        ("Monitors", "monitor_count", "text-cyan-300 dark:text-cyan-300"),
                        ("Up",       "up_count",      "text-emerald-400"),
                        ("Down",     "down_count",    "text-rose-400"),
                        ("Paused",   "paused_count",  "text-amber-400"),
                        ("Uptime",   "uptime_all",    "text-sky-300 dark:text-sky-300"),
                    ]:
                        with ui.card().classes(
                            UIStyles.CARD_BASE
                            + " p-3 sm:p-4 flex flex-col items-center justify-center text-center gap-0.5"
                        ):
                            ui.label(label).classes(UIStyles.LABEL_MINI)
                            stat_labels[key] = ui.label("—").classes(
                                f"text-2xl sm:text-3xl font-black {accent_cls} leading-none"
                            )

        # Customization toolbar
        render_toolbar(
            prefs,
            on_change=lambda _p: _rerender(),
            on_refresh=lambda: _force_refresh(),
        )

    # ── Full-bleed data area (NO max-w) ─────────────────────────────────
    data_container = ui.column().classes("w-full gap-4 px-3 sm:px-4 lg:px-6")

    # Loading spinner placeholder until first fetch resolves.
    with data_container:
        with ui.column().classes("w-full items-center justify-center py-16 sm:py-24 gap-4"):
            ui.spinner("dots", size="xl").classes("text-cyan-400")
            ui.label("Loading monitors…").classes(UIStyles.TEXT_MUTED)

    def _rerender() -> None:
        """Re-render the data area from cached data — no fetch."""
        if not data_cache["loaded"]:
            return
        data_container.clear()
        with data_container:
            _dispatch(prefs, data_cache["monitors"], data_cache["histories"])

    def _dispatch(p, monitors, histories) -> None:
        if p.view_mode == "table":
            render_table_view(ctx, svc, monitors, histories, p)
        elif p.view_mode == "split":
            render_split_view(ctx, svc, monitors, histories, p)
        else:
            render_cards_view(ctx, svc, monitors, histories, p)

    async def _fetch_and_render() -> None:
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
        data_cache["monitors"] = monitors_data
        data_cache["histories"] = histories_data
        data_cache["loaded"] = True
        _rerender()

    def _force_refresh() -> None:
        ui.timer(0, _fetch_and_render, once=True)

    ui.timer(0.05, _fetch_and_render, once=True)
    ui.timer(15.0, _fetch_and_render)
