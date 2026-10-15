import asyncio
import json
import time
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import case, func, text
from sqlalchemy.exc import IntegrityError, OperationalError

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
        self.scheduler = SimpleAsyncScheduler(log=getattr(ctx, "log", None))
        # The core event loop. setup() runs on it; API/bus handlers call the sync
        # write API through asyncio.to_thread, so scheduler task creation and
        # ctx.emit from those workers must hop back onto this loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._remember_loop()
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
        # Cluster placement: True only on the instance holding the
        # "monitoring:prober" lease (always True in single mode). Every instance
        # serves the API; only the prober runs scheduler, sweeps and housekeeping.
        self._probing = False
        self._resync_task: Optional[asyncio.Task] = None
        # Cluster quiesce: while monotonic() < this deadline the prober skips every
        # probe (a destructive maintenance job runs, possibly on another instance).
        self._quiesced_until = 0.0
        # Probe vantage point (setting ``probe_roles``): empty = any instance may
        # hold the prober lease; otherwise only instances with one of these roles.
        self._probe_roles: set = set()
        self._lease_wanted = False
        self._placement_ready = False
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
        self._force_full_sweep = False                    # maintenance: next sweep probes every monitor
        self._flap_threshold = 3                          # consecutive fails before a monitor flips to DOWN
        self._host_circuit_threshold = 8                  # consecutive host fails before the host is paused
        self._host_circuit_base = 30.0                    # circuit backoff base (seconds)
        self._host_circuit_max = 1800.0                   # circuit backoff cap (seconds)

    def _session(self):
        if not db_instance.is_connected or not db_instance.SessionLocal:
            return None
        return db_instance.SessionLocal()

    def _rc_session(self):
        """Session pinned to READ COMMITTED on MariaDB/MySQL (snapshot isolation
        would turn concurrent writers into error 1020); other dialects unchanged."""
        session = self._session()
        if session is None:
            return None
        try:
            bind = session.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") in ("mysql", "mariadb"):
                session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        except Exception as exc:  # an optimisation, never a reason to fail
            self.ctx.log.debug(f"State Monitoring: READ COMMITTED not applied: {exc}")
        return session

    # ------------------------------------------------------------------
    # Event-loop marshalling (sync write API runs in to_thread workers)
    # ------------------------------------------------------------------

    def _remember_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    @staticmethod
    def _on_loop_thread() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def _call_on_loop(self, fn, *args) -> bool:
        """Run ``fn(*args)`` now when we are on the loop thread, otherwise hand it
        to the core loop with ``call_soon_threadsafe``. Returns False when no
        running loop is known (the call is dropped)."""
        if self._on_loop_thread():
            fn(*args)
            return True
        loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)
            return True
        return False

    def _emit(self, topic: str, payload: Dict[str, Any]) -> None:
        """``ctx.emit`` from any thread: the bus schedules async subscribers with
        ``asyncio.create_task`` → only valid on the loop thread."""
        def _do():
            try:
                self.ctx.emit(topic, payload)
            except Exception as exc:
                self.ctx.log.debug(f"State Monitoring: emit {topic} failed: {exc}")

        if not self._call_on_loop(_do):
            _do()  # no loop known (tests / sync callers): emit directly

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
    # Cluster placement — exactly one prober (lease "monitoring:prober")
    # ------------------------------------------------------------------

    @property
    def is_prober(self) -> bool:
        return self._probing

    def start_probing(self):
        """Lease acquired (or single mode / old core): this instance runs the
        scheduler, the housekeeping jobs and the probe sweeps."""
        if self._probing:
            return
        self._probing = True
        self._stopping = False
        self._remember_loop()
        if getattr(self.http_client, "is_closed", False):
            self.http_client = httpx.AsyncClient(follow_redirects=False)
        self.start()
        self.queue_bootstrap()
        self.ctx.log.info("State Monitoring: this instance holds the prober lease — probing here.")

    async def stop_probing(self):
        """Lease lost (peer took over) — stop every background writer. The API
        keeps serving reads from the shared DB on this instance."""
        if not self._probing:
            return
        self._probing = False
        self.ctx.log.warning("State Monitoring: prober lease lost — stopping probes on this instance.")
        await self.stop()
        # stop() flags the background writers to stop; on a lease loss the
        # process lives on and the API (e.g. /maintenance/prune) must still work.
        self._stopping = False

    # ------------------------------------------------------------------
    # Probe vantage point (setting ``probe_roles``)
    # ------------------------------------------------------------------

    def may_probe(self) -> bool:
        """True when this instance is allowed to hold the prober lease: no
        ``probe_roles`` configured, or this instance carries one of them. A core
        without ``ctx.roles`` (single instance, old API) always may."""
        if not self._probe_roles:
            return True
        roles = getattr(self.ctx, "roles", None)
        if roles is None:
            return True
        return bool(set(roles) & self._probe_roles)

    def claim_prober_lease(self) -> None:
        """Contend for ``monitoring:prober`` if this instance may probe. Must run
        on the loop. Idempotent; a lease once wanted is kept until the plugin is
        reloaded (the core has no public "unwant") — narrowing ``probe_roles``
        therefore takes effect on the next plugin reload/restart."""
        self._placement_ready = True
        on_lease = getattr(self.ctx, "on_lease", None)
        if not callable(on_lease):
            self.start_probing()  # core < 1.7: one instance, probe here
            return
        if self._lease_wanted:
            return
        if not self.may_probe():
            roles = ",".join(sorted(self._probe_roles))
            self.ctx.log.info(
                f"State Monitoring: probe_roles={roles} — this instance "
                f"(roles {','.join(getattr(self.ctx, 'roles', None) or []) or '-'}) does not probe."
            )
            has_live_role = getattr(self.ctx, "has_live_role", None)
            if callable(has_live_role):
                with suppress(Exception):
                    if not any(has_live_role(r) for r in self._probe_roles):
                        self.ctx.log.warning(
                            f"State Monitoring: no live instance carries a probe role ({roles}) — nothing probes."
                        )
            return
        self._lease_wanted = True
        on_lease("monitoring:prober", acquired=self.start_probing, lost=self.stop_probing)

    async def init_placement(self) -> None:
        """Load the operator settings (Vault, off the loop) and only then decide
        whether to contend for the prober lease — ``probe_roles`` lives there."""
        await self.reload_settings()
        self.claim_prober_lease()

    async def reload_settings(self) -> None:
        """Re-read the plugin settings from Vault and apply them (boot,
        ``vault:ready_for_data`` and the bridged ``plugin:settings_changed``)."""
        getter = getattr(self.ctx, "get_all_settings", None)
        if not callable(getter):
            return
        try:
            values = await asyncio.to_thread(getter)  # Vault I/O is blocking
        except Exception as exc:
            self.ctx.log.warning(f"State Monitoring: could not load settings — {exc}")
            return
        self.apply_settings(values or {})
        # probe_roles widened at runtime → contend now (narrowing needs a reload).
        if getattr(self.ctx, "on_lease", None) and not self._lease_wanted and self.may_probe():
            if self._placement_ready:
                self.claim_prober_lease()

    def on_monitors_changed(self, payload: Optional[Dict[str, Any]]) -> None:
        """``monitoring:monitors_changed`` subscriber. Our own emits already took
        effect locally; a peer's (bridged, ``_origin`` set) means the prober must
        reload its jobs — or, for ``probe_all``, run a full sweep."""
        payload = payload or {}
        if not payload.get("_origin"):
            return
        if payload.get("reason") == "probe_all":
            self.request_full_probe()
        else:
            self.request_resync()

    def request_resync(self):
        """A peer changed the monitor set (cluster event): reload scheduler jobs
        and sweep the new/changed monitors — only meaningful on the prober."""
        if not self._probing:
            return
        if self._resync_task and not self._resync_task.done():
            return
        self._resync_task = asyncio.create_task(self._resync_async(), name="monitoring:resync")

    async def _resync_async(self):
        await asyncio.sleep(0.2)
        try:
            records = await asyncio.to_thread(self._snapshot_records_for_scheduler)
            self.sync_scheduler_jobs_from(records)
            self._view_cache.clear()
            self.enqueue_background_probe_refresh()
        except Exception as exc:
            self.ctx.log.error(f"State Monitoring: resync after cluster event failed: {exc}")

    def _notify_monitors_changed(self, reason: str, count: Optional[int] = None) -> None:
        """Tell the other core instances the monitor set changed (``cluster=True``
        reaches every instance; peers receive it with ``_origin`` set)."""
        payload = {"reason": reason, "count": count, "instance_id": getattr(self.ctx, "instance_id", None)}
        self._emit_cluster("monitoring:monitors_changed", payload)

    def _emit_cluster(self, topic: str, payload: Dict[str, Any]) -> None:
        """Bridged emit from any thread (marshalled onto the loop)."""
        def _do():
            try:
                self.ctx.emit(topic, payload, cluster=True)
            except TypeError:
                # Core < 1.7: no cluster flag — local emit only (single instance anyway).
                with suppress(Exception):
                    self.ctx.emit(topic, payload)
            except Exception as exc:
                self.ctx.log.debug(f"State Monitoring: {topic} emit failed: {exc}")

        if not self._call_on_loop(_do):
            _do()

    # ------------------------------------------------------------------
    # Cluster quiesce — destructive maintenance stops the prober's writers
    # ------------------------------------------------------------------

    QUIESCE_TOPIC = "monitoring:quiesce"
    QUIESCE_TTL_SECONDS = 120     # safety net if the "off" event is lost
    QUIESCE_GRACE_SECONDS = 3.0   # standby: bus relay (~0.5 s) + prober in-flight drain

    @property
    def is_quiesced(self) -> bool:
        return time.monotonic() < self._quiesced_until

    def apply_quiesce(self, on: bool, ttl: Optional[float] = None) -> None:
        """Local switch: while on, ``run_scheduled_probe`` skips every probe.
        Turning it off resyncs the jobs (the maintenance may have deleted rows)."""
        if on:
            ttl = float(ttl or self.QUIESCE_TTL_SECONDS)
            self._quiesced_until = time.monotonic() + max(1.0, min(ttl, 600.0))
        else:
            self._quiesced_until = 0.0
            self.request_resync()

    def on_quiesce(self, payload: Optional[Dict[str, Any]]) -> None:
        """``monitoring:quiesce`` subscriber — a peer runs destructive maintenance."""
        payload = payload or {}
        if not payload.get("_origin"):
            return  # our own request was applied locally already
        self.apply_quiesce(bool(payload.get("on")), payload.get("ttl"))

    @asynccontextmanager
    async def quiesced(self, monitor_ids: List[str]):
        """Stop every probe writer in the cluster for the duration of a
        destructive maintenance job. Locally: drop the affected jobs, skip probes
        and wait for in-flight ones. On a standby the prober is another instance:
        ask it via the bridged ``monitoring:quiesce`` event and give it a grace
        period to drain. The batched deletes additionally tolerate a straggling
        write (READ COMMITTED + 1020/1205/1213 retry)."""
        self.apply_quiesce(True)
        self._emit_cluster(self.QUIESCE_TOPIC, {"on": True, "ttl": self.QUIESCE_TTL_SECONDS})
        try:
            await self.quiesce_monitors(monitor_ids)
            if not self._probing:
                await asyncio.sleep(self.QUIESCE_GRACE_SECONDS)
            yield
        finally:
            self._quiesced_until = 0.0
            self._emit_cluster(self.QUIESCE_TOPIC, {"on": False})

    async def resync_after_delete(self) -> None:
        """After monitors/states were removed: forget probe bookkeeping, reload
        the local jobs (prober) and tell the other instances."""
        self.reset_probe_state()
        records = await asyncio.to_thread(self._snapshot_records_for_scheduler)
        self.sync_scheduler_jobs_from(records)
        # The prober may be another instance — it reloads its jobs on this event.
        self._notify_monitors_changed("maintenance", count=len(records or []))

    async def apply_monitor_set_change(self, reason: str) -> None:
        """The monitor set changed in bulk (inventory import): reload the local
        jobs, sweep the new monitors (prober only) and announce it cluster-wide."""
        records = await asyncio.to_thread(self._snapshot_records_for_scheduler)
        self.sync_scheduler_jobs_from(records)
        self._view_cache.clear()
        await asyncio.sleep(0)
        self.enqueue_background_probe_refresh()
        self._notify_monitors_changed(reason, count=len(records or []))

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
        session = self._rc_session()
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
            self._ensure_daily_aggregate_unique(db_instance.engine)

    def _ensure_tables_locked(self):
        """``ensure_tables`` under the core's cross-instance bootstrap lock (core
        ≥ 1.7) so N instances booting together do not race the DDL."""
        lock = getattr(db_instance, "advisory_lock", None)
        if callable(lock):
            with lock("lyndrix_bootstrap", timeout=60):
                self.ensure_tables()
        else:
            self.ensure_tables()

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

    DAILY_AGG_UNIQUE = "uq_monitoring_daily_aggregates_monitor_day"

    def _ensure_daily_aggregate_unique(self, engine) -> None:
        """Idempotent migration: one aggregate row per (monitor_id, day).

        Older installs could get duplicate rows (two writers both missed the
        select in select-then-insert). Merge them first — counters summed, the
        latency mean weighted by its sample count, kept on the lowest id — then
        add the unique index. "Duplicate key name" (1061) means a peer or a
        fresh ``create_all`` already did it; any other failure is logged and the
        plugin keeps running (writes then fall back to the old behaviour)."""
        from sqlalchemy import inspect as sa_inspect

        try:
            existing = {ix.get("name") for ix in sa_inspect(engine).get_indexes("monitoring_daily_aggregates")}
        except Exception:
            existing = set()
        if self.DAILY_AGG_UNIQUE in existing:
            return
        try:
            merged = self._dedupe_daily_aggregates(engine)
            if merged:
                self.ctx.log.warning(
                    f"State Monitoring: merged {merged} duplicate daily aggregate rows before adding the unique key."
                )
        except Exception as exc:
            self.ctx.log.warning(f"State Monitoring: daily aggregate dedupe failed: {exc}")
            return
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {self.DAILY_AGG_UNIQUE} "
                    "ON monitoring_daily_aggregates (monitor_id, day)"
                ))
        except Exception as exc:
            code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
            if code != 1061:  # duplicate key name → already there
                self.ctx.log.warning(f"State Monitoring: unique key on daily aggregates not added: {exc}")

    @staticmethod
    def _dedupe_daily_aggregates(engine) -> int:
        """Merge duplicate (monitor_id, day) aggregate rows; returns rows removed."""
        from sqlalchemy.orm import Session

        removed = 0
        with Session(bind=engine) as session:
            groups = (
                session.query(MonitorDailyAggregate.monitor_id, MonitorDailyAggregate.day)
                .group_by(MonitorDailyAggregate.monitor_id, MonitorDailyAggregate.day)
                .having(func.count(MonitorDailyAggregate.id) > 1)
                .all()
            )
            for monitor_id, day in groups:
                rows = (
                    session.query(MonitorDailyAggregate)
                    .filter(MonitorDailyAggregate.monitor_id == monitor_id, MonitorDailyAggregate.day == day)
                    .order_by(MonitorDailyAggregate.id)
                    .all()
                )
                keep, extra = rows[0], rows[1:]
                lat_n = sum(int(r.latency_samples or 0) for r in rows)
                lat_sum = sum(float(r.avg_latency_ms or 0.0) * int(r.latency_samples or 0) for r in rows)
                keep.total_samples = sum(int(r.total_samples or 0) for r in rows)
                keep.up_samples = sum(int(r.up_samples or 0) for r in rows)
                keep.down_samples = sum(int(r.down_samples or 0) for r in rows)
                keep.paused_samples = sum(int(r.paused_samples or 0) for r in rows)
                keep.latency_samples = lat_n
                if lat_n:
                    keep.avg_latency_ms = round(lat_sum / lat_n, 2)
                else:
                    keep.avg_latency_ms = next((r.avg_latency_ms for r in rows if r.avg_latency_ms is not None), None)
                keep.updated_at = _utc_now()
                for r in extra:
                    session.delete(r)
                removed += len(extra)
                session.commit()
        return removed

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
            await asyncio.to_thread(self._ensure_tables_locked)
        if not self._probing:
            # Standby instance: tables are there, routes serve reads; the prober
            # (lease holder) owns scheduler, sweeps and housekeeping.
            return
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
        if not self._probing:
            return  # only the prober sweeps
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

            force = self._force_full_sweep
            self._force_full_sweep = False
            candidates = [
                r for r in records
                if (r.target or r.address)
                and (
                    force
                    or r.latest_state in {MonitorState.UNKNOWN.value, MonitorState.DOWN.value}
                    or _is_stale(r)
                )
            ]
            candidates.sort(key=lambda r: (self._probe_priority(r), r.updated_at or _utc_now()))
            if self._initial_probe_batch_limit > 0 and not force:
                candidates = candidates[: self._initial_probe_batch_limit]
            # A forced sweep re-probes known states → banner only, never a 503 window.
            self._sweep_blocking = (not force) and any(
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
        raw_roles = values.get("probe_roles") or ""
        if isinstance(raw_roles, (list, tuple, set)):
            raw_roles = ",".join(str(r) for r in raw_roles)
        self._probe_roles = {r.strip() for r in str(raw_roles).split(",") if r.strip()}

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

    async def ingest_inventory(self, payload: InventorySyncPayload) -> Dict[str, Any]:
        """Synchronous inventory import (``POST /inventory-sync``): ingest, then
        give the new monitors probe jobs and announce the change — the bulk
        upserts run with ``sync_scheduler=False`` and never notify on their own."""
        stats = await asyncio.to_thread(self.ingest_inventory_snapshot, payload)
        try:
            await self.apply_monitor_set_change("inventory")
        except Exception as exc:
            self.ctx.log.error(f"State Monitoring: resync after inventory import failed: {exc}")
        return stats

    async def _process_inventory_sync_queue(self):
        await asyncio.sleep(0.5)
        while self._pending_inventory_payload is not None:
            payload = self._pending_inventory_payload
            self._pending_inventory_payload = None
            try:
                async with self.maintenance_window("Importing inventory"):
                    await asyncio.to_thread(self.ingest_inventory_snapshot, payload)
                await asyncio.sleep(0)
                # The prober may be another instance (inventory arrives where the
                # orchestrator runs) — this also tells it to reload its jobs.
                await self.apply_monitor_set_change("inventory")
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
        bind = session.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", "") in ("mysql", "mariadb"):
            self._upsert_daily_aggregate_mysql(session, monitor.monitor_id, day, state, latency_ms)
            return
        # Portable path (SQLite in tests): select-then-insert inside a savepoint;
        # a concurrent insert of the same (monitor_id, day) hits the unique key
        # → re-read the winner's row and increment that one.
        aggregate = self._get_daily_aggregate(session, monitor.monitor_id, day)
        if aggregate is None:
            try:
                with session.begin_nested():
                    aggregate = MonitorDailyAggregate(
                        monitor_id=monitor.monitor_id, day=day, total_samples=0, up_samples=0,
                        down_samples=0, paused_samples=0, latency_samples=0,
                    )
                    session.add(aggregate)
            except IntegrityError:
                aggregate = self._get_daily_aggregate(session, monitor.monitor_id, day)
                if aggregate is None:
                    raise
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

    @staticmethod
    def _get_daily_aggregate(session, monitor_id: str, day):
        return session.query(MonitorDailyAggregate).filter(
            MonitorDailyAggregate.monitor_id == monitor_id,
            MonitorDailyAggregate.day == day,
        ).first()

    @staticmethod
    def _upsert_daily_aggregate_mysql(session, monitor_id: str, day, state: "MonitorState",
                                      latency_ms: Optional[float]) -> None:
        """One atomic ``INSERT … ON DUPLICATE KEY UPDATE`` on the unique
        (monitor_id, day) key — no read, no lost update between instances.
        MariaDB evaluates the SET list left to right with already-updated values,
        so ``avg_latency_ms`` must be computed before ``latency_samples`` moves."""
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        up = 1 if state == MonitorState.UP else 0
        down = 1 if state == MonitorState.DOWN else 0
        paused = 1 if state == MonitorState.PAUSED else 0
        has_lat = 1 if latency_ms is not None else 0
        now = _utc_now()
        stmt = mysql_insert(MonitorDailyAggregate.__table__).values(
            monitor_id=monitor_id, day=day, total_samples=1, up_samples=up, down_samples=down,
            paused_samples=paused, avg_latency_ms=(round(float(latency_ms), 2) if has_lat else None),
            latency_samples=has_lat, updated_at=now,
        )
        t = MonitorDailyAggregate.__table__.c
        updates = [
            ("total_samples", t.total_samples + 1),
            ("up_samples", t.up_samples + up),
            ("down_samples", t.down_samples + down),
            ("paused_samples", t.paused_samples + paused),
        ]
        if has_lat:
            updates.append((
                "avg_latency_ms",
                func.round(
                    (func.coalesce(t.avg_latency_ms, 0.0) * t.latency_samples + float(latency_ms))
                    / (t.latency_samples + 1),
                    2,
                ),
            ))
            updates.append(("latency_samples", t.latency_samples + 1))
        updates.append(("updated_at", now))
        session.execute(stmt.on_duplicate_key_update(updates))

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
        aggregate = self._get_daily_aggregate(session, monitor.monitor_id, day)
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
        session = self._rc_session()
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
            session.commit()  # end the read transaction before the chunked writes
            # Short committed chunks (READ COMMITTED) next to the probe writers,
            # each retried on 1020/1205/1213 — one big transaction over every
            # monitor row used to collide with concurrent probe writes.
            for i in range(0, len(ids), self.ROLLUP_CHUNK):
                chunk = ids[i:i + self.ROLLUP_CHUNK]

                def _write(chunk=chunk) -> None:
                    for record in session.query(MonitorRecord).filter(MonitorRecord.monitor_id.in_(chunk)).all():
                        record.uptime_24h = day.get(record.monitor_id, 100.0)
                        record.uptime_7d = week.get(record.monitor_id, 100.0)
                        record.uptime_30d = month.get(record.monitor_id, 100.0)
                    session.commit()

                self._retrying(session, _write)
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
        # Runs in to_thread workers (probe writes, passive results) → marshal.
        self._emit(
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

    def _sync_job_for_monitor(self, record: Any):
        """Create/replace/remove the probe job of one monitor. The scheduler
        creates asyncio tasks, so off the loop (the write API runs in to_thread
        workers) the change is handed to the loop on a detached snapshot."""
        if not self._scheduler_started:
            return
        if not self._on_loop_thread():
            from types import SimpleNamespace
            snap = SimpleNamespace(
                monitor_id=record.monitor_id, enabled=record.enabled, latest_state=record.latest_state,
                monitor_type=record.monitor_type, target=record.target, address=record.address,
                interval_seconds=record.interval_seconds,
            )
            if not self._call_on_loop(self._sync_job_on_loop, snap):
                self.request_resync_threadsafe()
            return
        self._sync_job_on_loop(record)

    def request_resync_threadsafe(self) -> None:
        """``request_resync`` from any thread; a no-op without a known loop."""
        self._call_on_loop(self.request_resync)

    def _sync_job_on_loop(self, record: Any):
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
            # No DDL here: bootstrap creates the tables under the cluster lock.
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
                self._notify_monitors_changed("upsert", count=1)
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
            self._notify_monitors_changed("override", count=1)
            return _serialize_monitor(record)
        finally:
            session.close()

    def ingest_passive_result(self, payload: PassiveResult) -> Dict[str, Any]:
        session = self._rc_session()
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
        Returns the number of heartbeat rows deleted.

        Batched READ COMMITTED deletes with 1020/1205/1213 retry; callers stop the
        probe writers first (``clear_states``)."""
        session = self._maintenance_session()
        if not session:
            return 0
        try:
            deleted = self._delete_heartbeats_for(session, None)

            def _rest() -> None:
                session.query(MonitorDailyAggregate).delete(synchronize_session=False)
                session.query(MonitorRecord).update(
                    {
                        MonitorRecord.latest_state: MonitorState.UNKNOWN.value,
                        MonitorRecord.uptime_24h: 100.0,
                        MonitorRecord.uptime_7d: 100.0,
                        MonitorRecord.uptime_30d: 100.0,
                        MonitorRecord.last_checked_at: None,
                        MonitorRecord.latest_error: None,
                    },
                    synchronize_session=False,
                )
                session.commit()

            self._retrying(session, _rest)
            return deleted
        finally:
            session.close()

    async def clear_states(self) -> int:
        """Destructive reset (API + NiceGUI danger zone): stop every probe writer
        in the cluster, wipe heartbeats/aggregates, reset all monitors to
        UNKNOWN, then reload the jobs — the next sweep re-probes everything."""
        ids = await asyncio.to_thread(self.monitor_ids_for_scope, "all")
        async with self.quiesced(ids):
            deleted = await asyncio.to_thread(self.clear_states_db)
        await self.resync_after_delete()
        self.enqueue_background_probe_refresh()
        return deleted

    # ------------------------------------------------------------------
    # Maintenance actions (core settings dialog → POST /maintenance/*)
    # ------------------------------------------------------------------

    MANAGED_OWNER_SOURCES = ("orchestrator_service", "iac_orchestrator")
    DELETE_CHUNK = 500
    ROLLUP_CHUNK = 200
    # MariaDB error codes that mean "another transaction touched these rows":
    # 1020 record changed since last read (innodb_snapshot_isolation, MariaDB >= 11.6),
    # 1213 deadlock, 1205 lock wait timeout. Writers (probes, passive results) run
    # concurrently with maintenance, so every destructive batch retries on them.
    _RETRY_DB_ERRORS = (1020, 1205, 1213)

    def _maintenance_session(self):
        """Session for destructive maintenance: READ COMMITTED where the dialect
        supports it, so a long sequence of batches never trips snapshot-isolation
        conflicts against the probe writers (SQLite: no-op)."""
        return self._rc_session()

    def _retrying(self, session, fn, attempts: int = 4, extra_codes: tuple = ()):
        """Run ``fn()`` (one short write transaction, committed inside) and retry it
        on the concurrent-write errors above with a short back-off."""
        retry_codes = self._RETRY_DB_ERRORS + tuple(extra_codes)
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:  # sqlalchemy.exc.OperationalError wraps the driver error
                code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
                session.rollback()
                if code not in retry_codes or attempt == attempts:
                    raise
                time.sleep(0.2 * attempt)

    def monitor_ids_for_scope(self, scope: str = "managed") -> List[str]:
        """Ids ``delete_monitors(scope)`` would remove (``managed`` | ``all``)."""
        if scope not in ("managed", "all"):
            raise ValueError(f"unknown delete scope {scope!r}")
        session = self._session()
        if not session:
            return []
        try:
            q = session.query(MonitorRecord.monitor_id)
            if scope == "managed":
                q = q.filter(MonitorRecord.owner_source.in_(self.MANAGED_OWNER_SOURCES))
            return [r[0] for r in q.all()]
        finally:
            session.close()

    async def quiesce_monitors(self, monitor_ids: List[str], timeout: float = 5.0) -> None:
        """Stop the probe writers for ``monitor_ids`` before a destructive change:
        drop their scheduler jobs (loop thread) and wait until none of them is
        in flight any more. The scheduler is resynced from the DB afterwards."""
        if self._scheduler_started:
            for mid in monitor_ids:
                job_id = f"monitor:{mid}"
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
        wanted = set(monitor_ids)
        deadline = time.monotonic() + timeout
        while (self._inflight_probes & wanted) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    def _delete_heartbeats_for(self, session, monitor_ids: Optional[List[str]]) -> int:
        """Delete heartbeats of ``monitor_ids`` (``None`` = all) in PRUNE_BATCH-sized
        statements so none of them can run into the core's statement cap."""
        total = 0

        def _batch() -> int:
            q = session.query(MonitorHeartbeat.id)
            if monitor_ids is not None:
                q = q.filter(MonitorHeartbeat.monitor_id.in_(monitor_ids))
            ids = [r[0] for r in q.order_by(MonitorHeartbeat.id).limit(self.PRUNE_BATCH).all()]
            if ids:
                session.query(MonitorHeartbeat).filter(MonitorHeartbeat.id.in_(ids)).delete(synchronize_session=False)
            session.commit()
            return len(ids)

        while True:
            n = self._retrying(session, _batch)
            total += n
            if n < self.PRUNE_BATCH:
                return total

    def delete_monitors(self, scope: str = "managed") -> Dict[str, int]:
        """Delete monitors together with their heartbeats and daily aggregates.

        ``managed`` → only monitors the IaC orchestrator created from the inventory
        (``owner_source`` in MANAGED_OWNER_SOURCES); it recreates them on its next
        inventory publish, manual (``ui_admin``) monitors survive. ``all`` → every
        monitor. The caller resyncs the scheduler on the loop afterwards
        (``sync_scheduler_jobs_from``) and calls ``reset_probe_state``.
        """
        ids = self.monitor_ids_for_scope(scope)
        result = {"monitors": 0, "heartbeats": 0, "aggregates": 0}
        session = self._maintenance_session()
        if not session:
            return result
        try:
            for i in range(0, len(ids), self.DELETE_CHUNK):
                chunk = ids[i:i + self.DELETE_CHUNK]
                result["heartbeats"] += self._delete_heartbeats_for(session, chunk)

                def _rest() -> int:
                    n = session.query(MonitorDailyAggregate).filter(
                        MonitorDailyAggregate.monitor_id.in_(chunk)
                    ).delete(synchronize_session=False)
                    session.query(MonitorRecord).filter(MonitorRecord.monitor_id.in_(chunk)).delete(
                        synchronize_session=False
                    )
                    session.commit()
                    return n

                result["aggregates"] += self._retrying(session, _rest)
            result["monitors"] = len(ids)
            return result
        finally:
            session.close()

    def reset_statistics(self) -> Dict[str, int]:
        """Delete every heartbeat and daily aggregate and reset the uptime columns —
        but keep each monitor's current state, latency and last check (unlike
        ``clear_states_db``, which also resets everything to UNKNOWN)."""
        result = {"heartbeats": 0, "aggregates": 0}
        session = self._maintenance_session()
        if not session:
            return result
        try:
            result["heartbeats"] = self._delete_heartbeats_for(session, None)

            def _rest() -> int:
                n = session.query(MonitorDailyAggregate).delete(synchronize_session=False)
                session.query(MonitorRecord).update(
                    {MonitorRecord.uptime_24h: 100.0, MonitorRecord.uptime_7d: 100.0, MonitorRecord.uptime_30d: 100.0},
                    synchronize_session=False,
                )
                session.commit()
                return n

            result["aggregates"] = self._retrying(session, _rest)
            return result
        finally:
            session.close()

    def rebuild_statistics(self) -> Dict[str, int]:
        """Recompute the daily aggregates from the raw heartbeats and refresh the
        uptime windows — the repair job when counters look wrong."""
        self._refresh_rollups_sync()
        self._refresh_uptime_windows_sync()
        return {"monitors": int(self.status_counts().get("monitor_count", 0))}

    def reset_probe_state(self) -> None:
        """Forget in-memory probe bookkeeping (flap counters, host circuit breakers,
        view cache) after monitors were removed or states were reset."""
        self._consec_failures.clear()
        self._host_failures.clear()
        self._host_circuit_until.clear()
        self._view_cache.clear()

    def request_full_probe(self) -> None:
        """Queue a sweep that probes EVERY enabled monitor (not just stale/unknown
        ones). Reported as a non-blocking ``starting`` status with progress."""
        if not self._probing:
            return  # a standby never sweeps — request_full_probe_cluster() asks the prober
        self._force_full_sweep = True
        # Off the loop (tests, threads) the flag is honoured by the next sweep.
        self._call_on_loop(self.enqueue_background_probe_refresh)

    def request_full_probe_cluster(self) -> None:
        """``/maintenance/probe-all`` on any instance: sweep locally when this is
        the prober, and ask the prober (peer) via the bridged event otherwise."""
        self.request_full_probe()
        self._notify_monitors_changed("probe_all")

    async def refresh_rollups(self):
        await asyncio.to_thread(self._refresh_rollups_sync)

    def _refresh_rollups_sync(self):
        """Recompute today's aggregates, ROLLUP_CHUNK monitors per committed READ
        COMMITTED transaction (retried on 1020/1205/1213, and on 1062 when a
        concurrent writer inserted today's row first), then the uptime windows."""
        session = self._rc_session()
        if not session:
            return
        try:
            ids = [r[0] for r in session.query(MonitorRecord.monitor_id).all()]
            session.commit()
            for i in range(0, len(ids), self.ROLLUP_CHUNK):
                chunk = ids[i:i + self.ROLLUP_CHUNK]

                def _write(chunk=chunk) -> None:
                    for record in session.query(MonitorRecord).filter(MonitorRecord.monitor_id.in_(chunk)).all():
                        self._recompute_daily_aggregate(session, record)
                    session.commit()

                self._retrying(session, _write, extra_codes=(1062,))
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
        if monitor_id in self._inflight_probes or self.is_quiesced:
            return  # quiesced: a destructive maintenance job runs somewhere in the cluster
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
            try:
                await asyncio.to_thread(self._save_probe_result, monitor_id, state, latency_ms, error_message)
            except OperationalError as exc:
                # 1020 (snapshot isolation: row changed by a concurrent writer —
                # maintenance on another instance, a lease handover), 1205 lock
                # wait, 1213 deadlock: drop this sample, the next interval
                # re-probes. Never let one lost write kill the scheduler job.
                code = getattr(getattr(exc, "orig", None), "args", [None])[0]
                if code not in (1020, 1205, 1213):
                    raise
                self.ctx.log.debug(f"State Monitoring: probe write for '{monitor_id}' skipped (MariaDB {code}).")
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
