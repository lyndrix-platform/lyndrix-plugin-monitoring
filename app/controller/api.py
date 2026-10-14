import asyncio

from fastapi import APIRouter, Depends, HTTPException

from core.api import ApiIdentity, require_permission

from ..model.models import AdminOverride, InventorySyncPayload, MonitorUpsert, PassiveResult
from .service import MonitoringService

# Fully qualified permission ids (Identity 2.0). Route gates check the
# plugin-scoped ids so the manifest's viewer/operator/admin roles unlock
# them; holders of the GLOBAL api:read/api:write keep access via the core's
# one-directional fallback (access_service: global satisfies scoped).
_PERM_READ = "plugin:lyndrix.plugin.state_monitoring:api:read"
_PERM_WRITE = "plugin:lyndrix.plugin.state_monitoring:api:write"


def build_plugin_router(service: MonitoringService) -> APIRouter:
    """Single authenticated router.

    Core mounts this via ``ctx.register_routes`` at
    ``/api/plugins/lyndrix.plugin.state_monitoring/`` and applies
    ``require_api_auth`` to every route. Read routes additionally require the
    plugin-scoped ``api:read`` permission; mutating routes require the
    plugin-scoped ``api:write`` so a read-only API key can never tamper with
    monitoring state.

    Every ``MonitoringService`` call below is synchronous SQLAlchemy — routed
    through ``asyncio.to_thread`` (same pattern as entrypoint.py's event
    subscribers) so a slow/contended DB never blocks the shared event loop.
    """
    router = APIRouter(tags=["State Monitoring"])

    @router.get("/dashboard")
    async def dashboard_data(identity: ApiIdentity = Depends(require_permission(_PERM_READ))):
        return await service.dashboard()

    @router.get("/overview")
    async def overview(
        group_by: str = "site",
        hours: int = 24,
        include_paused: bool = True,
        include_unknown: bool = True,
        identity: ApiIdentity = Depends(require_permission(_PERM_READ)),
    ):
        """Computed overview for the React UI.

        Reuses the pure NiceGUI helpers (``build_grouped_overview`` /
        ``flatten_monitors``) so the React client renders the SAME grouped
        and flattened data the in-process NiceGUI dashboard does — no
        business logic lives in the frontend.
        """
        from .helpers import _GROUP_ICONS

        # Cache keys are built from these parameters: reject anything outside
        # the known axes so an authenticated caller cannot mint unbounded keys.
        if group_by not in _GROUP_ICONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown group_by '{group_by}' (expected one of {', '.join(sorted(_GROUP_ICONS))})",
            )
        hours = max(1, min(int(hours), 168))
        return await service.overview(
            group_by=group_by, hours=hours,
            include_paused=include_paused, include_unknown=include_unknown,
        )

    @router.get("/monitors/{monitor_id}")
    async def get_monitor(
        monitor_id: str,
        identity: ApiIdentity = Depends(require_permission(_PERM_READ)),
    ):
        item = await asyncio.to_thread(service.get_monitor, monitor_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {monitor_id}")
        return item

    @router.get("/history/{monitor_id}")
    async def get_history(
        monitor_id: str,
        identity: ApiIdentity = Depends(require_permission(_PERM_READ)),
    ):
        return await asyncio.to_thread(service.get_history, monitor_id)

    @router.post("/monitors")
    async def upsert_monitor(
        payload: MonitorUpsert,
        identity: ApiIdentity = Depends(require_permission(_PERM_WRITE)),
    ):
        try:
            return await asyncio.to_thread(service.upsert_monitor, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/passive")
    async def passive_result(
        payload: PassiveResult,
        identity: ApiIdentity = Depends(require_permission(_PERM_WRITE)),
    ):
        try:
            return await asyncio.to_thread(service.ingest_passive_result, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/admin-override")
    async def admin_override(
        payload: AdminOverride,
        identity: ApiIdentity = Depends(require_permission(_PERM_WRITE)),
    ):
        try:
            return await asyncio.to_thread(service.apply_admin_override, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/inventory-sync")
    async def inventory_sync(
        payload: InventorySyncPayload,
        identity: ApiIdentity = Depends(require_permission(_PERM_WRITE)),
    ):
        return await asyncio.to_thread(service.ingest_inventory_snapshot, payload)

    @router.post("/clear-states")
    async def clear_states(identity: ApiIdentity = Depends(require_permission(_PERM_WRITE))):
        """Destructive: wipe all heartbeats/aggregates and reset every monitor.

        Exposed via the API (not just the NiceGUI Danger Zone) so machine
        clients and the React UI can perform and audit the operation.
        """
        async with service.maintenance_window("Clearing monitor states"):
            deleted = await asyncio.to_thread(service.clear_states_db)
        return {"deleted": deleted}

    # ------------------------------------------------------------------
    # Maintenance actions — the buttons in the core's plugin settings dialog
    # (manifest settings_schema, kind="action"). All of them are write-gated and
    # return a human-readable ``message`` the dialog shows after the run.
    # ------------------------------------------------------------------

    async def _resync_after_delete():
        service.reset_probe_state()
        records = await asyncio.to_thread(service._snapshot_records_for_scheduler)
        service.sync_scheduler_jobs_from(records)

    @router.post("/maintenance/prune")
    async def maintenance_prune(identity: ApiIdentity = Depends(require_permission(_PERM_WRITE))):
        """Run the heartbeat retention job right now (same as the hourly job)."""
        deleted = await service.prune_heartbeats()
        return {
            "status": "ok",
            "deleted": deleted,
            "message": f"Pruned {deleted} heartbeats older than {service._heartbeat_retention_days} days.",
        }

    @router.post("/maintenance/rebuild-stats")
    async def maintenance_rebuild_stats(identity: ApiIdentity = Depends(require_permission(_PERM_WRITE))):
        """Recompute daily aggregates from raw heartbeats and refresh uptime windows."""
        async with service.maintenance_window("Rebuilding statistics"):
            result = await asyncio.to_thread(service.rebuild_statistics)
        return {
            "status": "ok",
            **result,
            "message": f"Rebuilt daily aggregates and uptime windows for {result['monitors']} monitors.",
        }

    @router.post("/maintenance/reset-stats")
    async def maintenance_reset_stats(identity: ApiIdentity = Depends(require_permission(_PERM_WRITE))):
        """Destructive: delete all heartbeats + aggregates, keep monitor states."""
        async with service.maintenance_window("Deleting statistics", blocking=True):
            # Stop every probe writer first, then delete, then resync the jobs.
            ids = await asyncio.to_thread(service.monitor_ids_for_scope, "all")
            await service.quiesce_monitors(ids)
            result = await asyncio.to_thread(service.reset_statistics)
            await _resync_after_delete()
        return {
            "status": "ok",
            **result,
            "message": (
                f"Deleted {result['heartbeats']} heartbeats and {result['aggregates']} daily aggregates; "
                "monitor states were kept."
            ),
        }

    @router.post("/maintenance/probe-all")
    async def maintenance_probe_all(identity: ApiIdentity = Depends(require_permission(_PERM_WRITE))):
        """Queue a sweep over every enabled monitor (progress in the plugin banner)."""
        service.request_full_probe()
        counts = await asyncio.to_thread(service.status_counts)
        return {
            "status": "ok",
            "monitors": counts.get("monitor_count", 0),
            "message": f"Full probe sweep queued for {counts.get('monitor_count', 0)} monitors; progress shows in the plugin banner.",
        }

    @router.post("/maintenance/delete-monitors/{scope}")
    async def maintenance_delete_monitors(
        scope: str,
        identity: ApiIdentity = Depends(require_permission(_PERM_WRITE)),
    ):
        """Destructive: delete monitors with their history.

        ``managed`` removes the IaC-inventory monitors only (the orchestrator
        recreates them on its next publish); ``all`` removes every monitor.
        """
        if scope not in ("managed", "all"):
            raise HTTPException(status_code=400, detail="scope must be 'managed' or 'all'")
        async with service.maintenance_window("Deleting monitors", blocking=True):
            # Stop the probe writers of the affected monitors first (their jobs +
            # in-flight probes), otherwise concurrent heartbeat/aggregate writes
            # trip MariaDB's snapshot-isolation check (error 1020) mid-delete.
            ids = await asyncio.to_thread(service.monitor_ids_for_scope, scope)
            await service.quiesce_monitors(ids)
            result = await asyncio.to_thread(service.delete_monitors, scope)
            await _resync_after_delete()
        hint = (
            " The IaC orchestrator recreates its monitors on the next inventory publish."
            if scope == "managed" else ""
        )
        return {
            "status": "ok",
            "scope": scope,
            **result,
            "message": f"Deleted {result['monitors']} monitors with {result['heartbeats']} heartbeats.{hint}",
        }

    return router
