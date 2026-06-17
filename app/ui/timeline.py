from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ..model.models import MonitorState, _utc_now
from .styles import aggregate_state, state_color

# ------------------------------------------------------------------
# Size configuration — gap MUST be identical between bars and scale
# so both grids align perfectly.
# ------------------------------------------------------------------
_SIZE_CONFIG = {
    "full": {
        "heights": {MonitorState.UP.value: 80, MonitorState.DOWN.value: 40, MonitorState.PAUSED.value: 44, MonitorState.UNKNOWN.value: 20},
        "container_height": 88,
        "gap": "6px",
        "radius": "0 0 3px 3px",
        "tick_height": "8px",
        "scale_font": "10px",
        "scale_padding": "5px",
        "scale_margin": "3px",
    },
    "host": {
        "heights": {MonitorState.UP.value: 46, MonitorState.DOWN.value: 23, MonitorState.PAUSED.value: 26, MonitorState.UNKNOWN.value: 12},
        "container_height": 52,
        "gap": "4px",
        "radius": "0 0 2px 2px",
        "tick_height": "6px",
        "scale_font": "9px",
        "scale_padding": "3px",
        "scale_margin": "2px",
    },
    "service": {
        "heights": {MonitorState.UP.value: 30, MonitorState.DOWN.value: 15, MonitorState.PAUSED.value: 16, MonitorState.UNKNOWN.value: 6},
        "container_height": 34,
        "gap": "2px",
        "radius": "0 0 1px 1px",
        "tick_height": "4px",
        "scale_font": "8px",
        "scale_padding": "2px",
        "scale_margin": "1px",
    },
}


def timeline_html(timeline: List[str], size: str = "full") -> str:
    """Render a bar-chart timeline using CSS grid so it aligns with timeline_scale_html."""
    if not timeline:
        timeline = [MonitorState.UNKNOWN.value] * 24
    cfg = _SIZE_CONFIG.get(size, _SIZE_CONFIG["full"])
    heights = cfg["heights"]
    unknown_height = heights[MonitorState.UNKNOWN.value]
    total = len(timeline)
    gap = cfg["gap"]
    radius = cfg["radius"]
    container_height = cfg["container_height"]

    bars = []
    for index, state in enumerate(timeline):
        hour_label = f"-{total - index}h"
        height = heights.get(state, unknown_height)
        color = state_color(state)
        bars.append(
            f'<div title="{hour_label}: {state}" style="'
            f'height:{height}px;'
            f'border-radius:{radius};'
            f'background:{color};'
            f'opacity:0.95;'
            f'box-shadow:0 0 6px {color}22;'
            f'align-self:end;'
            f'"></div>'
        )

    return (
        f'<div style="'
        f'display:grid;'
        f'grid-template-columns:repeat({total},minmax(0,1fr));'
        f'gap:{gap};'
        f'width:100%;'
        f'height:{container_height}px;'
        f'box-sizing:border-box;'
        f'">'
        + "".join(bars)
        + "</div>"
    )


def timeline_scale_html(hours: int = 24, size: str = "full") -> str:
    """Render the time scale beneath a timeline. Uses the same grid+gap as timeline_html."""
    cfg = _SIZE_CONFIG.get(size, _SIZE_CONFIG["full"])
    gap = cfg["gap"]
    tick_h = cfg["tick_height"]
    font = cfg["scale_font"]
    padding = cfg["scale_padding"]
    margin = cfg["scale_margin"]

    tick_positions = {
        0: f"-{hours}h",
        max(0, (hours // 2) - 1): f"-{hours // 2}h",
        max(0, hours - 6 - 1): "-6h",
        hours - 1: "now",
    }

    ticks = []
    labels = []
    for i in range(hours):
        is_tick = i in tick_positions
        tick_color = "rgba(var(--lx-text-muted-raw,161,161,170),0.7)" if is_tick else "rgba(82,82,91,0.25)"
        ticks.append(
            f'<div style="'
            f'width:1px;'
            f'height:{tick_h};'
            f'margin:0 auto;'
            f'background:{tick_color};'
            f'"></div>'
        )
        label_text = tick_positions.get(i, "")
        if label_text:
            if i == 0:
                transform = "translateX(0%)"
            elif i == hours - 1:
                transform = "translateX(-100%)"
            else:
                transform = "translateX(-50%)"
            labels.append(
                f'<div style="'
                f'font-size:{font};'
                f'color:var(--lx-text-muted);'
                f'letter-spacing:0.1em;'
                f'white-space:nowrap;'
                f'transform:{transform};'
                f'">{label_text}</div>'
            )
        else:
            labels.append("<div></div>")

    grid_style = (
        f'display:grid;'
        f'grid-template-columns:repeat({hours},minmax(0,1fr));'
        f'gap:{gap};'
        f'width:100%;'
    )
    return (
        f'<div style="width:100%;padding-top:{padding};">'
        f'<div style="{grid_style}height:{tick_h};">'
        + "".join(ticks)
        + "</div>"
        f'<div style="{grid_style}margin-top:{margin};">'
        + "".join(labels)
        + "</div>"
        "</div>"
    )


# ------------------------------------------------------------------
# Timeline data helpers
# ------------------------------------------------------------------

def timeline_from_history(history: List[Dict[str, Any]], hours: int = 24) -> List[str]:
    start = _utc_now() - timedelta(hours=hours)
    buckets: List[List[str]] = [[] for _ in range(hours)]
    for item in history:
        raw_ts = item.get("timestamp")
        if not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if ts < start:
            continue
        idx = int((ts - start).total_seconds() // 3600)
        if 0 <= idx < hours:
            buckets[idx].append(item.get("state") or MonitorState.UNKNOWN.value)
    return [aggregate_state(bucket) if bucket else MonitorState.UNKNOWN.value for bucket in buckets]


def merge_timelines(timelines: List[List[str]]) -> List[str]:
    valid = [t for t in timelines if t]
    if not valid:
        return []
    return [
        aggregate_state([t[i] for t in valid if i < len(t)])
        for i in range(len(valid[0]))
    ]
