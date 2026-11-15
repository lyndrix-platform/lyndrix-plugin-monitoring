import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Text, case, func

from core.api import Base


class MonitorState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    PAUSED = "PAUSED"
    UNKNOWN = "UNKNOWN"
    # Derived, never reported: a probe that fails while a maintenance window or an
    # override is active is stored as MAINTENANCE instead of DOWN (a successful
    # probe stays UP). See controller/maintenance.py.
    MAINTENANCE = "MAINTENANCE"


# States that do not count towards the uptime denominator.
UPTIME_EXCLUDED_STATES = (MonitorState.PAUSED.value, MonitorState.MAINTENANCE.value)


class MonitorType(str, Enum):
    SERVER = "server"
    SERVICE = "service"
    HTTP = "http"
    PING = "ping"


class MonitorRecord(Base):
    __tablename__ = "monitoring_monitors"
    # Tolerate the model module being imported via more than one path during
    # plugin load (shared Base.metadata) instead of aborting load_module.
    __table_args__ = {"extend_existing": True}

    monitor_id = Column(String(191), primary_key=True)
    name = Column(String(255), nullable=False)
    monitor_type = Column(String(32), nullable=False)
    owner_source = Column(String(64), nullable=False, default="ui_admin")
    target = Column(String(512), nullable=True)
    address = Column(String(255), nullable=True)
    host_name = Column(String(191), nullable=True)
    service_name = Column(String(191), nullable=True)
    logical_group = Column(String(191), nullable=False, default="default")
    interval_seconds = Column(Integer, nullable=False, default=60)
    timeout_seconds = Column(Integer, nullable=False, default=10)
    enabled = Column(Boolean, nullable=False, default=True)
    source_hash = Column(String(128), nullable=True)
    config_hash = Column(String(128), nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}")
    # Entity-Registry configuration-item id of the monitored thing (host or
    # service) and, for service monitors, of the host it runs on. Delivered by
    # the IaC orchestrator through /inventory-sync; empty until it does.
    ci_id = Column(String(36), nullable=True, index=True)
    host_ci_id = Column(String(36), nullable=True)
    latest_state = Column(String(16), nullable=False, default=MonitorState.UNKNOWN.value)
    latest_latency_ms = Column(Float, nullable=True)
    latest_error = Column(Text, nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    is_admin_overridden = Column(Boolean, nullable=False, default=False)
    uptime_24h = Column(Float, nullable=False, default=100.0)
    uptime_7d = Column(Float, nullable=False, default=100.0)
    uptime_30d = Column(Float, nullable=False, default=100.0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorHeartbeat(Base):
    __tablename__ = "monitoring_heartbeats"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(String(191), nullable=False, index=True)
    state = Column(String(16), nullable=False)
    latency_ms = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="active_probe")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)


