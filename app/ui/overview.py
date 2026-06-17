"""Legacy entry point — kept so the core dashboard widget surface
(``render_dashboard_widget`` → ``render_overview_ui``) keeps working.

New code should call ``views.cards.render_cards_view`` (or ``table`` /
``split``) directly. This wrapper just dispatches to the cards view with
the default prefs.
"""
from __future__ import annotations

from ..controller.service import MonitoringService
from .prefs import MonitoringPrefs
from .views.cards import render_cards_view


def render_overview_ui(ctx, service: MonitoringService, *, monitors=None, histories=None):
    if monitors is None:
        monitors = service.list_monitors()
    if histories is None:
        histories = service.get_histories(
            [m["monitor_id"] for m in monitors], limit_hours=24
        )
    render_cards_view(ctx, service, monitors, histories, MonitoringPrefs())
