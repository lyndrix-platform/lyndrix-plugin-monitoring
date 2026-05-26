import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx

from core.api import db_instance

from ..model.models import (
    AdminOverride,
    InventorySyncPayload,
    MonitorDailyAggregate,
    MonitorHeartbeat,
    MonitorRecord,
    MonitorState,
    MonitorType,
    MonitorUpsert,
    PassiveResult,
    _aggregate_uptime_percent,
    _calculate_uptime_percentages,
    _day_start,
    _safe_json_load,
    _serialize_monitor,
    _utc_now,
)
from .scheduler import SimpleAsyncScheduler
from .provider.http import is_http_target, run_http_probe
from .provider.icmp import run_icmp_probe
from .provider.tcp import looks_like_network_target, run_tcp_probe, tcp_fallback_ports
from .provider.docker import is_docker_host, run_docker_service_probe


class MonitoringService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.scheduler = SimpleAsyncScheduler()
        self.http_client = httpx.AsyncClient(follow_redirects=True)
        self._scheduler_started = False
        self._probe_semaphore = asyncio.Semaphore(24)
        self._initial_probe_delay_seconds = 8
        self._initial_probe_worker_limit = 8
        self._initial_probe_batch_limit = 0
        self._inflight_probes: set = set()
        self._background_probe_task: Optional[asyncio.Task] = None
        self._bootstrap_task: Optional[asyncio.Task] = None
        self._inventory_sync_task: Optional[asyncio.Task] = None
        self._pending_inventory_payload: Optional[InventorySyncPayload] = None

    def _session(self):
        if not db_instance.is_connected or not db_instance.SessionLocal:
            return None
        return db_instance.SessionLocal()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._scheduler_started:
            return
        self.scheduler.add_daily_job(self.prune_heartbeats, hour=0, minute=0, id="monitoring:prune")
        self.scheduler.add_daily_job(self.refresh_rollups, hour=0, minute=5, id="monitoring:rollups")
        self.scheduler.start()
        self._scheduler_started = True

    # ------------------------------------------------------------------
    # Async-safe probe lifecycle helpers
    # ------------------------------------------------------------------

    def _load_probe_record(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        """Read the record fields needed for a probe from the DB (runs in a thread)."""
        session = self._session()
        if not session:
            return None
        try:
            record = self._get_monitor(session, monitor_id)
            if not record or not record.enabled:
                return None
            target = record.target or record.address
            if not target:
                return None
            return {
                "monitor_id": record.monitor_id,
                "monitor_type": record.monitor_type,
                "target": target,
                "address": record.address,
                "service_name": record.service_name,
                "timeout_seconds": record.timeout_seconds,
                "metadata": _safe_json_load(record.metadata_json),
            }
        finally:
            session.close()

    async def _dispatch_probe(self, rd: Dict[str, Any]) -> tuple:
        """Run the appropriate probe strategy and return (state, latency_ms, error_message)."""
        target = rd["target"]
        monitor_type = rd["monitor_type"]
        timeout_seconds = rd["timeout_seconds"]
        address = rd["address"]
        service_name = rd["service_name"]
        metadata = rd["metadata"]

        if monitor_type in {MonitorType.PING.value, MonitorType.SERVER.value}:
            result = await run_icmp_probe(target, timeout_seconds)
            state = result["state"]
            latency_ms = result["latency_ms"]
            error_message = result["error_message"]
            if state != MonitorState.UP and monitor_type == MonitorType.SERVER.value and looks_like_network_target(target):
                fallback_host = address or target.split(":", 1)[0]
                for port in tcp_fallback_ports(metadata, target):
                    tcp_result = await run_tcp_probe(fallback_host, port, timeout_seconds)
                    if tcp_result["state"] == MonitorState.UP:
                        return tcp_result["state"], tcp_result["latency_ms"], None
            return state, latency_ms, error_message

        if metadata.get("deploy_type") == "docker_compose" and address and service_name:
            result = await run_docker_service_probe(self.http_client, address, service_name, timeout_seconds)
            return result["state"], result["latency_ms"], result["error_message"]

        if is_http_target(target):
            result = await run_http_probe(self.http_client, target, timeout_seconds)
            return result["state"], result["latency_ms"], result["error_message"]

        if looks_like_network_target(target):
            result = await run_icmp_probe(target, timeout_seconds)
            return result["state"], result["latency_ms"], result["error_message"]

        return MonitorState.UNKNOWN, None, f"No supported probe strategy for target '{target}'"

    def _save_probe_result(
        self,
        monitor_id: str,
        state: "MonitorState",
        latency_ms: Optional[float],
        error_message: Optional[str],
    ):
        """Persist probe result to the DB (runs in a thread)."""
        session = self._session()
        if not session:
            return
        try:
            record = self._get_monitor(session, monitor_id)
            if not record:
                return
            self._store_heartbeat(session, record, state, latency_ms, error_message, "active_probe")
            session.commit()
        finally:
            session.close()

    async def stop(self):
        """Cancel all background tasks and release resources for clean reload."""
        # Yield once so every task that was created-but-not-yet-started gets
        # its first event-loop step.  Without this, tasks cancelled before they
        # ever run trigger Python's "coroutine was never awaited" RuntimeWarning
        # during GC at process shutdown.
        await asyncio.sleep(0)

        # Collect explicitly tracked one-off tasks
        tasks_to_cancel: list = []
        for attr in ("_bootstrap_task", "_background_probe_task", "_inventory_sync_task"):
            t = getattr(self, attr, None)
            if t and not t.done():
                tasks_to_cancel.append(t)

        # Collect all scheduled repeating jobs
        for handle in self.scheduler.get_jobs():
            if not handle.task.done():
                tasks_to_cancel.append(handle.task)

        # Also sweep asyncio's own task registry for anything named
        # "monitoring:*" that we might have missed (created between the
        # snapshot above and now, e.g. during a concurrent sync).
        tracked_ids = {id(t) for t in tasks_to_cancel}
        current = asyncio.current_task()
        for t in asyncio.all_tasks():
            if t is current or t.done():
                continue
            name = t.get_name() or ""
            if name.startswith("monitoring:") and id(t) not in tracked_ids:
                tasks_to_cancel.append(t)

        # Clear scheduler state immediately so no new tasks are spawned
        self.scheduler._jobs.clear()
        self.scheduler._started = False
        self._scheduler_started = False

        # Cancel everything, then wait briefly — the process is exiting anyway
        for t in tasks_to_cancel:
            t.cancel()

        if tasks_to_cancel:
            with suppress(Exception):
                await asyncio.wait(tasks_to_cancel, timeout=0.5)

        # Release the shared HTTP client
        with suppress(Exception):
            await self.http_client.aclose()

        self.ctx.log.info("State Monitoring: stopped cleanly.")

    def ensure_tables(self):
        from core.api import Base
        if db_instance.is_connected and db_instance.engine:
            Base.metadata.create_all(bind=db_instance.engine, checkfirst=True)

    def bootstrap(self):
        self.ensure_tables()
        self.sync_scheduler_jobs()
        self.refresh_rollups()
        self.enqueue_background_probe_refresh()

    def queue_bootstrap(self):
        if self._bootstrap_task and not self._bootstrap_task.done():
            return
        self._bootstrap_task = asyncio.create_task(self._bootstrap_async(), name="monitoring:bootstrap")

    async def _bootstrap_async(self):
        await asyncio.sleep(0.1)
        with suppress(Exception):
            await asyncio.to_thread(self.ensure_tables)
        await asyncio.sleep(0)
        with suppress(Exception):
            # sync_scheduler_jobs does DB reads + asyncio.create_task; run the
            # DB portion first in a thread, then the task-creation inline.
            await asyncio.to_thread(self._load_all_records_for_scheduler)
            self.sync_scheduler_jobs()
        await asyncio.sleep(0)
        with suppress(Exception):
            await self.refresh_rollups()
        await asyncio.sleep(0)
        self.enqueue_background_probe_refresh()

    def enqueue_background_probe_refresh(self):
        if self._background_probe_task and not self._background_probe_task.done():
            return
        self._background_probe_task = asyncio.create_task(
            self._run_probe_refresh_batch(), name="monitoring:probe_refresh"
        )

    # ------------------------------------------------------------------
    # Probe batch
    # ------------------------------------------------------------------

    def _probe_priority(self, record: MonitorRecord) -> int:
        metadata = _safe_json_load(record.metadata_json)
        if metadata.get("deploy_type") == "docker_compose":
            return 0
        if is_http_target(record.target):
            return 1
        if record.monitor_type == MonitorType.SERVICE.value:
            return 2
        return 3

    async def _run_probe_refresh_batch(self):
        await asyncio.sleep(self._initial_probe_delay_seconds)
        session = self._session()
        if not session:
            return
        try:
            records = session.query(MonitorRecord).filter(MonitorRecord.enabled.is_(True)).all()

            def _is_stale(r: MonitorRecord) -> bool:
                if r.last_checked_at is None:
                    return True
                from datetime import timezone as _tz
                ts = r.last_checked_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                return (_utc_now() - ts).total_seconds() > r.interval_seconds * 2

            candidates = [
                r for r in records
                if (r.target or r.address)
                and (
                    r.latest_state in {MonitorState.UNKNOWN.value, MonitorState.DOWN.value}
                    or _is_stale(r)
                )
            ]
            candidates.sort(key=lambda r: (self._probe_priority(r), r.updated_at or _utc_now()))
            if self._initial_probe_batch_limit > 0:
                candidates = candidates[: self._initial_probe_batch_limit]
            pending_ids = [r.monitor_id for r in candidates]
            worker_count = max(1, min(self._initial_probe_worker_limit, len(pending_ids)))

            if pending_ids:
                self.ctx.log.info(
                    f"State Monitoring: starting initial probe refresh for {len(pending_ids)} monitors with {worker_count} workers."
                )

            async def worker():
                while pending_ids:
                    mid = pending_ids.pop(0)
                    with suppress(Exception):
                        await self.run_scheduled_probe(mid)

            if worker_count:
                await asyncio.gather(*(worker() for _ in range(worker_count)), return_exceptions=True)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Inventory sync queue
    # ------------------------------------------------------------------

    def queue_inventory_sync(self, payload: InventorySyncPayload):
        self._pending_inventory_payload = payload
        if self._inventory_sync_task and not self._inventory_sync_task.done():
            return
        self._inventory_sync_task = asyncio.create_task(
            self._process_inventory_sync_queue(), name="monitoring:inventory_sync"
        )

    async def _process_inventory_sync_queue(self):
        await asyncio.sleep(0.5)
        while self._pending_inventory_payload is not None:
            payload = self._pending_inventory_payload
            self._pending_inventory_payload = None
            try:
                await asyncio.to_thread(self.ingest_inventory_snapshot, payload)
                await asyncio.sleep(0)
                # sync_scheduler_jobs does asyncio.create_task; run it on the event loop.
                self.sync_scheduler_jobs()
                await asyncio.sleep(0)
                self.enqueue_background_probe_refresh()
            except Exception as exc:
                self.ctx.log.error(f"State Monitoring: inventory sync processing failed: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _canonical_hash(self, payload: Dict[str, Any]) -> str:
        import json as _json
        from hashlib import sha256
        encoded = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    def _parse_timestamp(self, value: Optional[str]):
        from datetime import timezone
        if not value:
            return _utc_now()
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return _utc_now()

    def _get_monitor(self, session, monitor_id: str) -> Optional[MonitorRecord]:
        return session.query(MonitorRecord).filter(MonitorRecord.monitor_id == monitor_id).first()

    def _update_uptime_windows(self, session, monitor: MonitorRecord):
        from sqlalchemy import func
        for days, attr in [(1, "uptime_24h"), (7, "uptime_7d"), (30, "uptime_30d")]:
            cutoff = _utc_now() - timedelta(days=days)
            total = session.query(func.count(MonitorHeartbeat.id)).filter(
                MonitorHeartbeat.monitor_id == monitor.monitor_id,
                MonitorHeartbeat.created_at >= cutoff,
                MonitorHeartbeat.state != MonitorState.PAUSED.value,
            ).scalar() or 0
            if total == 0:
                setattr(monitor, attr, 100.0)
                continue
            up_total = session.query(func.count(MonitorHeartbeat.id)).filter(
                MonitorHeartbeat.monitor_id == monitor.monitor_id,
                MonitorHeartbeat.created_at >= cutoff,
                MonitorHeartbeat.state == MonitorState.UP.value,
            ).scalar() or 0
            setattr(monitor, attr, round((up_total / total) * 100.0, 2))

    def _update_daily_aggregate(self, session, monitor: MonitorRecord):
        day = _day_start(_utc_now())
        next_day = day + timedelta(days=1)
        items = session.query(MonitorHeartbeat).filter(
            MonitorHeartbeat.monitor_id == monitor.monitor_id,
            MonitorHeartbeat.created_at >= day,
            MonitorHeartbeat.created_at < next_day,
        ).all()
        aggregate = session.query(MonitorDailyAggregate).filter(
            MonitorDailyAggregate.monitor_id == monitor.monitor_id,
            MonitorDailyAggregate.day == day,
        ).first()
        if aggregate is None:
            aggregate = MonitorDailyAggregate(monitor_id=monitor.monitor_id, day=day)
            session.add(aggregate)
        aggregate.total_samples = len(items)
        aggregate.up_samples = sum(1 for i in items if i.state == MonitorState.UP.value)
        aggregate.down_samples = sum(1 for i in items if i.state == MonitorState.DOWN.value)
        aggregate.paused_samples = sum(1 for i in items if i.state == MonitorState.PAUSED.value)
        latencies = [i.latency_ms for i in items if i.latency_ms is not None]
        aggregate.avg_latency_ms = round(sum(latencies) / len(latencies), 2) if latencies else None
        aggregate.updated_at = _utc_now()
        self._update_uptime_windows(session, monitor)

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        from sqlalchemy import func
        session = self._session()
        if not session:
            return {"monitor_count": 0, "up_count": 0, "down_count": 0, "paused_count": 0, "uptime_all": 100.0}
        try:
            rows = session.query(MonitorRecord.latest_state, func.count(MonitorRecord.monitor_id)).group_by(MonitorRecord.latest_state).all()
            monitor_count = session.query(func.count(MonitorRecord.monitor_id)).scalar() or 0
            result = {"monitor_count": monitor_count, "up_count": 0, "down_count": 0, "paused_count": 0, "uptime_all": 100.0}
            for state, count in rows:
                if state == MonitorState.UP.value:
                    result["up_count"] = count
                elif state == MonitorState.DOWN.value:
                    result["down_count"] = count
                elif state == MonitorState.PAUSED.value:
                    result["paused_count"] = count
            monitor_ids = [r[0] for r in session.query(MonitorRecord.monitor_id).all()]
            uptime_map = _calculate_uptime_percentages(session, monitor_ids)
            if uptime_map:
                result["uptime_all"] = _aggregate_uptime_percent(list(uptime_map.values()))
            return result
        finally:
            session.close()

    def list_monitors(self) -> List[Dict[str, Any]]:
        session = self._session()
        if not session:
            return []
        try:
            items = session.query(MonitorRecord).order_by(MonitorRecord.logical_group, MonitorRecord.name).all()
            uptime_map = _calculate_uptime_percentages(session, [i.monitor_id for i in items])
            serialized = []
            for item in items:
                payload = _serialize_monitor(item)
                payload["uptime_all"] = uptime_map.get(item.monitor_id, 100.0)
                serialized.append(payload)
            return serialized
        finally:
            session.close()

    def get_monitor(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        session = self._session()
        if not session:
            return None
        try:
            item = self._get_monitor(session, monitor_id)
            return _serialize_monitor(item) if item else None
        finally:
            session.close()

    def get_history(self, monitor_id: str, limit_hours: int = 24) -> List[Dict[str, Any]]:
        session = self._session()
        if not session:
            return []
        try:
            cutoff = _utc_now() - timedelta(hours=limit_hours)
            items = session.query(MonitorHeartbeat).filter(
                MonitorHeartbeat.monitor_id == monitor_id,
                MonitorHeartbeat.created_at >= cutoff,
            ).order_by(MonitorHeartbeat.created_at.asc()).all()
            return [
                {
                    "state": i.state,
                    "latency_ms": i.latency_ms,
                    "error_message": i.error_message,
                    "source": i.source,
                    "timestamp": i.created_at.isoformat() if i.created_at else None,
                }
                for i in items
            ]
        finally:
            session.close()

    def get_histories(self, monitor_ids: List[str], limit_hours: int = 24) -> Dict[str, List[Dict[str, Any]]]:
        if not monitor_ids:
            return {}
        session = self._session()
        if not session:
            return {mid: [] for mid in monitor_ids}
        try:
            cutoff = _utc_now() - timedelta(hours=limit_hours)
            items = session.query(MonitorHeartbeat).filter(
                MonitorHeartbeat.monitor_id.in_(monitor_ids),
                MonitorHeartbeat.created_at >= cutoff,
            ).order_by(MonitorHeartbeat.monitor_id.asc(), MonitorHeartbeat.created_at.asc()).all()
            grouped = {mid: [] for mid in monitor_ids}
            for i in items:
                grouped.setdefault(i.monitor_id, []).append(
                    {
                        "state": i.state,
                        "latency_ms": i.latency_ms,
                        "error_message": i.error_message,
                        "source": i.source,
                        "timestamp": i.created_at.isoformat() if i.created_at else None,
                    }
                )
            return grouped
        finally:
            session.close()

    # ------------------------------------------------------------------
    # State transitions & heartbeats
    # ------------------------------------------------------------------

    def _emit_transition(self, record: MonitorRecord, previous_state: str, error_message: Optional[str]):
        if previous_state == record.latest_state:
            return
        self.ctx.emit(
            "monitoring:state_changed",
            {
                "monitor_id": record.monitor_id,
                "previous_state": previous_state,
                "new_state": record.latest_state,
                "timestamp": _utc_now().isoformat(),
                "error_message": error_message,
            },
        )

    def _store_heartbeat(
        self,
        session,
        record: MonitorRecord,
        state: MonitorState,
        latency_ms: Optional[float],
        error_message: Optional[str],
        source: str,
        timestamp=None,
    ):
        current_time = timestamp or _utc_now()
        previous_state = record.latest_state
        record.latest_state = state.value
        record.latest_latency_ms = latency_ms
        record.latest_error = error_message
        record.last_checked_at = current_time
        record.updated_at = _utc_now()
        session.add(
            MonitorHeartbeat(
                monitor_id=record.monitor_id,
                state=state.value,
                latency_ms=latency_ms,
                error_message=error_message,
                source=source,
                created_at=current_time,
            )
        )
        self._update_daily_aggregate(session, record)
        self._emit_transition(record, previous_state, error_message)

    # ------------------------------------------------------------------
    # Scheduler sync
    # ------------------------------------------------------------------

    def _sync_job_for_monitor(self, record: MonitorRecord):
        if not self._scheduler_started:
            return
        job_id = f"monitor:{record.monitor_id}"
        schedulable = {MonitorType.PING.value, MonitorType.HTTP.value, MonitorType.SERVER.value, MonitorType.SERVICE.value}
        if (
            not record.enabled
            or record.latest_state == MonitorState.PAUSED.value
            or record.monitor_type not in schedulable
            or not (record.target or record.address)
        ):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            return
        self.scheduler.add_interval_job(
            self.run_scheduled_probe,
            seconds=record.interval_seconds,
            args=[record.monitor_id],
            id=job_id,
            replace_existing=True,
        )

    def sync_scheduler_jobs(self):
        session = self._session()
        if not session or not self._scheduler_started:
            return
        try:
            active_ids = set()
            for record in session.query(MonitorRecord).all():
                active_ids.add(f"monitor:{record.monitor_id}")
                self._sync_job_for_monitor(record)
            for job in self.scheduler.get_jobs():
                if job.id.startswith("monitor:") and job.id not in active_ids:
                    self.scheduler.remove_job(job.id)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def upsert_monitor(self, data: MonitorUpsert, sync_scheduler: bool = True) -> Dict[str, Any]:
        session = self._session()
        if not session:
            raise RuntimeError("Database unavailable")
        try:
            self.ensure_tables()
            record = self._get_monitor(session, data.monitor_id)
            payload = data.model_dump(exclude={"force"}, mode="json")
            desired_hash = data.source_hash or self._canonical_hash(payload)
            metadata_json = json.dumps(data.metadata, sort_keys=True)

            if record:
                if record.owner_source != data.owner_source and not data.force:
                    raise ValueError(f"Monitor '{data.monitor_id}' is owned by '{record.owner_source}'")
                if record.is_admin_overridden and record.owner_source == data.owner_source:
                    record.is_admin_overridden = False
                if record.config_hash == desired_hash and record.owner_source == data.owner_source:
                    return _serialize_monitor(record)
                record.name = data.name
                record.monitor_type = data.monitor_type.value
                record.owner_source = data.owner_source
                record.target = data.target
                record.address = data.address
                record.host_name = data.host_name
                record.service_name = data.service_name
                record.logical_group = data.logical_group
                record.interval_seconds = data.interval_seconds
                record.timeout_seconds = data.timeout_seconds
                record.enabled = data.enabled
                record.source_hash = data.source_hash
                record.config_hash = desired_hash
                record.metadata_json = metadata_json
                record.updated_at = _utc_now()
                if not data.enabled:
                    record.latest_state = MonitorState.PAUSED.value
            else:
                record = MonitorRecord(
                    monitor_id=data.monitor_id,
                    name=data.name,
                    monitor_type=data.monitor_type.value,
                    owner_source=data.owner_source,
                    target=data.target,
                    address=data.address,
                    host_name=data.host_name,
                    service_name=data.service_name,
                    logical_group=data.logical_group,
                    interval_seconds=data.interval_seconds,
                    timeout_seconds=data.timeout_seconds,
                    enabled=data.enabled,
                    source_hash=data.source_hash,
                    config_hash=desired_hash,
                    metadata_json=metadata_json,
                    latest_state=MonitorState.UNKNOWN.value if data.enabled else MonitorState.PAUSED.value,
                    created_at=_utc_now(),
                    updated_at=_utc_now(),
                )
                session.add(record)
            session.commit()
            session.refresh(record)
            if sync_scheduler:
                self._sync_job_for_monitor(record)
            return _serialize_monitor(record)
        finally:
            session.close()

    def apply_admin_override(self, request: AdminOverride) -> Dict[str, Any]:
        session = self._session()
        if not session:
            raise RuntimeError("Database unavailable")
        try:
            record = self._get_monitor(session, request.monitor_id)
            if not record:
                raise KeyError(request.monitor_id)
            record.is_admin_overridden = True
            record.updated_at = _utc_now()
            if request.action == "pause":
                previous_state = record.latest_state
                record.enabled = False
                record.latest_state = MonitorState.PAUSED.value
                record.latest_error = None
                self._emit_transition(record, previous_state, None)
            elif request.action == "resume":
                record.enabled = True
                if record.latest_state == MonitorState.PAUSED.value:
                    record.latest_state = MonitorState.UNKNOWN.value
            elif request.action == "update":
                allowed = {"name", "target", "address", "host_name", "service_name", "logical_group", "interval_seconds", "timeout_seconds", "enabled"}
                for key, value in request.patch.items():
                    if key in allowed:
                        setattr(record, key, value)
                metadata_patch = request.patch.get("metadata")
                if isinstance(metadata_patch, dict):
                    merged = _safe_json_load(record.metadata_json)
                    merged.update(metadata_patch)
                    record.metadata_json = json.dumps(merged, sort_keys=True)
            session.commit()
            session.refresh(record)
            self._sync_job_for_monitor(record)
            return _serialize_monitor(record)
        finally:
            session.close()

    def ingest_passive_result(self, payload: PassiveResult) -> Dict[str, Any]:
        session = self._session()
        if not session:
            raise RuntimeError("Database unavailable")
        try:
            record = self._get_monitor(session, payload.monitor_id)
            if not record:
                raise KeyError(payload.monitor_id)
            self._store_heartbeat(
                session, record, payload.state, payload.latency_ms,
                payload.error_message, payload.source, self._parse_timestamp(payload.timestamp)
            )
            session.commit()
            session.refresh(record)
            return _serialize_monitor(record)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def prune_heartbeats(self):
        await asyncio.to_thread(self._prune_heartbeats_sync)

    def _prune_heartbeats_sync(self):
        session = self._session()
        if not session:
            return
        try:
            cutoff = _utc_now() - timedelta(days=7)
            session.query(MonitorHeartbeat).filter(MonitorHeartbeat.created_at < cutoff).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def clear_states_db(self) -> int:
        """Delete ALL heartbeat + aggregate records and reset every monitor to UNKNOWN.
        Returns the number of heartbeat rows deleted."""
        session = self._session()
        if not session:
            return 0
        try:
            deleted = session.query(MonitorHeartbeat).delete(synchronize_session=False)
            session.query(MonitorDailyAggregate).delete(synchronize_session=False)
            for record in session.query(MonitorRecord).all():
                record.latest_state = MonitorState.UNKNOWN.value
                record.uptime_24h = 100.0
                record.uptime_7d = 100.0
                record.uptime_30d = 100.0
                record.last_checked_at = None
                record.latest_error = None
            session.commit()
            return deleted
        finally:
            session.close()

    async def refresh_rollups(self):
        await asyncio.to_thread(self._refresh_rollups_sync)

    def _refresh_rollups_sync(self):
        session = self._session()
        if not session:
            return
        try:
            for record in session.query(MonitorRecord).all():
                self._update_daily_aggregate(session, record)
            session.commit()
        finally:
            session.close()

    def _load_all_records_for_scheduler(self):
        """Warm the DB connection pool; actual task creation happens in sync code."""
        session = self._session()
        if not session:
            return
        try:
            _ = session.query(MonitorRecord).count()
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Active probe
    # ------------------------------------------------------------------

    async def run_scheduled_probe(self, monitor_id: str):
        """Run a single probe without blocking the event loop.

        Phase 1 – DB read  (thread): load record fields.
        Phase 2 – Probe    (async):  network I/O via semaphore.
        Phase 3 – DB write (thread): persist heartbeat + aggregates.
        """
        if monitor_id in self._inflight_probes:
            return
        self._inflight_probes.add(monitor_id)
        try:
            # Phase 1: read from DB in a worker thread so the event loop stays free.
            record_data = await asyncio.to_thread(self._load_probe_record, monitor_id)
            if not record_data:
                return

            # Phase 2: async network probe (already non-blocking).
            state = MonitorState.UNKNOWN
            latency_ms = None
            error_message = None
            try:
                async with self._probe_semaphore:
                    state, latency_ms, error_message = await self._dispatch_probe(record_data)
            except Exception as exc:
                state = MonitorState.DOWN
                error_message = str(exc)

            # Phase 3: write result + aggregates in a worker thread.
            await asyncio.to_thread(self._save_probe_result, monitor_id, state, latency_ms, error_message)
        finally:
            self._inflight_probes.discard(monitor_id)

    # ------------------------------------------------------------------
    # Inventory sync
    # ------------------------------------------------------------------

    def ingest_inventory_snapshot(self, payload: InventorySyncPayload) -> Dict[str, int]:
        upserts = 0
        for host in payload.hosts:
            host_name = host.get("host_name") or host.get("hostname")
            address = host.get("address") or host.get("ansible_host")
            if not host_name:
                continue
            self.upsert_monitor(MonitorUpsert(
                monitor_id=f"host:{host_name}",
                name=host_name,
                monitor_type=MonitorType.SERVER,
                owner_source=payload.owner_source,
                target=address,
                address=address,
                host_name=host_name,
                logical_group=host.get("site") or host.get("stage") or "iac",
                source_hash=payload.source_revision,
                metadata={
                    "stage": host.get("stage"),
                    "groups": host.get("groups") or host.get("ansible_groups") or [],
                    "baseline_roles": host.get("baseline_roles") or [],
                    "profiles": host.get("profiles") or [],
                    "terraform": host.get("terraform") or {},
                },
            ), sync_scheduler=False)
            upserts += 1

        for svc in payload.services:
            host_name = svc.get("host_name") or svc.get("hostname")
            service_name = svc.get("service_name") or svc.get("name")
            if not host_name or not service_name:
                continue
            target = svc.get("url") or svc.get("target") or svc.get("address")
            monitor_type = MonitorType.HTTP if target and str(target).startswith(("http://", "https://")) else MonitorType.SERVICE
            self.upsert_monitor(MonitorUpsert(
                monitor_id=f"service:{host_name}:{service_name}",
                name=f"{host_name}:{service_name}",
                monitor_type=monitor_type,
                owner_source=payload.owner_source,
                target=target,
                address=svc.get("address"),
                host_name=host_name,
                service_name=service_name,
                logical_group=svc.get("site") or svc.get("stage") or "iac",
                source_hash=payload.source_revision,
                enabled=bool(target),
                metadata={
                    "site": svc.get("site"),
                    "stage": svc.get("stage"),
                    "groups": svc.get("groups") or svc.get("ansible_groups") or [],
                    "deploy_type": svc.get("deploy_type"),
                    "desired_state": svc.get("desired_state") or svc.get("state"),
                    "git_repo": svc.get("git_repo"),
                    "git_version": svc.get("git_version"),
                    "config": svc.get("config") or {},
                },
            ), sync_scheduler=False)
            upserts += 1

        # Delete monitors that were previously owned by this source but are no
        # longer present in the current payload (host/service removed upstream).
        fresh_ids = set()
        for host in payload.hosts:
            hn = host.get("host_name") or host.get("hostname")
            if hn:
                fresh_ids.add(f"host:{hn}")
        for svc in payload.services:
            hn = svc.get("host_name") or svc.get("hostname")
            sn = svc.get("service_name") or svc.get("name")
            if hn and sn:
                fresh_ids.add(f"service:{hn}:{sn}")

        stale_deleted = 0
        legacy_owner_sources = {payload.owner_source, "orchestrator_service", "iac_orchestrator"}
        session_stale = self._session()
        if session_stale:
            try:
                stale = (
                    session_stale.query(MonitorRecord)
                    .filter(
                        MonitorRecord.owner_source.in_(legacy_owner_sources),
                        MonitorRecord.monitor_id.like("host:%")
                        | MonitorRecord.monitor_id.like("service:%"),
                    )
                    .all()
                )
                stale_ids = [rec.monitor_id for rec in stale if rec.monitor_id not in fresh_ids]
                if stale_ids:
                    session_stale.query(MonitorHeartbeat).filter(
                        MonitorHeartbeat.monitor_id.in_(stale_ids)
                    ).delete(synchronize_session=False)
                    session_stale.query(MonitorDailyAggregate).filter(
                        MonitorDailyAggregate.monitor_id.in_(stale_ids)
                    ).delete(synchronize_session=False)
                    session_stale.query(MonitorRecord).filter(
                        MonitorRecord.monitor_id.in_(stale_ids)
                    ).delete(synchronize_session=False)
                    stale_deleted = len(stale_ids)
                    session_stale.commit()
            finally:
                session_stale.close()

        return {"upserts": upserts, "stale_deleted": stale_deleted, "source_revision": payload.source_revision}
