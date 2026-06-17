"""User-scoped UI preferences for the monitoring dashboard.

Prefs are persisted to NiceGUI's per-user session storage via
``core.session.get_user_value`` / ``set_user_value`` (one key per field,
namespaced ``monitoring.*``). Mirrors the storage style used for
``theme_pref`` / ``theme_id`` in ``lyndrix-core/app/ui/layout.py``.

TODO(persistence): Today prefs live in ``app.storage.user`` (per-browser
session — cleared on logout/cookie wipe). Replace with a per-user DB
table so settings survive across devices/browsers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Literal

from core.session import get_user_value, set_user_value

ViewMode = Literal["cards", "table", "split"]
GroupBy = Literal["site", "stage", "location", "status", "flat"]
Density = Literal["compact", "cozy", "spacious"]

_PREFIX = "monitoring."


@dataclass
class MonitoringPrefs:
    view_mode: ViewMode = "cards"
    group_by: GroupBy = "site"
    density: Density = "cozy"
    show_timelines: bool = True
    show_uptime_24h: bool = True
    show_uptime_all: bool = True
    show_services_in_host: bool = True
    show_paused: bool = True
    show_unknown: bool = True


def load_prefs() -> MonitoringPrefs:
    p = MonitoringPrefs()
    for f in fields(p):
        setattr(p, f.name, get_user_value(_PREFIX + f.name, getattr(p, f.name)))
    return p


def save_prefs(p: MonitoringPrefs) -> None:
    for k, v in asdict(p).items():
        set_user_value(_PREFIX + k, v)


def update_pref(key: str, value) -> None:
    set_user_value(_PREFIX + key, value)
