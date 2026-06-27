"""Legacy entry point — kept so the core dashboard widget surface
(``render_dashboard_widget`` → ``render_overview_ui``) keeps working.

New code should call ``views.cards.render_cards_view`` (or ``table`` /
``split``) directly. This wrapper just dispatches to the cards view with
the default prefs.
"""
from __future__ import annotations

import asyncio

from ..controller.service import MonitoringService
from .prefs import MonitoringPrefs
from .views.cards import render_cards_view


async def render_overview_ui(ctx, service: MonitoringService, *, monitors=None, histories=None):
    # list_monitors()/get_histories() run multi-table heartbeat aggregations.
    # The core dashboard renders plugin surfaces inline on the single asyncio
    # loop, so these must be offloaded or they stall every connected client
    # (NiceGUI "connection lost"). Callers may also pass pre-fetched data.
    if monitors is None:
        monitors = await asyncio.to_thread(service.list_monitors)
    if histories is None:
        histories = await asyncio.to_thread(
            service.get_histories, [m["monitor_id"] for m in monitors], 24
        )
    render_cards_view(ctx, service, monitors, histories, MonitoringPrefs())
