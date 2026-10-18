"""Maintenance windows, overrides and the outage rule (1.7.0, core API >= 1.13).

Three layers, all driven by ``MonitoringService``:

* :class:`MaintenanceStore` — the shared tables ``monitoring_maintenance_windows``,
  ``monitoring_overrides`` and ``monitoring_outages`` (READ COMMITTED sessions,
  1020/1205/1213 retries, idempotent writes).
* :class:`MaintenanceCache` — the per-instance in-memory view the probe hot path
  reads (``state_for``/``rule_for`` touch no database); reloaded after every
  write and when a peer announces ``monitoring:monitors_changed`` with
  ``reason == CHANGE_REASON``.
* :class:`MaintenanceManager` — the bus handlers, the manual-window/override
  operations of the API, the outage bookkeeping inside the heartbeat
  transaction and the 60 s outage evaluator.

Contract: ``lyndrix-core/features/maintenance-windows/00-ergebnis-p0.md`` §3/§4
and ``core.api.maintenance``. ``maintenance:*`` and ``monitoring:outage_*`` are
emitted **locally** (no ``cluster=True``); cache invalidation of the peers runs
over the plugin's own ``monitoring:monitors_changed`` broadcast.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, overload

from sqlalchemy.exc import IntegrityError

from core.api import maintenance as mw

from ..model.models import (
    MaintenanceOverrideRecord,
    MaintenanceWindowRecord,
    MonitorRecord,
    MonitorState,
    MonitorType,
    OutageRecord,
    OverrideCreate,
    WindowCreate,
    _utc_now,
)

#: ``source`` of windows/overrides created in this plugin's UI/API.
MANUAL_SOURCE = "monitoring"
#: ``reason`` of the ``monitoring:monitors_changed`` broadcast that tells the
#: peers to reload their maintenance cache (``"maintenance"`` already means
#: "destructive maintenance ran, reload your probe jobs").
CHANGE_REASON = "maintenance_windows"
#: Kinds with a dedicated on/off setting; every other kind is governed by
#: ``outage_rules_disabled_kinds`` (kinds are open, decision A).
SWITCHED_KINDS = ("hypervisor", "host", "service", "container")

OUTAGE_OPEN = "open"
OUTAGE_PERSISTED = "persisted"
OUTAGE_RECOVERED = "recovered"


# --------------------------------------------------------------------------- #
# Time helpers — tables hold naive UTC, core.api.maintenance wants aware UTC
# --------------------------------------------------------------------------- #


@overload
def aware(value: datetime) -> datetime: ...


@overload
def aware(value: None) -> None: ...


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """Naive (UTC wall time) → aware UTC; aware → converted to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@overload
def naive(value: datetime) -> datetime: ...


@overload
def naive(value: None) -> None: ...


