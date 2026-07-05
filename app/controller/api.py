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
        monitors = await asyncio.to_thread(service.list_monitors)
        stats = await asyncio.to_thread(service.stats)
        return {"monitors": monitors, "stats": stats}

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
        from .helpers import build_grouped_overview, flatten_monitors

        monitors = await asyncio.to_thread(service.list_monitors)
        histories = await asyncio.to_thread(service.get_histories, [m["monitor_id"] for m in monitors], hours)
        groups = build_grouped_overview(
            monitors,
            histories,
            group_by=group_by,
            include_paused=include_paused,
            include_unknown=include_unknown,
        )
        rows = flatten_monitors(
            monitors,
            histories,
            include_paused=include_paused,
            include_unknown=include_unknown,
        )
        stats = await asyncio.to_thread(service.stats)
        return {"groups": groups, "rows": rows, "stats": stats, "hours": hours}

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
        deleted = await asyncio.to_thread(service.clear_states_db)
        return {"deleted": deleted}

    return router
