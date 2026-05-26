import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, case, func

from core.api import Base


class MonitorState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    PAUSED = "PAUSED"
    UNKNOWN = "UNKNOWN"


class MonitorType(str, Enum):
    SERVER = "server"
    SERVICE = "service"
    HTTP = "http"
    PING = "ping"


class MonitorRecord(Base):
    __tablename__ = "monitoring_monitors"

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(String(191), nullable=False, index=True)
    state = Column(String(16), nullable=False)
    latency_ms = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="active_probe")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)


class MonitorDailyAggregate(Base):
    __tablename__ = "monitoring_daily_aggregates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    monitor_id = Column(String(191), nullable=False, index=True)
    day = Column(DateTime(timezone=True), nullable=False, index=True)
    total_samples = Column(Integer, nullable=False, default=0)
    up_samples = Column(Integer, nullable=False, default=0)
    down_samples = Column(Integer, nullable=False, default=0)
    paused_samples = Column(Integer, nullable=False, default=0)
    avg_latency_ms = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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
    force: bool = False


class PassiveResult(BaseModel):
    monitor_id: str
    state: MonitorState
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    source: str = "event_bus"
    timestamp: Optional[str] = None


class AdminOverride(BaseModel):
    monitor_id: str
    action: str = Field(pattern="^(pause|resume|update)$")
    patch: Dict[str, Any] = Field(default_factory=dict)


class InventorySyncPayload(BaseModel):
    owner_source: str = "orchestrator_service"
    source_revision: Optional[str] = None
    hosts: List[Dict[str, Any]] = Field(default_factory=list)
    services: List[Dict[str, Any]] = Field(default_factory=list)


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
        return json.loads(raw)
    except Exception:
        return {}


def _serialize_monitor(record: MonitorRecord) -> Dict[str, Any]:
    return {
        "monitor_id": record.monitor_id,
        "name": record.name,
        "monitor_type": record.monitor_type,
        "owner_source": record.owner_source,
        "target": record.target,
        "address": record.address,
        "host_name": record.host_name,
        "service_name": record.service_name,
        "logical_group": record.logical_group,
        "interval_seconds": record.interval_seconds,
        "timeout_seconds": record.timeout_seconds,
        "enabled": record.enabled,
        "source_hash": record.source_hash,
        "metadata": _safe_json_load(record.metadata_json),
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
        MonitorHeartbeat.state != MonitorState.PAUSED.value,
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