class MonitorDailyAggregate(Base):
    __tablename__ = "monitoring_daily_aggregates"
    # One row per monitor and day: lets the probe writers upsert atomically
    # (INSERT … ON DUPLICATE KEY UPDATE). Existing installs get it through the
    # deduplicating migration in MonitoringService._ensure_daily_aggregate_unique.
    __table_args__ = (
        Index("uq_monitoring_daily_aggregates_monitor_day", "monitor_id", "day", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(String(191), nullable=False, index=True)
    day = Column(DateTime(timezone=True), nullable=False, index=True)
    total_samples = Column(Integer, nullable=False, default=0)
    up_samples = Column(Integer, nullable=False, default=0)
    down_samples = Column(Integer, nullable=False, default=0)
    paused_samples = Column(Integer, nullable=False, default=0)
    # Failed probes inside a maintenance window/override; excluded from the
    # uptime denominator like paused samples (added in 1.7.0, _ensure_schema).
    maintenance_samples = Column(Integer, nullable=False, default=0)
    avg_latency_ms = Column(Float, nullable=True)
    # Number of samples that carried a latency — lets avg_latency_ms be updated
    # incrementally per heartbeat (running mean) instead of re-reading the day.
    latency_samples = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Maintenance windows, overrides and outages (1.7.0, core API >= 1.13)
#
# Timestamps are stored as naive UTC like the rest of the plugin; the
# controller converts to aware datetimes for core.api.maintenance.
# ---------------------------------------------------------------------------


class MaintenanceWindowRecord(Base):
    """Windows published by one ``source`` for one CI (``maintenance:windows_published``)
    or entered manually in this plugin (``source="monitoring"``, by CI or monitor)."""

    __tablename__ = "monitoring_maintenance_windows"
    __table_args__ = (
        Index("uq_monitoring_mw_source_ci_monitor", "source", "ci_id", "monitor_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ci_id = Column(String(36), nullable=True, index=True)
    monitor_id = Column(String(191), nullable=True, index=True)
    source = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False, default="generic")
    name = Column(String(255), nullable=False, default="")
    host_ci_id = Column(String(36), nullable=True)
    windows_json = Column(Text, nullable=False, default="[]")
    inherited_from = Column(String(32), nullable=True)
    incidents_json = Column(Text, nullable=True)
    deploy_policy = Column(String(16), nullable=True)
    note = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MaintenanceOverrideRecord(Base):
    """A temporary maintenance marker (running deploy, manual override)."""

    __tablename__ = "monitoring_overrides"
    __table_args__ = {"extend_existing": True}

    override_id = Column(String(64), primary_key=True)
    source = Column(String(64), nullable=False)
    ci_ids_json = Column(Text, nullable=False, default="[]")
    monitor_ids_json = Column(Text, nullable=True)
    until = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    linger_minutes = Column(Integer, nullable=False, default=15)
    reason = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class OutageRecord(Base):
    """Outage clock of one monitor: ``open`` while DOWN/MAINTENANCE, ``persisted``
    once reported, deleted on recovery."""

    __tablename__ = "monitoring_outages"
    __table_args__ = {"extend_existing": True}

    monitor_id = Column(String(191), primary_key=True)
    outage_id = Column(String(128), nullable=True)
    ci_id = Column(String(36), nullable=True)
    down_since = Column(DateTime(timezone=True), nullable=False)
    suppressed_until = Column(DateTime(timezone=True), nullable=True)
    state = Column(String(16), nullable=False, default="open")  # open | persisted | recovered
    persisted_at = Column(DateTime(timezone=True), nullable=True)
    recovered_at = Column(DateTime(timezone=True), nullable=True)
    rule_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class OutageEventRecord(Base):
    """Outbox of ``monitoring:outage_persisted`` / ``outage_recovered`` (MO-09).

    The row is written in the SAME transaction as the outage state change and
    deleted once the event was emitted. A process that dies between commit and
    emit, or an emit that fails, leaves the row behind and the probing instance
    delivers it on its next evaluator pass. Delivery is at-least-once; consumers
    dedupe on ``outage_id`` (ITSM: ``external_id``)."""

    __tablename__ = "monitoring_outage_events"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String(64), nullable=False)
    outage_id = Column(String(128), nullable=False, index=True)
    payload_json = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    # Delivery claim: another instance leaves the row alone until this passes.
    claimed_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class MonitorUpsert(BaseModel):
    monitor_id: str
    name: str
    monitor_type: MonitorType
    owner_source: str = "ui_admin"
    target: Optional[str] = None
    address: Optional[str] = None
    host_name: Optional[str] = None
    service_name: Optional[str] = None
    logical_group: str = "default"
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    enabled: bool = True
    source_hash: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Part of the config hash: a changed ci_id is a changed monitor.
    ci_id: Optional[str] = Field(default=None, max_length=36)
    host_ci_id: Optional[str] = Field(default=None, max_length=36)
    force: bool = False


class PassiveResult(BaseModel):
    monitor_id: str
    state: MonitorState
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    source: str = "event_bus"
    timestamp: Optional[str] = None

    @field_validator("state")
    @classmethod
    def _no_derived_state(cls, value: MonitorState) -> MonitorState:
        # MAINTENANCE is derived from DOWN + an active window; a reporter only
        # knows whether the thing is up or down.
        if value == MonitorState.MAINTENANCE:
            raise ValueError("state MAINTENANCE is derived by the monitoring plugin and cannot be reported")
        return value


class WindowCreate(BaseModel):
    """``POST /windows`` — a manual maintenance window for a CI or a monitor."""

    ci_id: Optional[str] = Field(default=None, max_length=36)
    monitor_id: Optional[str] = Field(default=None, max_length=191)
    windows: List[Dict[str, Any]] = Field(min_length=1)
    note: Optional[str] = Field(default=None, max_length=1000)


class OverrideCreate(BaseModel):
    """``POST /overrides`` — a manual override for CIs and/or monitors."""

    ci_ids: List[str] = Field(default_factory=list)
    monitor_ids: List[str] = Field(default_factory=list)
    minutes: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=1000)


class AdminOverride(BaseModel):
    monitor_id: str
    action: str = Field(pattern="^(pause|resume|update)$")
    patch: Dict[str, Any] = Field(default_factory=dict)


class InventorySyncPayload(BaseModel):
    owner_source: str = "orchestrator_service"
    source_revision: Optional[str] = None
    hosts: List[Dict[str, Any]] = Field(default_factory=list)
    services: List[Dict[str, Any]] = Field(default_factory=list)
    # Explicit confirmation that this import may remove more managed monitors
    # than the plausibility guard allows (empty inventory, large shrink).
    allow_mass_delete: bool = False


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(value: datetime) -> datetime:
    utc = value.astimezone(timezone.utc)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def _safe_json_load(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Monitor metadata allowlist (MO-01)
#
# ``metadata_json`` is served to every ``api:read`` holder and ends up in DB
# dumps, so it may only ever hold descriptive, non-secret values. The inventory
# publisher used to send whole ``terraform`` / service ``config`` blocks (with
# passwords and tokens); nothing in this plugin reads them. The allowlist is
# what the grouping helpers (``controller/helpers.py``) and the probe logic
# (``is_docker_host``, ``deploy_type``) actually read, plus a few plain
# descriptive values. It is applied on EVERY write and again on every read, so
# even a row written by an older version never leaves the API unfiltered.
# ---------------------------------------------------------------------------

#: Top-level keys kept; each value must be a scalar or a flat list of scalars.
#: ``git_repo`` is deliberately absent — repository URLs can embed credentials.
METADATA_ALLOWED_KEYS = (
    "site",
    "stage",
    "location",
    "groups",
    "baseline_roles",
    "profiles",
    "deploy_type",
    "desired_state",
    "git_version",
)
#: Nested blocks kept only with these (scalar) sub-keys.
METADATA_ALLOWED_NESTED = {"terraform": ("node_name", "vm_id")}
_METADATA_MAX_TEXT = 255
_METADATA_MAX_ITEMS = 200


def _metadata_scalar(value: Any) -> Any:
    """A JSON scalar that is safe to keep, else ``None`` (dicts/objects never pass)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_METADATA_MAX_TEXT]
    return None


def sanitize_metadata(raw: Any) -> Dict[str, Any]:
    """Reduce arbitrary metadata to the allowlisted, non-secret subset.

    Unknown keys, nested objects and oversized values are dropped silently —
    never logged, the dropped part is exactly what may hold credentials."""
    if not isinstance(raw, dict):
        return {}
    clean: Dict[str, Any] = {}
    for key in METADATA_ALLOWED_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, (list, tuple)):
            clean[key] = [
                item for item in (_metadata_scalar(v) for v in list(value)[:_METADATA_MAX_ITEMS])
                if item is not None
            ]
        elif not isinstance(value, dict):
            clean[key] = _metadata_scalar(value)
    for key, sub_keys in METADATA_ALLOWED_NESTED.items():
        block = raw.get(key)
        if not isinstance(block, dict):
            continue
        kept = {
            sub: _metadata_scalar(block[sub])
            for sub in sub_keys
            if sub in block and not isinstance(block[sub], (dict, list, tuple))
        }
        kept = {k: v for k, v in kept.items() if v is not None}
        if kept:
            clean[key] = kept
    return clean


def sanitized_metadata_json(raw_json: Optional[str]) -> str:
    """Canonical JSON of the allowlisted subset of a stored ``metadata_json``."""
    return json.dumps(sanitize_metadata(_safe_json_load(raw_json)), sort_keys=True)


def safe_error_text(exc: BaseException) -> str:
    """Error text for logs that never echoes request input: a pydantic
    ``ValidationError`` prints the offending input value, which for a monitor
    payload may be a secret-bearing block."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
                for e in errors(include_input=False, include_url=False)
            ) or type(exc).__name__
        except Exception:
            return type(exc).__name__
    return str(exc)


def strip_url_userinfo(value: Optional[str]) -> Optional[str]:
    """``https://user:pw@host/x`` → ``https://host/x``. The plugin has no
    credential fields, so basic-auth credentials can only sit in the URL; the
    probe still uses the stored value, viewers never see it (MO-22)."""
    if not value or "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    # The authority ends at the first "/", "?" or "#" — an "@" after that is part
    # of the path or query, not userinfo.
    end = next((i for i, ch in enumerate(rest) if ch in "/?#"), len(rest))
    authority, tail = rest[:end], rest[end:]
    if "@" not in authority:
        return value
    return f"{scheme}://{authority.rsplit('@', 1)[1]}{tail}"


def _serialize_monitor(record: MonitorRecord) -> Dict[str, Any]:
    return {
        "monitor_id": record.monitor_id,
        "name": record.name,
        "monitor_type": record.monitor_type,
        "owner_source": record.owner_source,
        "target": strip_url_userinfo(record.target),
        "address": strip_url_userinfo(record.address),
        "host_name": record.host_name,
        "service_name": record.service_name,
        "logical_group": record.logical_group,
        "interval_seconds": record.interval_seconds,
        "timeout_seconds": record.timeout_seconds,
        "enabled": record.enabled,
        "source_hash": record.source_hash,
        # Filtered again at read time: defence in depth for rows written before
        # the allowlist existed (the startup scrub may not have run yet).
        "metadata": sanitize_metadata(_safe_json_load(record.metadata_json)),
        "ci_id": record.ci_id,
        "host_ci_id": record.host_ci_id,
        "latest_state": record.latest_state,
        "latest_latency_ms": record.latest_latency_ms,
        "latest_error": record.latest_error,
        "last_checked_at": record.last_checked_at.isoformat() if record.last_checked_at else None,
        "is_admin_overridden": record.is_admin_overridden,
        "uptime_24h": round(record.uptime_24h or 0.0, 2),
        "uptime_7d": round(record.uptime_7d or 0.0, 2),
        "uptime_30d": round(record.uptime_30d or 0.0, 2),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def _calculate_uptime_percentages(session, monitor_ids: List[str]) -> Dict[str, float]:
    if not monitor_ids:
        return {}
    totals = {mid: {"total": 0, "up": 0} for mid in monitor_ids}
    rows = session.query(
        MonitorHeartbeat.monitor_id,
        func.count(MonitorHeartbeat.id),
        func.sum(case((MonitorHeartbeat.state == MonitorState.UP.value, 1), else_=0)),
    ).filter(
        MonitorHeartbeat.monitor_id.in_(monitor_ids),
        MonitorHeartbeat.state.notin_(UPTIME_EXCLUDED_STATES),
    ).group_by(MonitorHeartbeat.monitor_id).all()
    for mid, total, up_total in rows:
        totals[mid] = {"total": int(total or 0), "up": int(up_total or 0)}
    percentages: Dict[str, float] = {}
    for mid, values in totals.items():
        if values["total"] <= 0:
            percentages[mid] = 100.0
        else:
            percentages[mid] = round((values["up"] / values["total"]) * 100.0, 2)
    return percentages


def _uptime_from_aggregates(session, monitor_ids: List[str], days: int = 30) -> Dict[str, float]:
    """Uptime over the last ``days`` from ``monitoring_daily_aggregates`` — one
    small query over ~days × monitors rows instead of a scan of every heartbeat.
    Paused and maintenance samples do not count. Monitors without samples read 100 %.
    """
    if not monitor_ids:
        return {}
    cutoff = _day_start(_utc_now()) - timedelta(days=max(1, days) - 1)
    rows = session.query(
        MonitorDailyAggregate.monitor_id,
        func.sum(MonitorDailyAggregate.up_samples),
        func.sum(
            MonitorDailyAggregate.total_samples
            - MonitorDailyAggregate.paused_samples
            - MonitorDailyAggregate.maintenance_samples
        ),
    ).filter(
        MonitorDailyAggregate.monitor_id.in_(monitor_ids),
        MonitorDailyAggregate.day >= cutoff,
    ).group_by(MonitorDailyAggregate.monitor_id).all()
    percentages = {mid: 100.0 for mid in monitor_ids}
    for mid, up, total in rows:
        total = int(total or 0)
        if total > 0:
            percentages[mid] = round(min(100.0, (int(up or 0) / total) * 100.0), 2)
    return percentages


# TODO(agent): MO-13 — make ix_monitoring_heartbeats_monitor_created covering
# (monitor_id, created_at, state) and drop the redundant single-column monitor_id
# index. Needs a guarded online migration (create_all never alters existing
# tables), so it is not part of this change.
def _hourly_state_counts(session, monitor_ids: List[str], hours: int = 24) -> Dict[str, List[Dict[str, int]]]:
    """Per monitor, ``hours`` buckets (oldest first) of heartbeat counts by state,
    bucketed by the database: ``{monitor_id: [{"UP": n, "DOWN": n, ...}, ...]}``.

    Replaces loading a full day of heartbeat rows per overview request (hundreds
    of thousands of ORM objects) with at most ``hours × monitors`` result rows.
    The bucket expression is dialect-specific (MariaDB/MySQL vs SQLite tests).
    """
    from sqlalchemy import Integer as _Int, text as _text

    if not monitor_ids:
        return {}
    hours = max(1, int(hours))
    # Heartbeats are stored as naive UTC wall time (see MonitorHeartbeat.created_at
    # default); compare against a naive UTC start so both dialects agree.
    start = (_utc_now() - timedelta(hours=hours)).replace(tzinfo=None, microsecond=0)
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    if dialect in ("mysql", "mariadb"):
        bucket = func.floor(func.timestampdiff(_text("SECOND"), start, MonitorHeartbeat.created_at) / 3600)
    else:
        start_epoch = int(start.replace(tzinfo=timezone.utc).timestamp())
        bucket = (func.cast(func.strftime("%s", MonitorHeartbeat.created_at), _Int) - start_epoch) / 3600
    bucket = func.cast(bucket, _Int).label("bucket")
    rows = session.query(
        MonitorHeartbeat.monitor_id, bucket, MonitorHeartbeat.state, func.count(MonitorHeartbeat.id),
    ).filter(
        MonitorHeartbeat.monitor_id.in_(monitor_ids),
        MonitorHeartbeat.created_at >= start,
    ).group_by(MonitorHeartbeat.monitor_id, bucket, MonitorHeartbeat.state).all()
    out: Dict[str, List[Dict[str, int]]] = {mid: [dict() for _ in range(hours)] for mid in monitor_ids}
    for mid, idx, state, n in rows:
        idx = int(idx or 0)
        if mid in out and 0 <= idx < hours:
            out[mid][idx][state] = out[mid][idx].get(state, 0) + int(n or 0)
    return out


def _aggregate_uptime_percent(values: List[float]) -> float:
    valid = [float(v) for v in values if v is not None]
    if not valid:
        return 100.0
    return round(min(valid), 2)


def _timeline_uptime_percent(timeline: List[str]) -> float:
    if not timeline:
        return 100.0
    up_count = sum(1 for s in timeline if s == MonitorState.UP.value)
    return round((up_count / len(timeline)) * 100.0, 2)
