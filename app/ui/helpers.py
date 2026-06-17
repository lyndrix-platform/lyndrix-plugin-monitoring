from typing import Any, Callable, Dict, List, Optional, Tuple

from ..model.models import MonitorState, MonitorType, _aggregate_uptime_percent, _timeline_uptime_percent
from .styles import aggregate_state
from .timeline import merge_timelines, timeline_from_history


def humanize_label(value: Optional[str]) -> str:
    if not value:
        return "Ungrouped"
    chunks = str(value).replace("/", " ").replace("_", " ").replace("-", " ").split()
    normalized = []
    for chunk in chunks:
        if chunk.isupper() or chunk.isdigit():
            normalized.append(chunk)
        elif len(chunk) <= 3 and chunk.isalpha():
            normalized.append(chunk.upper())
        else:
            normalized.append(chunk.capitalize())
    return " ".join(normalized)


def infer_site_and_stage(monitor: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    metadata = monitor.get("metadata") or {}
    groups = metadata.get("groups") or []
    site = metadata.get("site")
    stage = metadata.get("stage")

    if not site:
        site = next(
            (g.split("site_", 1)[1] for g in groups if isinstance(g, str) and g.startswith("site_")),
            None,
        )
    if not stage:
        stage = next(
            (g.split("stage_", 1)[1] for g in groups if isinstance(g, str) and g.startswith("stage_")),
            None,
        )
    if not site:
        logical_group = monitor.get("logical_group") or "ungrouped"
        site = logical_group if logical_group not in {"iac", "manual", "default"} else "ungrouped"

    return humanize_label(site), humanize_label(stage) if stage else None


def service_display_name(monitor: Dict[str, Any]) -> str:
    service_name = monitor.get("service_name")
    if service_name:
        return humanize_label(service_name)
    return humanize_label(monitor.get("name") or monitor.get("monitor_id"))


def host_display_name(monitor: Dict[str, Any]) -> str:
    return monitor.get("host_name") or monitor.get("address") or humanize_label(monitor.get("name") or monitor.get("monitor_id"))


def infer_location(monitor: Dict[str, Any]) -> str:
    """Best-effort location label: metadata.location, then a ``loc_*`` / ``location_*`` group prefix."""
    metadata = monitor.get("metadata") or {}
    location = metadata.get("location")
    if not location:
        groups = metadata.get("groups") or []
        for g in groups:
            if not isinstance(g, str):
                continue
            for prefix in ("location_", "loc_"):
                if g.startswith(prefix):
                    location = g[len(prefix):]
                    break
            if location:
                break
    return humanize_label(location) if location else "Unknown"


def _filter_monitors(
    monitors: List[Dict[str, Any]],
    *,
    include_paused: bool,
    include_unknown: bool,
) -> List[Dict[str, Any]]:
    out = []
    for m in monitors:
        state = (m.get("latest_state") or "UNKNOWN").upper()
        if not include_paused and state == MonitorState.PAUSED.value:
            continue
        if not include_unknown and state == MonitorState.UNKNOWN.value:
            continue
        out.append(m)
    return out


def _group_key(monitor: Dict[str, Any], group_by: str) -> Optional[str]:
    """Return the group-name a monitor belongs to under the selected axis.

    ``flat`` returns ``None`` so the caller emits a single synthetic group
    with no header.
    """
    if group_by == "flat":
        return None
    if group_by == "stage":
        _, stage = infer_site_and_stage(monitor)
        return stage or "General"
    if group_by == "location":
        return infer_location(monitor)
    if group_by == "status":
        return (monitor.get("latest_state") or "UNKNOWN").upper()
    site, _ = infer_site_and_stage(monitor)
    return site


_GROUP_ICONS = {
    "site": "domain",
    "stage": "layers",
    "location": "public",
    "status": "monitor_heart",
    "flat": "view_module",
}


def build_grouped_overview(
    monitors: List[Dict[str, Any]],
    histories: Dict[str, List[Dict[str, Any]]],
    *,
    group_by: str = "site",
    include_paused: bool = True,
    include_unknown: bool = True,
) -> List[Dict[str, Any]]:
    """Group monitors into ``[{name, hosts:[…]}]`` along the requested axis.

    Today's two-level site → stage tree collapses into a one-level
    group → host layout. The stage label is preserved on each host for
    the card subtitle so users grouping by site still see it.
    """
    monitors = _filter_monitors(
        monitors, include_paused=include_paused, include_unknown=include_unknown
    )
    sites: Dict[Optional[str], Dict[str, Any]] = {}

    for monitor in monitors:
        site_name, stage_name = infer_site_and_stage(monitor)
        stage_key = stage_name or "General"
        group_name = _group_key(monitor, group_by)
        site_entry = sites.setdefault(group_name, {"name": group_name, "stages": {}})
        stage_entry = site_entry["stages"].setdefault(stage_key, {"name": stage_key, "hosts": {}})

        host_key = monitor.get("host_name") or monitor.get("address") or monitor.get("monitor_id")
        host_entry = stage_entry["hosts"].setdefault(
            host_key,
            {
                "key": host_key,
                "name": host_display_name(monitor),
                "address": monitor.get("address"),
                "host_monitor": None,
                "services": [],
                "timelines": [],
                "states": [],
                "uptimes": [],
                "uptimes_all": [],
            },
        )

        history = histories.get(monitor["monitor_id"], [])
        timeline = timeline_from_history(history)
        state = monitor.get("latest_state") or "UNKNOWN"
        host_entry["states"].append(state)
        host_entry["timelines"].append(timeline)
        host_entry["uptimes"].append(float(monitor.get("uptime_24h") or 0.0))
        host_entry["uptimes_all"].append(float(monitor.get("uptime_all") or 0.0))
        host_entry["address"] = host_entry.get("address") or monitor.get("address")

        monitor_view = {
            "monitor_id": monitor["monitor_id"],
            "name": monitor.get("name"),
            "display_name": service_display_name(monitor),
            "state": state,
            "uptime_24h": float(monitor.get("uptime_24h") or 0.0),
            "uptime_all": float(monitor.get("uptime_all") or 0.0),
            "timeline": timeline,
            "target": monitor.get("target") or monitor.get("address"),
            "type": monitor.get("monitor_type"),
            "latest_error": monitor.get("latest_error"),
        }

        if monitor.get("monitor_type") == MonitorType.SERVER.value and monitor.get("host_name"):
            host_entry["host_monitor"] = monitor_view
        else:
            host_entry["services"].append(monitor_view)

    icon = _GROUP_ICONS.get(group_by, "domain")
    grouped = []
    for site_entry in sites.values():
        host_list = []
        host_states = []
        host_timelines = []
        host_uptimes_all = []

        for stage_entry in site_entry["stages"].values():
            for host_entry in stage_entry["hosts"].values():
                agg_state = aggregate_state(host_entry["states"])
                agg_timeline = merge_timelines(host_entry["timelines"])
                avg_uptime = _timeline_uptime_percent(agg_timeline)
                avg_uptime_all = _aggregate_uptime_percent(
                    host_entry.get("uptimes_all") or [100.0]
                )
                services = sorted(
                    host_entry["services"], key=lambda i: (i["state"], i["display_name"])
                )
                host_list.append({
                    "name": host_entry["name"],
                    "address": host_entry.get("address"),
                    "stage": stage_entry["name"],
                    "state": agg_state,
                    "timeline": agg_timeline,
                    "uptime_24h": avg_uptime,
                    "uptime_all": avg_uptime_all,
                    "host_monitor": host_entry.get("host_monitor"),
                    "services": services,
                    "service_count": len(services),
                })
                host_states.append(agg_state)
                host_timelines.append(agg_timeline)
                host_uptimes_all.append(avg_uptime_all)

        host_list.sort(key=lambda i: (i["state"], i["name"]))
        group_timeline = merge_timelines(host_timelines)
        grouped.append({
            "name": site_entry["name"],
            "icon": icon,
            "state": aggregate_state(host_states),
            "timeline": group_timeline,
            "uptime_24h": _timeline_uptime_percent(group_timeline),
            "uptime_all": (
                _aggregate_uptime_percent(host_uptimes_all) if host_uptimes_all else 100.0
            ),
            "hosts": host_list,
            "host_count": len(host_list),
            "service_count": sum(h["service_count"] for h in host_list),
        })

    # ``flat`` produces one group with name=None; sort gracefully.
    return sorted(grouped, key=lambda i: (i["name"] is None, str(i["name"] or "")))


def flatten_monitors(
    monitors: List[Dict[str, Any]],
    histories: Dict[str, List[Dict[str, Any]]],
    *,
    include_paused: bool = True,
    include_unknown: bool = True,
) -> List[Dict[str, Any]]:
    """One row per monitor — feeds table and split views.

    Mirrors the column shape the user wants to see (name | type | location |
    status | uptime), plus enough auxiliary fields (host, site, stage,
    timeline, latest_error) for filtering and the split-view detail card.
    """
    monitors = _filter_monitors(
        monitors, include_paused=include_paused, include_unknown=include_unknown
    )
    rows = []
    for m in monitors:
        site, stage = infer_site_and_stage(m)
        history = histories.get(m["monitor_id"], [])
        timeline = timeline_from_history(history)
        host_key = m.get("host_name") or m.get("address") or m.get("monitor_id")
        rows.append({
            "monitor_id": m["monitor_id"],
            "name": service_display_name(m) if (m.get("monitor_type") != MonitorType.SERVER.value or m.get("service_name")) else host_display_name(m),
            "type": m.get("monitor_type") or "monitor",
            "location": infer_location(m),
            "host": host_key,
            "host_key": host_key,
            "site": site,
            "stage": stage or "General",
            "state": (m.get("latest_state") or "UNKNOWN").upper(),
            "uptime_24h": float(m.get("uptime_24h") or 0.0),
            "uptime_all": float(m.get("uptime_all") or 0.0),
            "timeline": timeline,
            "target": m.get("target") or m.get("address"),
            "latest_error": m.get("latest_error"),
        })
    rows.sort(key=lambda r: (r["site"], r["host"], r["type"] != "server", r["name"]))
    return rows
