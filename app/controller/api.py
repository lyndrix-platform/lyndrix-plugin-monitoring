from fastapi import APIRouter, Depends, HTTPException

from core.api import ApiIdentity, require_permission

from ..model.models import AdminOverride, InventorySyncPayload, MonitorUpsert, PassiveResult
from .service import MonitoringService


def build_plugin_router(service: MonitoringService) -> APIRouter:
    """Single authenticated router.

    Core mounts this via ``ctx.register_routes`` at
    ``/api/plugins/lyndrix.plugin.state_monitoring/`` and applies
    ``require_api_auth`` to every route. Read routes additionally require the
    ``api:read`` permission; mutating routes require ``api:write`` so a
    read-only API key can never tamper with monitoring state.
    """
    router = APIRouter(tags=["State Monitoring"])

    @router.get("/dashboard")
    async def dashboard_data(identity: ApiIdentity = Depends(require_permission("api:read"))):
        return {"monitors": service.list_monitors(), "stats": service.stats()}

    @router.get("/monitors/{monitor_id}")
    async def get_monitor(
        monitor_id: str,
        identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        item = service.get_monitor(monitor_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {monitor_id}")
        return item

    @router.get("/history/{monitor_id}")
    async def get_history(
        monitor_id: str,
        identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        return service.get_history(monitor_id)

    @router.post("/monitors")
    async def upsert_monitor(
        payload: MonitorUpsert,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            return service.upsert_monitor(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/passive")
    async def passive_result(
        payload: PassiveResult,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            return service.ingest_passive_result(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/admin-override")
    async def admin_override(
        payload: AdminOverride,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            return service.apply_admin_override(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/inventory-sync")
    async def inventory_sync(
        payload: InventorySyncPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        return service.ingest_inventory_snapshot(payload)

    @router.post("/clear-states")
    async def clear_states(identity: ApiIdentity = Depends(require_permission("api:write"))):
        """Destructive: wipe all heartbeats/aggregates and reset every monitor.

        Exposed via the API (not just the NiceGUI Danger Zone) so machine
        clients and the React UI can perform and audit the operation.
        """
        deleted = service.clear_states_db()
        return {"deleted": deleted}

    return router
