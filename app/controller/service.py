import asyncio
import json
import time
from contextlib import nullcontext, suppress
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import case, func, text

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
    _day_start,
    _hourly_state_counts,
    _safe_json_load,
    _serialize_monitor,
    _uptime_from_aggregates,
    _utc_now,
)
from ..ui.styles import aggregate_state
from .scheduler import SimpleAsyncScheduler
from .provider.http import is_http_target, run_http_probe
from .provider.icmp import run_icmp_probe
from .provider.tcp import looks_like_network_target, run_tcp_probe, tcp_fallback_ports
from .provider.docker import is_docker_host, run_docker_service_probe


class MonitoringService:
    # "uptime_all" is a rolling window over the daily aggregates — raw heartbeats
    # only live for the retention period, so an all-time scan was never all-time.
    UPTIME_ALL_DAYS = 30
    PRUNE_BATCH = 20_000

    def __init__(self, ctx):
        self.ctx = ctx
        self.scheduler = SimpleAsyncScheduler()
        # Redirects are followed manually by the http provider (run_http_probe)
        # so every hop can be re-validated against the SSRF guard — a client
        # with follow_redirects=True would chase a monitored host's
        # 302 -> internal/metadata target without ever checking it.
        self.http_client = httpx.AsyncClient(follow_redirects=False)
        self._scheduler_started = False
        self._probe_semaphore = asyncio.Semaphore(24)
        self._initial_probe_delay_seconds = 8
        self._initial_probe_worker_limit = 8
        self._initial_probe_batch_limit = 0
        self._inflight_probes: set = set()
        self._background_probe_task: Optional[asyncio.Task] = None
        self._bootstrap_task: Optional[asyncio.Task] = None
        self._inventory_sync_task: Optional[asyncio.Task] = None
        self._startup_prune_task: Optional[asyncio.Task] = None
        # Operator settings (manifest settings_schema, applied on vault:ready_for_data).
        self._heartbeat_retention_days = 7
        self._overview_cache_seconds = 10
        # Set by stop(): sweep workers stop taking monitors, housekeeping stops.
        self._stopping = False
        # Decided by _load_probe_candidates(): the sweep only blocks the UI when it
        # has monitors we know nothing about (UNKNOWN or stale) — re-probing a set
        # of persistently DOWN monitors after every restart is not a warm-up.
        self._sweep_blocking = True
        # Single-flight cache for the computed views (see _cached()): N pollers
        # share one computation and re-use it for _overview_cache_seconds.
        self._view_cache: Dict[str, tuple] = {}
        self._view_inflight: Dict[str, asyncio.Future] = {}
        self._pending_inventory_payload: Optional[InventorySyncPayload] = None

        # --- Backend-failure resilience (keeps a slow/broken backend from
        # flooding the single event loop with probes + state_changed events;
        # critical when prod+test are monitored together = hundreds of monitors) ---
        self._consec_failures: Dict[str, int] = {}        # monitor_id -> consecutive failures
        self._host_failures: Dict[str, int] = {}          # host_key  -> consecutive failures
        self._host_circuit_until: Dict[str, float] = {}   # host_key  -> monotonic deadline while paused
        self._flap_threshold = 3                          # consecutive fails before a monitor flips to DOWN
        self._host_circuit_threshold = 8                  # consecutive host fails before the host is paused
        self._host_circuit_base = 30.0                    # circuit backoff base (seconds)
        self._host_circuit_max = 1800.0                   # circuit backoff cap (seconds)

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
        # Housekeeping that must not depend on the process surviving until
        # midnight (every restart used to reset the daily jobs → heartbeats grew
        # for months). First runs are delayed so they never compete with the
        # startup sweep; the sweep itself schedules the first prune.
        self.scheduler.add_interval_job(
            self.prune_heartbeats, seconds=3600, id="monitoring:prune_hourly", initial_delay=3600
        )
        self.scheduler.add_interval_job(
            self.refresh_uptime_windows, seconds=3600, id="monitoring:uptime_hourly", initial_delay=1800
        )
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
                "host_name": record.host_name,
                "latest_state": record.latest_state,
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
        self._stopping = True  # sweep workers stop taking monitors, housekeeping stops
        await asyncio.sleep(0)

        # Collect explicitly tracked one-off tasks
        tasks_to_cancel: list = []
        for attr in ("_bootstrap_task", "_background_probe_task", "_inventory_sync_task", "_startup_prune_task"):
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

        # Probes running in worker threads cannot be cancelled — give them up to
        # 5 s to drain so a reload does not run two sweeps against the DB.
        for _ in range(50):
            if not self._inflight_probes:
                break
            await asyncio.sleep(0.1)

        # Release the shared HTTP client
        with suppress(Exception):
            await self.http_client.aclose()

        self._set_status("ready")
        self.ctx.log.info("State Monitoring: stopped cleanly.")

    def ensure_tables(self):
        from core.api import Base
        if db_instance.is_connected and db_instance.engine:
            Base.metadata.create_all(bind=db_instance.engine, checkfirst=True)
            self._ensure_schema(db_instance.engine)

    @staticmethod
    def _ensure_schema(engine) -> None:
        """Hand-rolled, idempotent migration: the composite index that turns the
        24 h window queries into an index range scan, and the latency_samples
        counter on daily aggregates. A dialect without IF NOT EXISTS for ALTER
        (SQLite in tests — the column already comes from create_all) just skips."""
        statements = [
            "CREATE INDEX IF NOT EXISTS ix_monitoring_heartbeats_monitor_created "
            "ON monitoring_heartbeats (monitor_id, created_at)",
            "ALTER TABLE monitoring_daily_aggregates "
            "ADD COLUMN IF NOT EXISTS latency_samples INTEGER NOT NULL DEFAULT 0",
        ]
        for stmt in statements:
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception:
                pass

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
            # DB read off the loop; scheduler task-creation stays on the loop.
            records = await asyncio.to_thread(self._snapshot_records_for_scheduler)
            self.sync_scheduler_jobs_from(records)
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

    def _load_probe_candidates(self) -> list:
        """Sync helper — run via asyncio.to_thread to avoid blocking the event loop."""
        session = self._session()
        if not session:
            return []
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
            self._sweep_blocking = any(
                r.latest_state == MonitorState.UNKNOWN.value or _is_stale(r) for r in candidates
            )
            return [r.monitor_id for r in candidates]
        finally:
            session.close()

    async def _run_probe_refresh_batch(self):
        await asyncio.sleep(self._initial_probe_delay_seconds)
        if self._stopping:
            return

        pending_ids = await asyncio.to_thread(self._load_probe_candidates)
        total = len(pending_ids)
        if not pending_ids:
            self._schedule_startup_prune()
            return

        worker_count = max(1, min(self._initial_probe_worker_limit, total))
        self.ctx.log.info(
            f"State Monitoring: starting initial probe refresh for {total} monitors with {worker_count} workers."
        )
        # Startup sequence (core API >= 1.5): the shell shows a warm-up panel with
        # progress and the core answers our API with 503 + Retry-After until the
        # sweep is done. On a warm restart there is nothing stale → no window.
        message = f"Initial probe sweep: {total} monitors"
        blocking = self._sweep_blocking
        done = 0
        self._set_status("starting", message=message, progress=(0, total), blocking=blocking, retry_after=15)

        async def worker():
            nonlocal done
            while pending_ids and not self._stopping:
                mid = pending_ids.pop(0)
                with suppress(Exception):
                    await self.run_scheduled_probe(mid)
                done += 1
                # The core coalesces progress-only updates to one per second.
                self._set_status("starting", message=message, progress=(done, total), blocking=blocking, retry_after=15)

        try:
            await asyncio.gather(*(worker() for _ in range(worker_count)), return_exceptions=True)
        finally:
            self._set_status("ready")
            self._view_cache.clear()
        self.ctx.log.info(f"State Monitoring: initial probe refresh finished ({done}/{total}).")
        self._schedule_startup_prune()

    # ------------------------------------------------------------------
    # Runtime status (startup sequence / maintenance window)
    # ------------------------------------------------------------------

    def _set_status(self, phase: str, **kw) -> None:
        """Report the runtime phase to the core; a no-op on cores without
        ``ctx.set_status`` (API < 1.5) so the plugin keeps loading there."""
        fn = getattr(self.ctx, "set_status", None)
        if fn is None:
            return
        try:
            fn(phase, **kw)
        except Exception as exc:
            self.ctx.log.debug(f"State Monitoring: set_status failed: {exc}")

    def maintenance_window(self, message: str, *, blocking: bool = False):
        """``async with service.maintenance_window("…")``: a core-visible
        maintenance status for the duration (banner; ``blocking=True`` → 503s)."""
        fn = getattr(self.ctx, "maintenance", None)
        return fn(message, blocking=blocking) if fn else nullcontext()

    def _schedule_startup_prune(self) -> None:
        """First retention run one minute after the sweep (the daily 00:00 job
        never fired on a process that was restarted before midnight)."""
        if self._startup_prune_task and not self._startup_prune_task.done():
            return

        async def _later():
            await asyncio.sleep(60)
            if not self._stopping:
                await self.prune_heartbeats()

        self._startup_prune_task = asyncio.create_task(_later(), name="monitoring:startup_prune")

    def apply_settings(self, values: Dict[str, Any]) -> None:
        """Operator settings from the manifest ``settings_schema`` (Vault-backed)."""
        def _int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(values.get(key) or default)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, value))

        self._heartbeat_retention_days = _int("heartbeat_retention_days", 7, 1, 365)
        self._initial_probe_worker_limit = _int("initial_probe_workers", 8, 1, 64)
        self._overview_cache_seconds = _int("overview_cache_seconds", 10, 0, 300)

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
                async with self.maintenance_window("Importing inventory"):
                    await asyncio.to_thread(self.ingest_inventory_snapshot, payload)
                await asyncio.sleep(0)
                # DB read off the loop; scheduler task-creation stays on the loop.
                records = await asyncio.to_thread(self._snapshot_records_for_scheduler)
                self.sync_scheduler_jobs_from(records)
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

    def _bump_daily_aggregate(self, session, monitor: MonitorRecord, state: "MonitorState", latency_ms: Optional[float]):
        """Incremental per-heartbeat update of today's aggregate — counters and a
        running latency mean. The old path re-read the whole day and ran six
        COUNTs per probe; with 200 monitors that alone saturated the database."""
        day = _day_start(_utc_now())
        aggregate = session.query(MonitorDailyAggregate).filter(
            MonitorDailyAggregate.monitor_id == monitor.monitor_id,
            MonitorDailyAggregate.day == day,
        ).first()
        if aggregate is None:
            aggregate = MonitorDailyAggregate(
                monitor_id=monitor.monitor_id, day=day, total_samples=0, up_samples=0,
                down_samples=0, paused_samples=0, latency_samples=0,
            )
            session.add(aggregate)
        aggregate.total_samples = (aggregate.total_samples or 0) + 1
        if state == MonitorState.UP:
            aggregate.up_samples = (aggregate.up_samples or 0) + 1
        elif state == MonitorState.DOWN:
            aggregate.down_samples = (aggregate.down_samples or 0) + 1
        elif state == MonitorState.PAUSED:
            aggregate.paused_samples = (aggregate.paused_samples or 0) + 1
        if latency_ms is not None:
            n = aggregate.latency_samples or 0
            prev = float(aggregate.avg_latency_ms) if n > 0 and aggregate.avg_latency_ms is not None else 0.0
            aggregate.avg_latency_ms = round((prev * n + float(latency_ms)) / (n + 1), 2)
            aggregate.latency_samples = n + 1
        aggregate.updated_at = _utc_now()

    def _recompute_daily_aggregate(self, session, monitor: MonitorRecord):
        """Full recompute of today's aggregate from heartbeats (daily rollup job —
        self-heals the incremental counters)."""
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
        aggregate.latency_samples = len(latencies)
        aggregate.updated_at = _utc_now()

    async def refresh_uptime_windows(self):
        await asyncio.to_thread(self._refresh_uptime_windows_sync)

    def _refresh_uptime_windows_sync(self):
        """uptime_24h from the last 24 h of heartbeats (one GROUP BY over the
        composite index), uptime_7d/30d from daily aggregates — for all monitors
        in one pass. Runs hourly and after the daily rollup, never per write."""
        session = self._session()
        if not session:
            return
        try:
            ids = [r[0] for r in session.query(MonitorRecord.monitor_id).all()]
            if not ids:
                return
            cutoff = (_utc_now() - timedelta(hours=24)).replace(tzinfo=None)
            rows = session.query(
                MonitorHeartbeat.monitor_id,
                func.count(MonitorHeartbeat.id),
                func.sum(case((MonitorHeartbeat.state == MonitorState.UP.value, 1), else_=0)),
            ).filter(
                MonitorHeartbeat.created_at >= cutoff,
                MonitorHeartbeat.state != MonitorState.PAUSED.value,
            ).group_by(MonitorHeartbeat.monitor_id).all()
            day = {
                mid: (round((int(up or 0) / int(total)) * 100.0, 2) if total else 100.0)
                for mid, total, up in rows
            }
            week = _uptime_from_aggregates(session, ids, days=7)
            month = _uptime_from_aggregates(session, ids, days=30)
            for record in session.query(MonitorRecord).all():
                record.uptime_24h = day.get(record.monitor_id, 100.0)
                record.uptime_7d = week.get(record.monitor_id, 100.0)
                record.uptime_30d = month.get(record.monitor_id, 100.0)
            session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def status_counts(self) -> Dict[str, int]:
        """Cheap by-state counts from MonitorRecord only — a single GROUP BY, no
        heartbeat aggregation. Used by the dashboard widget, which must stay light
        (unlike stats(), which also computes uptime over MonitorHeartbeat)."""
        from sqlalchemy import func
        session = self._session()
        result = {"monitor_count": 0, "up_count": 0, "down_count": 0, "paused_count": 0}
        if not session:
            return result
        try:
            rows = (
                session.query(MonitorRecord.latest_state, func.count(MonitorRecord.monitor_id))
                .group_by(MonitorRecord.latest_state)
                .all()
            )
            for state, count in rows:
                count = int(count or 0)
                result["monitor_count"] += count
                if state == MonitorState.UP.value:
                    result["up_count"] = count
                elif state == MonitorState.DOWN.value:
                    result["down_count"] = count
                elif state == MonitorState.PAUSED.value:
                    result["paused_count"] = count
            return result
        finally:
            session.close()

    def stats(self) -> Dict[str, Any]:
        """Counts by state (one GROUP BY on the monitor table) plus the rolling
        ``UPTIME_ALL_DAYS`` uptime from daily aggregates. No heartbeat scan."""
        session = self._session()
        if not session:
            return {"monitor_count": 0, "up_count": 0, "down_count": 0, "paused_count": 0, "uptime_all": 100.0}
        try:
            rows = session.query(MonitorRecord.latest_state, func.count(MonitorRecord.monitor_id)).group_by(MonitorRecord.latest_state).all()
            result = {"monitor_count": 0, "up_count": 0, "down_count": 0, "paused_count": 0, "uptime_all": 100.0}
            for state, count in rows:
                count = int(count or 0)
                result["monitor_count"] += count
                if state == MonitorState.UP.value:
                    result["up_count"] = count
                elif state == MonitorState.DOWN.value:
                    result["down_count"] = count
                elif state == MonitorState.PAUSED.value:
                    result["paused_count"] = count
            monitor_ids = [r[0] for r in session.query(MonitorRecord.monitor_id).all()]
            uptime_map = _uptime_from_aggregates(session, monitor_ids, days=self.UPTIME_ALL_DAYS)
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
            uptime_map = _uptime_from_aggregates(session, [i.monitor_id for i in items], days=self.UPTIME_ALL_DAYS)
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

    def get_timelines(self, monitor_ids: List[str], hours: int = 24) -> Dict[str, List[str]]:
        """Hourly state timeline per monitor, bucketed by the database (at most
        hours × monitors result rows) — same result as bucketing the raw
        heartbeats with ``timeline_from_history`` but without loading them."""
        hours = max(1, int(hours))
        unknown = [MonitorState.UNKNOWN.value] * hours
        session = self._session()
        if not session:
            return {mid: list(unknown) for mid in monitor_ids}
        try:
            counts = _hourly_state_counts(session, monitor_ids, hours)
        finally:
            session.close()
        return {
            mid: [aggregate_state([s for s, n in bucket.items() if n > 0]) if bucket else MonitorState.UNKNOWN.value
                  for bucket in buckets]
            for mid, buckets in counts.items()
        }

    # ------------------------------------------------------------------
    # Computed views (React UI) — cached, single-flight
    # ------------------------------------------------------------------

    async def overview(self, *, group_by: str = "site", hours: int = 24,
                       include_paused: bool = True, include_unknown: bool = True) -> Dict[str, Any]:
        """Grouped + flat overview for the React UI. Reuses the pure helpers the
        NiceGUI dashboard uses, with timelines bucketed in SQL."""
        from .helpers import build_grouped_overview, flatten_monitors

        key = f"overview|{group_by}|{hours}|{include_paused}|{include_unknown}"

        async def _compute() -> Dict[str, Any]:
            monitors = await asyncio.to_thread(self.list_monitors)
            ids = [m["monitor_id"] for m in monitors]
            timelines = await asyncio.to_thread(self.get_timelines, ids, hours)
            groups = build_grouped_overview(
                monitors, {}, group_by=group_by, include_paused=include_paused,
                include_unknown=include_unknown, timelines=timelines,
            )
            rows = flatten_monitors(
                monitors, {}, include_paused=include_paused, include_unknown=include_unknown,
                timelines=timelines,
            )
            stats = await asyncio.to_thread(self.stats)
            return {"groups": groups, "rows": rows, "stats": stats, "hours": hours,
                    "generated_at": _utc_now().isoformat()}

        return await self._cached(key, _compute)

    async def dashboard(self) -> Dict[str, Any]:
        async def _compute() -> Dict[str, Any]:
            monitors = await asyncio.to_thread(self.list_monitors)
            stats = await asyncio.to_thread(self.stats)
            return {"monitors": monitors, "stats": stats}

        return await self._cached("dashboard", _compute)

    # Views are keyed by validated parameters (api.py checks group_by against the
    # catalog, clamps hours), so the key space is small; the cap is a backstop.
    VIEW_CACHE_MAX_KEYS = 64

    async def _cached(self, key: str, compute):
        """TTL cache + single flight: concurrent callers of the same view share one
        computation (20 s polling × N browser tabs used to mean N full scans)."""
        ttl = max(0, self._overview_cache_seconds)
        now = time.monotonic()
        hit = self._view_cache.get(key)
        if hit and ttl and now - hit[0] < ttl:
            return hit[1]
        # Housekeeping: drop expired entries; never let the cache grow unbounded.
        if len(self._view_cache) >= self.VIEW_CACHE_MAX_KEYS:
            for k in [k for k, (ts, _) in self._view_cache.items() if not ttl or now - ts >= ttl]:
                self._view_cache.pop(k, None)
            while len(self._view_cache) >= self.VIEW_CACHE_MAX_KEYS:
                self._view_cache.pop(next(iter(self._view_cache)), None)  # oldest insert
        inflight = self._view_inflight.get(key)
        if inflight is not None:
            return await inflight
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        fut.add_done_callback(lambda f: None if f.cancelled() else f.exception())  # mark retrieved
        self._view_inflight[key] = fut
        try:
            result = await compute()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            raise
        else:
            fut.set_result(result)
            self._view_cache[key] = (time.monotonic(), result)
            return result
        finally:
            self._view_inflight.pop(key, None)

    # ------------------------------------------------------------------
    # State transitions & heartbeats
    # ------------------------------------------------------------------

    def _emit_transition(self, record: MonitorRecord, previous_state: str, error_message: Optional[str]):
        if previous_state == record.latest_state:
            return
        self._view_cache.clear()  # a state change must show up on the next poll
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
        self._bump_daily_aggregate(session, record, state, latency_ms)
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

    def _snapshot_records_for_scheduler(self) -> Optional[List[Any]]:
        """Read scheduler-relevant fields off the event loop and return detached
        snapshots. Lets sync_scheduler_jobs_from() create the (loop-only) probe
        tasks without doing any DB I/O on the loop — for a few hundred monitors a
        full MonitorRecord scan on the loop is enough to stall NiceGUI."""
        from types import SimpleNamespace
        session = self._session()
        if not session:
            return None
        try:
            return [
                SimpleNamespace(
                    monitor_id=r.monitor_id,
                    enabled=r.enabled,
                    latest_state=r.latest_state,
                    monitor_type=r.monitor_type,
                    target=r.target,
                    address=r.address,
                    interval_seconds=r.interval_seconds,
                )
                for r in session.query(MonitorRecord).all()
            ]
        finally:
            session.close()

    def sync_scheduler_jobs_from(self, records: Optional[List[Any]]):
        """Create/remove scheduler jobs from pre-loaded snapshots. MUST run on the
        event loop thread (the scheduler uses asyncio.create_task)."""
        if records is None or not self._scheduler_started:
            return
        active_ids = set()
        for record in records:
            active_ids.add(f"monitor:{record.monitor_id}")
            self._sync_job_for_monitor(record)
        for job in self.scheduler.get_jobs():
            if job.id.startswith("monitor:") and job.id not in active_ids:
                self.scheduler.remove_job(job.id)

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

    async def prune_heartbeats(self) -> int:
        """Delete heartbeats older than the retention in batches of PRUNE_BATCH —
        every statement stays far below the core's 60 s statement cap even when
        months of backlog are removed — yielding to the loop between batches."""
        total = 0
        for _ in range(500):
            if self._stopping:
                break
            n = await asyncio.to_thread(self._prune_heartbeats_batch)
            total += n
            if n < self.PRUNE_BATCH:
                break
            await asyncio.sleep(0.2)
        if total:
            self.ctx.log.info(
                f"State Monitoring: pruned {total} heartbeats older than {self._heartbeat_retention_days} days."
            )
        return total

    def _prune_heartbeats_batch(self) -> int:
        session = self._session()
        if not session:
            return 0
        try:
            cutoff = (_utc_now() - timedelta(days=self._heartbeat_retention_days)).replace(tzinfo=None)
            ids = [
                r[0] for r in session.query(MonitorHeartbeat.id)
                .filter(MonitorHeartbeat.created_at < cutoff)
                .order_by(MonitorHeartbeat.id)
                .limit(self.PRUNE_BATCH)
                .all()
            ]
            if not ids:
                return 0
            session.query(MonitorHeartbeat).filter(MonitorHeartbeat.id.in_(ids)).delete(synchronize_session=False)
            session.commit()
            return len(ids)
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
                self._recompute_daily_aggregate(session, record)
            session.commit()
        finally:
            session.close()
        self._refresh_uptime_windows_sync()

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

    @staticmethod
    def _host_key_for(monitor_id: str, host_name: Optional[str]) -> str:
        """Group monitors by their backing host so one dead host/backend opens a
        single circuit. Falls back to parsing the monitor_id (``host:<h>`` /
        ``service:<h>:<svc>``)."""
        if host_name:
            return host_name
        parts = monitor_id.split(":")
        return parts[1] if len(parts) >= 2 and parts[1] else monitor_id

    async def run_scheduled_probe(self, monitor_id: str):
        """Run a single probe without blocking the event loop.

        Phase 1 – DB read  (thread): load record fields.
        Phase 2 – Probe    (async):  network I/O via semaphore.
        Phase 3 – DB write (thread): persist heartbeat + aggregates.

        Resilience: a per-host circuit-breaker skips probing a host that keeps
        failing (so a broken backend can't flood the loop), and flap dampening
        suppresses a single transient failure (no DB write / no state_changed
        emit) until ``_flap_threshold`` consecutive failures are seen.
        """
        if monitor_id in self._inflight_probes:
            return
        self._inflight_probes.add(monitor_id)
        try:
            # Phase 1: read from DB in a worker thread so the event loop stays free.
            record_data = await asyncio.to_thread(self._load_probe_record, monitor_id)
            if not record_data:
                return

            host_key = self._host_key_for(monitor_id, record_data.get("host_name"))
            now = time.monotonic()

            # Circuit breaker: while a host is in backoff, skip the probe entirely.
            deadline = self._host_circuit_until.get(host_key, 0.0)
            if deadline and now < deadline:
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

            if state != MonitorState.DOWN:
                # Recovery: clear this monitor's and its host's failure state.
                self._consec_failures.pop(monitor_id, None)
                self._host_failures.pop(host_key, None)
                self._host_circuit_until.pop(host_key, None)
            else:
                self._consec_failures[monitor_id] = self._consec_failures.get(monitor_id, 0) + 1
                self._host_failures[host_key] = self._host_failures.get(host_key, 0) + 1

                # Open/extend the host circuit (exponential backoff) once the whole
                # host has failed repeatedly without any interspersed success.
                host_fails = self._host_failures[host_key]
                if host_fails >= self._host_circuit_threshold:
                    n = host_fails - self._host_circuit_threshold
                    delay = min(self._host_circuit_max, self._host_circuit_base * (2 ** min(n, 16)))
                    self._host_circuit_until[host_key] = now + delay

                # Flap dampening: suppress a transient DOWN until confirmed. Never
                # suppress a monitor that is already DOWN (keep persistent state).
                if (
                    self._consec_failures[monitor_id] < self._flap_threshold
                    and record_data.get("latest_state") in (MonitorState.UP.value, None)
                ):
                    return

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
            try:
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
            except Exception as exc:
                if self.ctx:
                    self.ctx.log.warning(f"Inventory sync: skipping host {host_name!r}: {exc}")

        for svc in payload.services:
            host_name = svc.get("host_name") or svc.get("hostname")
            service_name = svc.get("service_name") or svc.get("name")
            if not host_name or not service_name:
                continue
            target = svc.get("url") or svc.get("target") or svc.get("address")
            monitor_type = MonitorType.HTTP if target and str(target).startswith(("http://", "https://")) else MonitorType.SERVICE
            try:
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
            except Exception as exc:
                if self.ctx:
                    self.ctx.log.warning(f"Inventory sync: skipping service {host_name!r}:{service_name!r}: {exc}")

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