def naive(value: Optional[datetime]) -> Optional[datetime]:
    """Aware → naive UTC wall time for storage; naive passes through."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return mw.iso(aware(value)) if value is not None else None


def _loads(raw: Optional[str], default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


# --------------------------------------------------------------------------- #
# Settings (manifest settings_schema, applied by MonitoringService.apply_settings)
# --------------------------------------------------------------------------- #


@dataclass
class MaintenanceSettings:
    outage_rules_enabled: bool = True
    kind_switches: Dict[str, bool] = field(
        default_factory=lambda: {
            "hypervisor": True,
            "host": True,
            "service": True,
            "container": False,
        }
    )
    disabled_kinds: frozenset = frozenset()
    default_offline_minutes: int = 10
    default_severity: str = "high"
    default_auto_resolve: bool = True
    override_default_minutes: int = 60
    override_max_minutes: int = mw.DEFAULT_MAX_OVERRIDE_MINUTES
    override_linger_minutes: int = mw.DEFAULT_LINGER_MINUTES

    @classmethod
    def from_values(cls, values: Dict[str, Any]) -> "MaintenanceSettings":
        base = cls()
        switches = {
            kind: _as_bool(
                values.get(f"outage_rules_enabled_{kind}"), base.kind_switches[kind]
            )
            for kind in SWITCHED_KINDS
        }
        raw_disabled = values.get("outage_rules_disabled_kinds") or ""
        if isinstance(raw_disabled, (list, tuple, set)):
            raw_disabled = ",".join(str(k) for k in raw_disabled)
        disabled = frozenset(
            k.strip().lower()
            for k in str(raw_disabled).split(",")
            if k.strip() and mw.is_valid_kind(k.strip().lower())
        )
        severity = (
            str(values.get("outage_default_severity") or base.default_severity)
            .strip()
            .lower()
        )
        if severity not in mw.SEVERITIES:
            severity = base.default_severity
        max_minutes = _as_int(
            values.get("override_max_minutes"),
            base.override_max_minutes,
            1,
            7 * 24 * 60,
        )
        return cls(
            outage_rules_enabled=_as_bool(values.get("outage_rules_enabled"), True),
            kind_switches=switches,
            disabled_kinds=disabled,
            default_offline_minutes=_as_int(
                values.get("outage_default_offline_minutes"), 10, 0, 7 * 24 * 60
            ),
            default_severity=severity,
            default_auto_resolve=_as_bool(
                values.get("outage_default_auto_resolve"), True
            ),
            override_default_minutes=min(
                max_minutes,
                _as_int(
                    values.get("override_default_minutes"),
                    base.override_default_minutes,
                    1,
                    7 * 24 * 60,
                ),
            ),
            override_max_minutes=max_minutes,
            override_linger_minutes=_as_int(
                values.get("override_linger_minutes"),
                base.override_linger_minutes,
                0,
                24 * 60,
            ),
        )

    def kind_enabled(self, kind: str) -> bool:
        """Emergency switches: known kinds have their own toggle, every other
        kind is silent only when listed in ``outage_rules_disabled_kinds``."""
        if not self.outage_rules_enabled:
            return False
        if kind in self.kind_switches:
            return self.kind_switches[kind]
        return kind not in self.disabled_kinds

    def default_rule(self) -> mw.IncidentRule:
        return mw.IncidentRule(
            enabled=self.default_offline_minutes > 0,
            offline_minutes=self.default_offline_minutes,
            severity=self.default_severity,
            auto_resolve=self.default_auto_resolve,
        )


# --------------------------------------------------------------------------- #
# Item normalisation (maintenance:windows_published)
# --------------------------------------------------------------------------- #


def normalize_item(item: Any, *, log=None) -> Optional[Dict[str, Any]]:
    """Validate one published item; returns the row values or None (logged).

    An item without ``ci_id`` or with an invalid kind/window is skipped — one bad
    item never fails the publish. Unknown fields are ignored (newer emitters).
    """
    if not isinstance(item, dict):
        return None
    ci_id = item.get("ci_id")
    if not isinstance(ci_id, str) or not ci_id.strip():
        return None
    ci_id = ci_id.strip()[:36]
    kind = str(item.get("kind") or "generic").strip().lower()
    if not mw.is_valid_kind(kind):
        if log:
            log.warning(
                f"State Monitoring: windows_published item {ci_id} has invalid kind {kind!r} — skipped."
            )
        return None
    try:
        windows = mw.to_dicts(mw.parse_windows(item.get("windows") or []))
    except mw.MaintenanceError as exc:
        if log:
            log.warning(
                f"State Monitoring: windows_published item {ci_id}: {exc} — skipped."
            )
        return None
    incidents = None
    raw_incidents = item.get("incidents")
    if raw_incidents is not None:
        try:
            rule = mw.parse_incident_rule(raw_incidents)
            incidents = mw.rule_to_dict(rule) if rule else None
        except mw.MaintenanceError as exc:
            if log:
                log.warning(
                    f"State Monitoring: windows_published item {ci_id}: incidents ignored ({exc})."
                )
    inherited = item.get("inherited_from")
    deploy_policy = item.get("deploy_policy")
    host_ci_id = item.get("host_ci_id")
    return {
        "ci_id": ci_id,
        "kind": kind,
        "name": str(item.get("name") or ci_id)[:255],
        "host_ci_id": str(host_ci_id)[:36] if host_ci_id else None,
        "windows": windows,
        "inherited_from": str(inherited)[:32] if inherited else None,
        "incidents": incidents,
        "deploy_policy": (
            str(deploy_policy)[:16] if deploy_policy in mw.DEPLOY_POLICIES else None
        ),
    }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class MaintenanceStore:
    """Persistence for windows, overrides and outages. Every method opens its own
    READ COMMITTED session (``MonitoringService._rc_session``) and retries short
    write transactions on MariaDB 1020/1205/1213 (``MonitoringService._retrying``).
    An IntegrityError on insert means "a peer wrote the same row" and is accepted."""

    def __init__(self, service):
        self.svc = service

    # -- helpers ---------------------------------------------------------------

    def _session(self):
        return self.svc._rc_session()

    @staticmethod
    def window_to_dict(row: MaintenanceWindowRecord) -> Dict[str, Any]:
        return {
            "id": row.id,
            "ci_id": row.ci_id,
            "monitor_id": row.monitor_id,
            "source": row.source,
            "kind": row.kind,
            "name": row.name,
            "host_ci_id": row.host_ci_id,
            "windows": _loads(row.windows_json, []),
            "inherited_from": row.inherited_from,
            "incidents": _loads(row.incidents_json, None),
            "deploy_policy": row.deploy_policy,
            "note": row.note,
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def override_to_dict(row: MaintenanceOverrideRecord) -> Dict[str, Any]:
        return {
            "override_id": row.override_id,
            "source": row.source,
            "ci_ids": _loads(row.ci_ids_json, []),
            "monitor_ids": _loads(row.monitor_ids_json, []) or [],
            "until": _iso(row.until),
            "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at),
            "linger_minutes": int(row.linger_minutes or 0),
            "reason": row.reason or "",
        }

    @staticmethod
    def override_from_row(row: MaintenanceOverrideRecord) -> mw.Override:
        return mw.Override(
            override_id=row.override_id,
            ci_ids=tuple(str(c) for c in _loads(row.ci_ids_json, [])),
            until=aware(row.until),
            started_at=aware(row.started_at),
            ended_at=aware(row.ended_at),
            linger_minutes=int(row.linger_minutes or 0),
            reason=row.reason or "",
            source=row.source or "",
        )

    @staticmethod
    def outage_to_dict(row: OutageRecord) -> Dict[str, Any]:
        return {
            "monitor_id": row.monitor_id,
            "outage_id": row.outage_id,
            "ci_id": row.ci_id,
            "down_since": _iso(row.down_since),
            "suppressed_until": _iso(row.suppressed_until),
            "state": row.state,
            "persisted_at": _iso(row.persisted_at),
            "recovered_at": _iso(row.recovered_at),
            "rule": _loads(row.rule_json, None),
            "error": row.error,
        }

    # -- windows ---------------------------------------------------------------

    def replace_windows(self, source: str, items: Sequence[Dict[str, Any]]) -> int:
        """``replaces: true`` — drop every row of ``source`` and insert ``items``
        (already normalised) in ONE transaction. Duplicate ci_ids within one
        publish: the last item wins."""
        session = self._session()
        if session is None:
            return 0
        by_ci: Dict[str, Dict[str, Any]] = {it["ci_id"]: it for it in items}
        try:

            def _write() -> int:
                session.query(MaintenanceWindowRecord).filter(
                    MaintenanceWindowRecord.source == source
                ).delete(synchronize_session=False)
                now = naive(_utc_now())
                for it in by_ci.values():
                    session.add(self._row_from_item(source, it, now))
                session.commit()
                return len(by_ci)

            try:
                return self.svc._retrying(session, _write)
            except IntegrityError:
                session.rollback()  # a peer replaced the same source concurrently
                return 0
        finally:
            session.close()

    def upsert_items(self, source: str, items: Sequence[Dict[str, Any]]) -> int:
        """``replaces: false`` — update/insert per ci_id, keep the rest."""
        session = self._session()
        if session is None:
            return 0
        try:
            count = 0
            for it in items:

                def _write(it=it) -> None:
                    row = (
                        session.query(MaintenanceWindowRecord)
                        .filter(
                            MaintenanceWindowRecord.source == source,
                            MaintenanceWindowRecord.ci_id == it["ci_id"],
                            MaintenanceWindowRecord.monitor_id.is_(None),
                        )
                        .first()
                    )
                    now = naive(_utc_now())
                    if row is None:
                        session.add(self._row_from_item(source, it, now))
                    else:
                        self._apply_item(row, it, now)
                    session.commit()

                try:
                    self.svc._retrying(session, _write)
                    count += 1
                except IntegrityError:
                    session.rollback()
            return count
        finally:
            session.close()

    @staticmethod
    def _row_from_item(
        source: str, it: Dict[str, Any], now: datetime
    ) -> MaintenanceWindowRecord:
        row = MaintenanceWindowRecord(
            source=source, ci_id=it["ci_id"], monitor_id=it.get("monitor_id")
        )
        MaintenanceStore._apply_item(row, it, now)
        return row

    @staticmethod
    def _apply_item(
        row: MaintenanceWindowRecord, it: Dict[str, Any], now: datetime
    ) -> None:
        row.kind = it.get("kind") or "generic"
        row.name = it.get("name") or row.ci_id or row.monitor_id or ""
        row.host_ci_id = it.get("host_ci_id")
        row.windows_json = json.dumps(it.get("windows") or [], sort_keys=True)
        row.inherited_from = it.get("inherited_from")
        row.incidents_json = (
            json.dumps(it["incidents"], sort_keys=True) if it.get("incidents") else None
        )
        row.deploy_policy = it.get("deploy_policy")
        row.note = it.get("note")
        row.updated_at = now

    def add_manual_window(
        self,
        *,
        ci_id: Optional[str],
        monitor_id: Optional[str],
        windows: List[Dict[str, Any]],
        note: Optional[str],
        kind: str,
        name: str,
        host_ci_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One manual row per target (``source="monitoring"``, ci_id/monitor_id);
        a second POST for the same target appends its windows to that row."""
        session = self._session()
        if session is None:
            raise RuntimeError("Database unavailable")
        try:
            result: Dict[str, Any] = {}

            def _write() -> None:
                nonlocal result
                row = (
                    session.query(MaintenanceWindowRecord)
                    .filter(
                        MaintenanceWindowRecord.source == MANUAL_SOURCE,
                        (
                            MaintenanceWindowRecord.ci_id.is_(None)
                            if ci_id is None
                            else MaintenanceWindowRecord.ci_id == ci_id
                        ),
                        (
                            MaintenanceWindowRecord.monitor_id.is_(None)
                            if monitor_id is None
                            else MaintenanceWindowRecord.monitor_id == monitor_id
                        ),
                    )
                    .first()
                )
                now = naive(_utc_now())
                if row is None:
                    row = MaintenanceWindowRecord(
                        source=MANUAL_SOURCE, ci_id=ci_id, monitor_id=monitor_id
                    )
                    session.add(row)
                    merged = list(windows)
                else:
                    existing = _loads(row.windows_json, [])
                    seen = {json.dumps(w, sort_keys=True) for w in existing}
                    merged = existing + [
                        w for w in windows if json.dumps(w, sort_keys=True) not in seen
                    ]
                self._apply_item(
                    row,
                    {
                        "kind": kind,
                        "name": name,
                        "host_ci_id": host_ci_id,
                        "windows": merged,
                        "note": note if note is not None else row.note,
                    },
                    now,
                )
                session.commit()
                session.refresh(row)
                result = self.window_to_dict(row)

            self.svc._retrying(session, _write)
            return result
        finally:
            session.close()

    def delete_window(self, window_id: int) -> str:
        """Returns ``deleted`` | ``missing`` | ``foreign`` (only manual rows may go)."""
        session = self._session()
        if session is None:
            raise RuntimeError("Database unavailable")
        try:
            row = session.get(MaintenanceWindowRecord, window_id)
            if row is None:
                return "missing"
            if row.source != MANUAL_SOURCE:
                return "foreign"

            def _write() -> None:
                session.query(MaintenanceWindowRecord).filter(
                    MaintenanceWindowRecord.id == window_id
                ).delete(synchronize_session=False)
                session.commit()

            self.svc._retrying(session, _write)
            return "deleted"
        finally:
            session.close()

    def list_windows(
        self,
        *,
        ci_id: Optional[str] = None,
        monitor_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        session = self._session()
        if session is None:
            return []
        try:
            q = session.query(MaintenanceWindowRecord)
            if ci_id:
                q = q.filter(MaintenanceWindowRecord.ci_id == ci_id)
            if monitor_id:
                q = q.filter(MaintenanceWindowRecord.monitor_id == monitor_id)
            if source:
                q = q.filter(MaintenanceWindowRecord.source == source)
            rows = q.order_by(
                MaintenanceWindowRecord.source, MaintenanceWindowRecord.name
            ).all()
            return [self.window_to_dict(r) for r in rows]
        finally:
            session.close()

    # -- overrides -------------------------------------------------------------

    def upsert_override(
        self, override: mw.Override, monitor_ids: Sequence[str] = ()
    ) -> None:
        """Insert or update by ``override_id`` (idempotent for replayed events)."""
        session = self._session()
        if session is None:
            raise RuntimeError("Database unavailable")
        try:

            def _write() -> None:
                row = session.get(MaintenanceOverrideRecord, override.override_id)
                if row is None:
                    row = MaintenanceOverrideRecord(override_id=override.override_id)
                    session.add(row)
                row.source = override.source or ""
                row.ci_ids_json = json.dumps(list(override.ci_ids))
                if monitor_ids or row.monitor_ids_json is None:
                    row.monitor_ids_json = json.dumps(list(monitor_ids))
                row.until = naive(override.until)
                row.started_at = naive(override.started_at)
                if override.ended_at is not None:
                    row.ended_at = naive(override.ended_at)
                row.linger_minutes = int(override.linger_minutes)
                row.reason = override.reason or ""
                row.updated_at = naive(_utc_now())
                session.commit()

            try:
                self.svc._retrying(session, _write)
            except IntegrityError:
                session.rollback()
                self.svc._retrying(session, _write)  # the peer's insert won → update it
        finally:
            session.close()

    def end_override(
        self, override_id: str, ended_at: datetime, linger_minutes: Optional[int]
    ) -> bool:
        session = self._session()
        if session is None:
            raise RuntimeError("Database unavailable")
        try:
            row = session.get(MaintenanceOverrideRecord, override_id)
            if row is None:
                return False

            def _write() -> None:
                row.ended_at = naive(ended_at)
                if linger_minutes is not None:
                    row.linger_minutes = int(linger_minutes)
                row.updated_at = naive(_utc_now())
                session.commit()

            self.svc._retrying(session, _write)
            return True
        finally:
            session.close()

    def get_override(self, override_id: str) -> Optional[Dict[str, Any]]:
        session = self._session()
        if session is None:
            return None
        try:
            row = session.get(MaintenanceOverrideRecord, override_id)
            return self.override_to_dict(row) if row else None
        finally:
            session.close()

    def list_override_rows(self) -> List[MaintenanceOverrideRecord]:
        session = self._session()
        if session is None:
            return []
        try:
            rows = (
                session.query(MaintenanceOverrideRecord)
                .order_by(MaintenanceOverrideRecord.started_at.desc())
                .all()
            )
            session.expunge_all()
            return rows
        finally:
            session.close()

    def purge_expired(self, now: datetime, grace: timedelta = timedelta(days=1)) -> int:
        """Delete overrides whose effective end lies more than ``grace`` in the past."""
        session = self._session()
        if session is None:
            return 0
        try:
            cutoff = aware(now) - grace
            rows = session.query(MaintenanceOverrideRecord).all()
            stale = [
                r.override_id
                for r in rows
                if mw.override_until(self.override_from_row(r)) < cutoff
            ]
            if not stale:
                return 0

            def _write() -> None:
                session.query(MaintenanceOverrideRecord).filter(
                    MaintenanceOverrideRecord.override_id.in_(stale)
                ).delete(synchronize_session=False)
                session.commit()

            self.svc._retrying(session, _write)
            return len(stale)
        finally:
            session.close()

    # -- outages ---------------------------------------------------------------

    def outage_rows(
        self, states: Iterable[str] = (OUTAGE_OPEN, OUTAGE_PERSISTED)
    ) -> List[OutageRecord]:
        session = self._session()
        if session is None:
            return []
        try:
            rows = (
                session.query(OutageRecord)
                .filter(OutageRecord.state.in_(list(states)))
                .all()
            )
            session.expunge_all()
            return rows
        finally:
            session.close()

    def outage_map(self) -> Dict[str, Dict[str, Any]]:
        return {r.monitor_id: self.outage_to_dict(r) for r in self.outage_rows()}

    @staticmethod
    def delete_outages_in(session, monitor_ids: Optional[Sequence[str]]) -> int:
        """Delete outage rows inside the caller's transaction (``None`` = all)."""
        q = session.query(OutageRecord)
        if monitor_ids is not None:
            if not monitor_ids:
                return 0
            q = q.filter(OutageRecord.monitor_id.in_(list(monitor_ids)))
        return int(q.delete(synchronize_session=False) or 0)

    # -- snapshot for the cache --------------------------------------------------

    def load_snapshot(
        self,
    ) -> Tuple[
        List[MaintenanceWindowRecord], List[MaintenanceOverrideRecord], Dict[str, str]
    ]:
        session = self._session()
        if session is None:
            return [], [], {}
        try:
            windows = session.query(MaintenanceWindowRecord).all()
            overrides = session.query(MaintenanceOverrideRecord).all()
            host_ci = {
                host_name: ci_id
                for host_name, ci_id in session.query(
                    MonitorRecord.host_name, MonitorRecord.ci_id
                )
                .filter(
                    MonitorRecord.monitor_type == MonitorType.SERVER.value,
                    MonitorRecord.ci_id.isnot(None),
                    MonitorRecord.host_name.isnot(None),
                )
                .all()
            }
            session.expunge_all()
            return windows, overrides, host_ci
        finally:
            session.close()


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _attrs(
    record: Any,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """``(monitor_id, ci_id, host_ci_id, host_name, monitor_type)`` from an ORM
    record, a scheduler snapshot or a serialised dict."""
    if isinstance(record, dict):
        get = record.get
    else:

        def get(key, default=None):
            return getattr(record, key, default)

    return (
        str(get("monitor_id") or ""),
        get("ci_id") or None,
        get("host_ci_id") or None,
        get("host_name") or None,
        get("monitor_type") or None,
    )


class MaintenanceCache:
    """In-memory evaluation state of one instance. ``reload()`` reads the shared
    tables (off the loop); every lookup afterwards is pure Python."""

    def __init__(self, store: MaintenanceStore, log=None):
        self.store = store
        self.log = log
        self.settings = MaintenanceSettings()
        self._windows_by_ci: Dict[str, List[mw.Schedule]] = {}
        self._windows_by_monitor: Dict[str, List[mw.Schedule]] = {}
        self._overrides_by_ci: Dict[str, List[mw.Override]] = {}
        self._overrides_by_monitor: Dict[str, List[mw.Override]] = {}
        self._overrides: List[mw.Override] = []
        self._rules_by_ci: Dict[str, mw.IncidentRule] = {}
        self._kind_by_ci: Dict[str, str] = {}
        self._name_by_ci: Dict[str, str] = {}
        self._host_ci: Dict[str, str] = {}
        self.loaded_at: float = 0.0
        self.generation = 0

    # -- loading -----------------------------------------------------------------

    def reload(self) -> None:
        windows, overrides, host_ci = self.store.load_snapshot()
        by_ci: Dict[str, List[mw.Schedule]] = {}
        by_monitor: Dict[str, List[mw.Schedule]] = {}
        rules: Dict[str, mw.IncidentRule] = {}
        kinds: Dict[str, str] = {}
        names: Dict[str, str] = {}
        # Published sources first so a manual row never overrides their kind/rule.
        for row in sorted(windows, key=lambda r: r.source == MANUAL_SOURCE):
            try:
                schedules = mw.parse_windows(_loads(row.windows_json, []))
            except mw.MaintenanceError as exc:
                if self.log:
                    self.log.warning(
                        f"State Monitoring: stored window row {row.id} unreadable ({exc}) — ignored."
                    )
                continue
            if row.ci_id:
                by_ci.setdefault(row.ci_id, []).extend(schedules)
                if row.ci_id not in kinds or row.source != MANUAL_SOURCE:
                    kinds[row.ci_id] = row.kind or "generic"
                    names[row.ci_id] = row.name or row.ci_id
                if (
                    row.incidents_json
                    and row.source != MANUAL_SOURCE
                    and row.ci_id not in rules
                ):
                    try:
                        rule = mw.parse_incident_rule(_loads(row.incidents_json, None))
                        if rule is not None:
                            rules[row.ci_id] = rule
                    except mw.MaintenanceError:
                        pass
            if row.monitor_id:
                by_monitor.setdefault(row.monitor_id, []).extend(schedules)
        now = mw.utcnow()
        ov_all: List[mw.Override] = []
        ov_ci: Dict[str, List[mw.Override]] = {}
        ov_monitor: Dict[str, List[mw.Override]] = {}
        for row in overrides:
            try:
                ov = self.store.override_from_row(row)
            except Exception:  # defensive: never let one bad row poison the cache
                continue
            if mw.override_until(ov) <= now:
                continue  # expired — purge_expired removes it eventually
            ov_all.append(ov)
            for ci in ov.ci_ids:
                ov_ci.setdefault(ci, []).append(ov)
            for mid in _loads(row.monitor_ids_json, []) or []:
                ov_monitor.setdefault(str(mid), []).append(ov)
        # Swap atomically (plain attribute assignment; readers run under the GIL).
        self._windows_by_ci, self._windows_by_monitor = by_ci, by_monitor
        self._overrides, self._overrides_by_ci, self._overrides_by_monitor = (
            ov_all,
            ov_ci,
            ov_monitor,
        )
        self._rules_by_ci, self._kind_by_ci, self._name_by_ci = rules, kinds, names
        self._host_ci = dict(host_ci)
        self.loaded_at = time.monotonic()
        self.generation += 1

    def reload_if_stale(self, max_age_seconds: float) -> bool:
        if time.monotonic() - self.loaded_at >= max_age_seconds:
            self.reload()
            return True
        return False

    def note_monitor(self, record: Any) -> None:
        """Keep the host → ci map current after a single upsert (no full reload)."""
        monitor_id, ci_id, _host_ci_id, host_name, monitor_type = _attrs(record)
        if monitor_type == MonitorType.SERVER.value and host_name:
            if ci_id:
                self._host_ci[host_name] = ci_id
            else:
                self._host_ci.pop(host_name, None)

    # -- lookups ---------------------------------------------------------------

    @property
    def active_overrides(self) -> List[mw.Override]:
        return list(self._overrides)

    def resolve_ci(self, record: Any) -> Tuple[Optional[str], Optional[str]]:
        """``(ci_id, host_ci_id)`` of a monitor. A service monitor without its own
        ci_id borrows the ci_id of its host monitor (``host:<host_name>``) — the
        pre-P2 bridge; its host_ci_id is then None (it IS the host)."""
        _monitor_id, ci_id, host_ci_id, host_name, monitor_type = _attrs(record)
        is_host = monitor_type == MonitorType.SERVER.value
        if is_host:
            return ci_id, None
        host_ci = host_ci_id or (self._host_ci.get(host_name) if host_name else None)
        if ci_id:
            return ci_id, host_ci
        return host_ci, None

    def kind_for(self, record: Any, ci_id: Optional[str] = None) -> str:
        """Kind from the published windows of the CI, else derived from the
        monitor type (``server`` → ``host``, anything else → ``service``)."""
        if ci_id is None:
            ci_id, _ = self.resolve_ci(record)
        if ci_id and ci_id in self._kind_by_ci:
            return self._kind_by_ci[ci_id]
        _mid, _ci, _hci, _host, monitor_type = _attrs(record)
        return "host" if monitor_type == MonitorType.SERVER.value else "service"

    def name_for_ci(self, ci_id: str) -> Optional[str]:
        return self._name_by_ci.get(ci_id)

    def kind_by_ci(self, ci_id: Optional[str]) -> Optional[str]:
        """Kind a publisher declared for the CI, None when nobody published it."""
        return self._kind_by_ci.get(ci_id) if ci_id else None

    def overrides_for_ci(self, ci_id: Optional[str]) -> List[mw.Override]:
        return list(self._overrides_by_ci.get(ci_id, [])) if ci_id else []

    def windows_for_ci(self, ci_id: Optional[str]) -> List[mw.Schedule]:
        return list(self._windows_by_ci.get(ci_id, [])) if ci_id else []

    def state_for(self, record: Any, now: datetime) -> mw.MaintenanceState:
        """Effective maintenance state of one monitor: its own manual windows and
        overrides (by monitor_id) plus those of its (effective) CI."""
        monitor_id, *_rest = _attrs(record)
        ci_id, _host_ci_id = self.resolve_ci(record)
        schedules = list(self._windows_by_monitor.get(monitor_id, ()))
        overrides = list(self._overrides_by_monitor.get(monitor_id, ()))
        if ci_id:
            schedules.extend(self._windows_by_ci.get(ci_id, ()))
            overrides.extend(self._overrides_by_ci.get(ci_id, ()))
        if not schedules and not overrides:
            return mw.MaintenanceState(False, None, None, None, None)
        return mw.effective_state(schedules, overrides, aware(now))

    def rule_for(self, record: Any) -> Optional[mw.IncidentRule]:
        """Incident rule cascade: rule published for the service CI → rule of the
        host CI → settings default of the kind. Emergency switches apply here.
        None = no outage events for this monitor."""
        ci_id, host_ci_id = self.resolve_ci(record)
        kind = self.kind_for(record, ci_id)
        if not self.settings.kind_enabled(kind):
            return None
        rule: Optional[mw.IncidentRule] = None
        if ci_id and ci_id in self._rules_by_ci:
            rule = self._rules_by_ci[ci_id]
        elif host_ci_id and host_ci_id in self._rules_by_ci:
            rule = self._rules_by_ci[host_ci_id]
        else:
            rule = self.settings.default_rule()
        if not rule.enabled or rule.offline_minutes <= 0:
            return None
        return rule


# --------------------------------------------------------------------------- #
# Manager — bus handlers, API operations, outage clock
# --------------------------------------------------------------------------- #


class MaintenanceManager:
    """Owns the store and the cache; called by ``MonitoringService``."""

    OUTAGE_JOB_ID = "monitoring:outages"
    OUTAGE_INTERVAL_SECONDS = 60
    #: Safety net: the cache re-reads the tables at least this often even when
    #: every change event was missed (e.g. the plugin was loaded late).
    CACHE_MAX_AGE_SECONDS = 300

    def __init__(self, service):
        self.svc = service
        self.store = MaintenanceStore(service)
        self.cache = MaintenanceCache(self.store, log=getattr(service.ctx, "log", None))
        self._warned_no_ci: set = set()

    @property
    def log(self):
        return self.svc.ctx.log

    @property
    def settings(self) -> MaintenanceSettings:
        return self.cache.settings

    def apply_settings(self, values: Dict[str, Any]) -> None:
        self.cache.settings = MaintenanceSettings.from_values(values or {})

    # -- cache invalidation ------------------------------------------------------

    def reload(self) -> None:
        """Sync: re-read the tables (call off the loop)."""
        self.cache.reload()
        self.svc._view_cache.clear()

    def after_local_change(self) -> None:
        """Something was written here: refresh the local cache and tell the peers
        (own cluster broadcast; the ``maintenance:*`` events themselves stay local)."""
        self.reload()
        self.svc._emit_cluster(
            "monitoring:monitors_changed",
            {
                "reason": CHANGE_REASON,
                "count": None,
                "instance_id": getattr(self.svc.ctx, "instance_id", None),
            },
        )

    # -- bus handlers (run in to_thread workers) ------------------------------------

    def on_windows_published(self, payload: Optional[Dict[str, Any]]) -> int:
        payload = payload or {}
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            return 0
        source = source.strip()[:64]
        if source == MANUAL_SOURCE:
            return 0  # our own publish — already persisted
        items = [
            normalize_item(it, log=self.log) for it in (payload.get("items") or [])
        ]
        valid = [it for it in items if it is not None]
        if payload.get("replaces", True):
            n = self.store.replace_windows(source, valid)
        else:
            n = self.store.upsert_items(source, valid)
        self.after_local_change()
        self.log.info(
            f"State Monitoring: maintenance windows from '{source}' — {n} item(s) stored."
        )
        return n

    def on_override_started(self, payload: Optional[Dict[str, Any]]) -> bool:
        payload = payload or {}
        if payload.get("source") == MANUAL_SOURCE:
            return False
        override = mw.override_from_payload(payload)
        self.store.upsert_override(override)
        self.after_local_change()
        return True

    def on_override_ended(self, payload: Optional[Dict[str, Any]]) -> bool:
        payload = payload or {}
        if payload.get("source") == MANUAL_SOURCE:
            return False
        override_id = payload.get("override_id")
        if not isinstance(override_id, str) or not override_id:
            return False
        ended_raw = payload.get("ended_at")
        ended_at = mw.parse_iso(ended_raw) if ended_raw else mw.utcnow()
        linger = payload.get("linger_minutes")
        linger_minutes = (
            int(linger)
            if isinstance(linger, (int, float, str)) and str(linger).strip()
            else None
        )
        changed = self.store.end_override(override_id, ended_at, linger_minutes)
        if changed:
            self.after_local_change()
        return changed

    def on_peer_change(self, payload: Optional[Dict[str, Any]]) -> bool:
        """``monitoring:monitors_changed`` from a peer with ``reason == CHANGE_REASON``."""
        if (payload or {}).get("reason") != CHANGE_REASON:
            return False
        self.reload()
        return True

    # -- manual windows (API) -------------------------------------------------------

    def _monitor_record(self, monitor_id: str) -> Optional[MonitorRecord]:
        session = self.svc._session()
        if session is None:
            raise RuntimeError("Database unavailable")
        try:
            row = self.svc._get_monitor(session, monitor_id)
            if row is not None:
                session.expunge(row)
            return row
        finally:
            session.close()

    def create_manual_window(self, data: WindowCreate) -> Dict[str, Any]:
        if not data.ci_id and not data.monitor_id:
            raise ValueError("ci_id or monitor_id is required")
        errors = mw.validate_windows(data.windows, default_tz="UTC")
        if errors:
            raise ValueError("; ".join(errors))
        windows = mw.to_dicts(mw.parse_windows(data.windows, default_tz="UTC"))
        ci_id = data.ci_id.strip() if data.ci_id else None
        monitor_id = data.monitor_id.strip() if data.monitor_id else None
        host_ci_id = None
        if monitor_id:
            record = self._monitor_record(monitor_id)
            if record is None:
                raise KeyError(monitor_id)
            resolved_ci, host_ci_id = self.cache.resolve_ci(record)
            ci_id = ci_id or resolved_ci
            kind = self.cache.kind_for(record, ci_id)
            name = record.name or monitor_id
        else:
            kind = self.cache.kind_by_ci(ci_id) or "generic"
            name = (self.cache.name_for_ci(ci_id) if ci_id else None) or ci_id or ""
        row = self.store.add_manual_window(
            ci_id=ci_id,
            monitor_id=monitor_id,
            windows=windows,
            note=data.note,
            kind=kind,
            name=name,
            host_ci_id=host_ci_id,
        )
        self.publish_manual_windows()
        self.after_local_change()
        return row

    def delete_manual_window(self, window_id: int) -> str:
        status = self.store.delete_window(window_id)
        if status == "deleted":
            self.publish_manual_windows()
            self.after_local_change()
        return status

    def publish_manual_windows(self) -> Dict[str, Any]:
        """Tell the registry/server manager about our manual windows (local emit,
        ``source: "monitoring"``; our own handler ignores it). Rows without a
        ci_id stay local — the contract has no item without one."""
        items: Dict[str, Dict[str, Any]] = {}
        for row in self.store.list_windows(source=MANUAL_SOURCE):
            ci_id = row.get("ci_id")
            if not ci_id:
                continue
            item = items.setdefault(
                ci_id,
                {
                    "ci_id": ci_id,
                    "kind": row.get("kind") or "generic",
                    "name": row.get("name") or ci_id,
                    "host_ci_id": row.get("host_ci_id"),
                    "windows": [],
                    "inherited_from": None,
                },
            )
            seen = {json.dumps(w, sort_keys=True) for w in item["windows"]}
            item["windows"].extend(
                w
                for w in row.get("windows") or []
                if json.dumps(w, sort_keys=True) not in seen
            )
        payload = {
            "source": MANUAL_SOURCE,
            "published_at": mw.iso(mw.utcnow()),
            "replaces": True,
            "items": list(items.values()),
        }
        self.svc._emit(mw.TOPIC_WINDOWS_PUBLISHED, payload)
        return payload

    def list_windows(
        self,
        *,
        ci_id: Optional[str] = None,
        monitor_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        now = mw.utcnow()
        out = []
        for row in self.store.list_windows(
            ci_id=ci_id, monitor_id=monitor_id, source=source
        ):
            try:
                schedules = mw.parse_windows(row.get("windows") or [])
                state = mw.effective_state(
                    schedules, self.cache.overrides_for_ci(row.get("ci_id")), now
                )
                row["effective"] = self._state_dict(state)
            except mw.MaintenanceError as exc:
                row["effective"] = {"error": str(exc)}
            out.append(row)
        return out

    @staticmethod
    def _state_dict(state: mw.MaintenanceState) -> Dict[str, Any]:
        return {
            "active": state.active,
            "until": mw.iso(state.until) if state.until else None,
            "source": state.source,
            "next_start": mw.iso(state.next_start) if state.next_start else None,
            "note": state.note,
        }

    # -- manual overrides (API) ------------------------------------------------------

    def create_override(self, data: OverrideCreate) -> Dict[str, Any]:
        ci_ids = {c.strip() for c in data.ci_ids if isinstance(c, str) and c.strip()}
        monitor_ids = [
            m.strip() for m in data.monitor_ids if isinstance(m, str) and m.strip()
        ]
        if not ci_ids and not monitor_ids:
            raise ValueError("ci_ids or monitor_ids is required")
        for mid in monitor_ids:
            record = self._monitor_record(mid)
            if record is None:
                raise KeyError(mid)
            resolved, _host = self.cache.resolve_ci(record)
            if resolved:
                ci_ids.add(resolved)
        settings = self.settings
        minutes = data.minutes or settings.override_default_minutes
        minutes = max(1, min(int(minutes), settings.override_max_minutes))
        now = mw.utcnow()
        override = mw.Override(
            override_id=f"mon-{uuid.uuid4()}",
            ci_ids=tuple(sorted(ci_ids)),
            until=now + timedelta(minutes=minutes),
            started_at=now,
            ended_at=None,
            linger_minutes=settings.override_linger_minutes,
            reason=(data.reason or "").strip(),
            source=MANUAL_SOURCE,
        )
        self.store.upsert_override(override, monitor_ids=monitor_ids)
        self.svc._emit(
            mw.TOPIC_OVERRIDE_STARTED,
            {
                "override_id": override.override_id,
                "ci_ids": list(override.ci_ids),
                "until": mw.iso(override.until),
                "reason": override.reason,
                "source": MANUAL_SOURCE,
                "started_at": mw.iso(override.started_at),
                "linger_minutes": override.linger_minutes,
            },
        )
        self.after_local_change()
        result = self.store.get_override(override.override_id) or {}
        result["active"] = True
        result["until_effective"] = mw.iso(override.until)
        return result

    def end_manual_override(self, override_id: str) -> str:
        """Returns ``ended`` | ``missing`` | ``foreign``."""
        current = self.store.get_override(override_id)
        if current is None:
            return "missing"
        if current.get("source") != MANUAL_SOURCE:
            return "foreign"
        now = mw.utcnow()
        linger = self.settings.override_linger_minutes
        self.store.end_override(override_id, now, linger)
        self.svc._emit(
            mw.TOPIC_OVERRIDE_ENDED,
            {
                "override_id": override_id,
                "source": MANUAL_SOURCE,
                "ended_at": mw.iso(now),
                "linger_minutes": linger,
            },
        )
        self.after_local_change()
        return "ended"

    def list_overrides(self, *, active_only: bool = False) -> Dict[str, Any]:
        now = mw.utcnow()
        items = []
        for row in self.store.list_override_rows():
            ov = self.store.override_from_row(row)
            active = mw.override_active(ov, now)
            if active_only and not active:
                continue
            item = self.store.override_to_dict(row)
            item["active"] = active
            item["until_effective"] = mw.iso(mw.override_until(ov))
            item["own"] = row.source == MANUAL_SOURCE
            item["ci_names"] = [self.cache.name_for_ci(c) or c for c in item["ci_ids"]]
            items.append(item)
        s = self.settings
        return {
            "items": items,
            "defaults": {
                "default_minutes": s.override_default_minutes,
                "max_minutes": s.override_max_minutes,
                "linger_minutes": s.override_linger_minutes,
            },
        }

    # -- outage bookkeeping inside the heartbeat transaction ---------------------------

    def track_outage(
        self,
        session,
        record: MonitorRecord,
        effective: MonitorState,
        now: datetime,
        ms: Optional[mw.MaintenanceState],
        error_message: Optional[str],
    ) -> None:
        """Keep ``monitoring_outages`` consistent with the heartbeat being written
        (same transaction, no phantoms). Recovery events are queued on
        ``session.info["monitoring_events"]`` and emitted by the caller after
        ``commit()``."""
        now_naive = naive(aware(now))
        row = session.get(OutageRecord, record.monitor_id)
        if effective in (MonitorState.DOWN, MonitorState.MAINTENANCE):
            suppressed = (
                naive(ms.until) if (ms is not None and ms.active and ms.until) else None
            )
            if row is None or row.state == OUTAGE_RECOVERED:
                if row is not None:
                    session.delete(row)
                    session.flush()
                ci_id, _host_ci = self.cache.resolve_ci(record)
                session.add(
                    OutageRecord(
                        monitor_id=record.monitor_id,
                        ci_id=ci_id,
                        down_since=now_naive,
                        suppressed_until=suppressed,
                        state=OUTAGE_OPEN,
                        error=error_message,
                        updated_at=now_naive,
                    )
                )
                return
            if suppressed is not None and (
                row.suppressed_until is None or naive(row.suppressed_until) < suppressed
            ):
                row.suppressed_until = suppressed
            row.error = error_message
            row.updated_at = now_naive
            return
        if effective != MonitorState.UP or row is None:
            return  # UNKNOWN/PAUSED neither open nor close an outage here
        if row.state == OUTAGE_PERSISTED:
            # Conditional update = exactly-once recovery event even if two
            # instances store the same passive UP concurrently.
            n = (
                session.query(OutageRecord)
                .filter(
                    OutageRecord.monitor_id == record.monitor_id,
                    OutageRecord.state == OUTAGE_PERSISTED,
                )
                .update(
                    {
                        OutageRecord.state: OUTAGE_RECOVERED,
                        OutageRecord.recovered_at: now_naive,
                        OutageRecord.updated_at: now_naive,
                    },
                    synchronize_session=False,
                )
            )
            if n == 1 and row.outage_id and row.ci_id:
                since = aware(row.down_since)
                session.info.setdefault("monitoring_events", []).append(
                    (
                        mw.TOPIC_OUTAGE_RECOVERED,
                        {
                            "outage_id": row.outage_id,
                            "ci_id": row.ci_id,
                            "since": mw.iso(since),
                            "recovered_at": mw.iso(aware(now)),
                            "duration_s": int((aware(now) - since).total_seconds()),
                        },
                    )
                )
        session.query(OutageRecord).filter(
            OutageRecord.monitor_id == record.monitor_id
        ).delete(synchronize_session=False)

    @staticmethod
    def flush_session_events(session, emit) -> int:
        """Emit the events queued by ``track_outage`` — call after ``commit()``."""
        events = session.info.pop("monitoring_events", None) or []
        for topic, payload in events:
            emit(topic, payload)
        return len(events)

    # -- outage evaluator (60 s job on the probing instance) ----------------------------

    def _evaluates(self, monitor_id: str) -> bool:
        """Lease mode: the prober evaluates everything. Sharded: the owner of the
        monitor, plus the housekeeping worker for rows whose monitor is in no shard
        (left behind by a shard change or a deleted monitor)."""
        svc = self.svc
        if svc.owns(monitor_id):
            return True
        if svc.is_sharded and monitor_id not in svc._shard_universe:
            return svc.housekeeping_here()
        return False

    def evaluate_outages(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """Sync (run off the loop). For every open outage outside maintenance whose
        effective downtime reached the rule's ``offline_minutes``: mark it
        ``persisted`` with a conditional UPDATE and emit ``monitoring:outage_persisted``
        exactly once (``rowcount == 1`` wins, also across a lease handover)."""
        now_aware = aware(now) if now is not None else mw.utcnow()
        self.cache.reload_if_stale(self.CACHE_MAX_AGE_SECONDS)
        stats = {"checked": 0, "persisted": 0, "suppressed": 0, "skipped": 0}
        session = self.svc._rc_session()
        if session is None:
            return stats
        try:
            # Plain tuples, not ORM rows: every commit below expires the session
            # and a row another instance just recovered would raise on refresh.
            pending = [
                (r.monitor_id, aware(r.down_since), aware(r.suppressed_until), r.error)
                for r in session.query(OutageRecord)
                .filter(OutageRecord.state == OUTAGE_OPEN)
                .all()
            ]
            session.expunge_all()
            for monitor_id, down_since, suppressed_until, error in pending:
                if not self._evaluates(monitor_id):
                    continue
                stats["checked"] += 1
                record = self.svc._get_monitor(session, monitor_id)
                if record is None:
                    session.query(OutageRecord).filter(
                        OutageRecord.monitor_id == monitor_id
                    ).delete(synchronize_session=False)
                    session.commit()
                    continue
                ms = self.cache.state_for(record, now_aware)
                if ms.active:
                    stats["suppressed"] += 1
                    if ms.until is not None and (
                        suppressed_until is None or suppressed_until < ms.until
                    ):
                        until = naive(ms.until)

                        def _extend(until=until) -> None:
                            session.query(OutageRecord).filter(
                                OutageRecord.monitor_id == monitor_id,
                                OutageRecord.state == OUTAGE_OPEN,
                            ).update(
                                {
                                    OutageRecord.suppressed_until: until,
                                    OutageRecord.updated_at: naive(now_aware),
                                },
                                synchronize_session=False,
                            )
                            session.commit()

                        self.svc._retrying(session, _extend)
                    continue
                rule = self.cache.rule_for(record)
                if rule is None:
                    stats["skipped"] += 1
                    continue
                effective_since = max(down_since, suppressed_until or down_since)
                if now_aware - effective_since < timedelta(
                    minutes=rule.offline_minutes
                ):
                    continue
                ci_id, host_ci_id = self.cache.resolve_ci(record)
                if not ci_id:
                    if monitor_id not in self._warned_no_ci:
                        self._warned_no_ci.add(monitor_id)
                        self.log.info(
                            f"State Monitoring: outage of '{monitor_id}' not reported — the monitor has no ci_id."
                        )
                    stats["skipped"] += 1
                    continue
                outage_id = mw.make_outage_id(ci_id, down_since)
                rule_dict = mw.rule_to_dict(rule)
                persisted_at = naive(now_aware)

                def _claim(
                    monitor_id=monitor_id,
                    outage_id=outage_id,
                    ci_id=ci_id,
                    rule_dict=rule_dict,
                ) -> int:
                    n = (
                        session.query(OutageRecord)
                        .filter(
                            OutageRecord.monitor_id == monitor_id,
                            OutageRecord.state == OUTAGE_OPEN,
                        )
                        .update(
                            {
                                OutageRecord.state: OUTAGE_PERSISTED,
                                OutageRecord.persisted_at: persisted_at,
                                OutageRecord.outage_id: outage_id,
                                OutageRecord.ci_id: ci_id,
                                OutageRecord.rule_json: json.dumps(
                                    rule_dict, sort_keys=True
                                ),
                                OutageRecord.updated_at: persisted_at,
                            },
                            synchronize_session=False,
                        )
                    )
                    session.commit()
                    return int(n or 0)

                if self.svc._retrying(session, _claim) != 1:
                    continue  # another instance reported it first
                stats["persisted"] += 1
                self._warned_no_ci.discard(monitor_id)
                payload: Dict[str, Any] = {
                    "outage_id": outage_id,
                    "ci_id": ci_id,
                    "kind": self.cache.kind_for(record, ci_id),
                    "name": record.name,
                    "host_ci_id": host_ci_id,
                    "monitor_id": monitor_id,
                    "since": mw.iso(down_since),
                    "persisted_at": mw.iso(now_aware),
                    "duration_s": int((now_aware - down_since).total_seconds()),
                    "rule": {
                        "offline_minutes": rule.offline_minutes,
                        "severity": rule.severity,
                        "auto_resolve": rule.auto_resolve,
                    },
                    "error": error or record.latest_error,
                }
                self.svc._emit(mw.TOPIC_OUTAGE_PERSISTED, payload)
        finally:
            session.close()
        if self.svc.housekeeping_here():
            try:
                self.store.purge_expired(now_aware)
            except Exception as exc:  # housekeeping only — never fail the evaluation
                self.log.debug(f"State Monitoring: override purge skipped: {exc}")
        if stats["persisted"]:
            self.svc._view_cache.clear()
        return stats

    def list_outages(self) -> List[Dict[str, Any]]:
        now = mw.utcnow()
        session = self.svc._session()
        if session is None:
            return []
        try:
            out = []
            rows = (
                session.query(OutageRecord)
                .filter(OutageRecord.state.in_([OUTAGE_OPEN, OUTAGE_PERSISTED]))
                .all()
            )
            for row in rows:
                record = self.svc._get_monitor(session, row.monitor_id)
                item = self.store.outage_to_dict(row)
                down_since = aware(row.down_since)
                effective_since = max(
                    down_since, aware(row.suppressed_until) or down_since
                )
                item["name"] = record.name if record else row.monitor_id
                item["effective_since"] = mw.iso(effective_since)
                item["effective_duration_s"] = max(
                    0, int((now - effective_since).total_seconds())
                )
                item["duration_s"] = max(0, int((now - down_since).total_seconds()))
                if record is not None:
                    ms = self.cache.state_for(record, now)
                    item["in_maintenance"] = ms.active
                    rule = self.cache.rule_for(record)
                    item["rule"] = item["rule"] or (
                        mw.rule_to_dict(rule) if rule else None
                    )
                    item["kind"] = self.cache.kind_for(record)
                out.append(item)
            out.sort(key=lambda i: i["down_since"] or "")
            return out
        finally:
            session.close()

    # -- read decoration (list_monitors / overview) --------------------------------------

    def decorate(
        self,
        payload: Dict[str, Any],
        record: Any,
        outages: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> None:
        """Add ``in_maintenance``, ``maintenance_until``, ``maintenance_source``,
        ``maintenance_next_start`` and ``outage_since`` to a serialised monitor."""
        ms = self.cache.state_for(record, now)
        payload["in_maintenance"] = ms.active
        payload["maintenance_until"] = mw.iso(ms.until) if ms.until else None
        payload["maintenance_source"] = ms.source
        payload["maintenance_next_start"] = (
            mw.iso(ms.next_start) if ms.next_start else None
        )
        outage = outages.get(str(payload.get("monitor_id") or ""))
        payload["outage_since"] = outage["down_since"] if outage else None
        payload["outage_state"] = outage["state"] if outage else None
