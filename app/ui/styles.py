from typing import List

from ..model.models import MonitorState

STATE_STYLES = {
    MonitorState.UP.value: {
        "badge": "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
        "card": "border-emerald-500/25 shadow-[0_0_0_1px_rgba(16,185,129,0.08)]",
        "accent": "#10b981",
    },
    MonitorState.DOWN.value: {
        "badge": "bg-rose-500/15 text-rose-300 border border-rose-500/30",
        "card": "border-rose-500/25 shadow-[0_0_0_1px_rgba(244,63,94,0.08)]",
        "accent": "#f43f5e",
    },
    MonitorState.PAUSED.value: {
        "badge": "bg-amber-500/15 text-amber-300 border border-amber-500/30",
        "card": "border-amber-500/25 shadow-[0_0_0_1px_rgba(245,158,11,0.08)]",
        "accent": "#f59e0b",
    },
    MonitorState.UNKNOWN.value: {
        "badge": "bg-sky-500/15 text-sky-300 border border-sky-500/30",
        "card": "border-sky-500/25 shadow-[0_0_0_1px_rgba(14,165,233,0.08)]",
        "accent": "#0ea5e9",
    },
}

_DEFAULT_STYLE = STATE_STYLES[MonitorState.UNKNOWN.value]


def state_badge_classes(state: str) -> str:
    return STATE_STYLES.get(state, _DEFAULT_STYLE)["badge"]


def state_card_classes(state: str) -> str:
    return STATE_STYLES.get(state, _DEFAULT_STYLE)["card"]


def state_color(state: str) -> str:
    return STATE_STYLES.get(state, _DEFAULT_STYLE)["accent"]


def state_strip_style(state: str) -> str:
    color = state_color(state)
    return f"height:4px;width:100%;background:{color};box-shadow:0 0 18px {color}66"


def aggregate_state(states: List[str]) -> str:
    filtered = [s for s in states if s]
    if not filtered:
        return MonitorState.UNKNOWN.value
    if MonitorState.DOWN.value in filtered:
        return MonitorState.DOWN.value
    if MonitorState.UNKNOWN.value in filtered:
        return MonitorState.UNKNOWN.value
    if MonitorState.UP.value in filtered:
        return MonitorState.UP.value
    if MonitorState.PAUSED.value in filtered:
        return MonitorState.PAUSED.value
    return MonitorState.UNKNOWN.value
