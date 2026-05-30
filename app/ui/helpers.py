from typing import Any, Dict, List, Optional, Tuple

from ..model.models import MonitorType, _aggregate_uptime_percent, _timeline_uptime_percent
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


def host_tile_span_classes(service_count: int) -> str:
    if service_count >= 10:
        return "xl:col-span-2 2xl:col-span-3"
    if service_count >= 6:
        return "2xl:col-span-2"
    return ""


def service_grid_classes(service_count: int) -> str:
    if service_count >= 10:
        return "grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-3 w-full"
    if service_count >= 6:
        return "grid grid-cols-1 xl:grid-cols-2 gap-3 w-full"
    return "grid grid-cols-1 gap-3 w-full"


def build_grouped_overview(
    monitors: List[Dict[str, Any]],
    histories: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    sites: Dict[str, Dict[str, Any]] = {}

    for monitor in monitors:
        site_name, stage_name = infer_site_and_stage(monitor)
        stage_key = stage_name or "General"
        site_entry = sites.setdefault(site_name, {"name": site_name, "stages": {}})
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

    grouped_sites = []
    for site_entry in sites.values():
        stage_list = []
        stage_states = []
        stage_timelines = []

        for stage_entry in site_entry["stages"].values():
            host_list = []
            host_states = []
            host_timelines = []

            for host_entry in stage_entry["hosts"].values():
                agg_state = aggregate_state(host_entry["states"])
                agg_timeline = merge_timelines(host_entry["timelines"])
                avg_uptime = _timeline_uptime_percent(agg_timeline)
                avg_uptime_all = _aggregate_uptime_percent(host_entry.get("uptimes_all") or [100.0])
                services = sorted(host_entry["services"], key=lambda i: (i["state"], i["display_name"]))
                host_list.append({
                    "name": host_entry["name"],
                    "address": host_entry.get("address"),
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

            host_list.sort(key=lambda i: (i["state"], i["name"]))
            stage_state = aggregate_state(host_states)
            stage_timeline = merge_timelines(host_timelines)
            stage_uptime = _timeline_uptime_percent(stage_timeline)
            stage_uptime_all = _aggregate_uptime_percent([h.get("uptime_all", 100.0) for h in host_list]) if host_list else 100.0
            stage_list.append({
                "name": stage_entry["name"],
                "state": stage_state,
                "timeline": stage_timeline,
                "uptime_24h": stage_uptime,
                "uptime_all": stage_uptime_all,
                "hosts": host_list,
                "host_count": len(host_list),
                "service_count": sum(h["service_count"] for h in host_list),
            })
            stage_states.append(stage_state)
            stage_timelines.append(stage_timeline)

        stage_list.sort(key=lambda i: i["name"])
        site_timeline = merge_timelines(stage_timelines)
        grouped_sites.append({
            "name": site_entry["name"],
            "state": aggregate_state(stage_states),
            "timeline": site_timeline,
            "uptime_24h": _timeline_uptime_percent(site_timeline),
            "uptime_all": _aggregate_uptime_percent([s.get("uptime_all", 100.0) for s in stage_list]) if stage_list else 100.0,
            "stages": stage_list,
            "host_count": sum(s["host_count"] for s in stage_list),
            "service_count": sum(s["service_count"] for s in stage_list),
        })

    return sorted(grouped_sites, key=lambda i: i["name"])
