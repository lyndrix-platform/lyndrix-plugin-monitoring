from fastapi import APIRouter, HTTPException

from ..model.models import AdminOverride, InventorySyncPayload, MonitorUpsert, PassiveResult
from .service import MonitoringService


def build_router(service: MonitoringService) -> APIRouter:
    router = APIRouter(prefix="/api/monitoring", tags=["State Monitoring"])

    @router.get("/dashboard")
    async def dashboard_data():
        return {"monitors": service.list_monitors(), "stats": service.stats()}

    @router.get("/monitors/{monitor_id}")
    async def monitor_data(monitor_id: str):
        item = service.get_monitor(monitor_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {monitor_id}")
        return item

    @router.get("/history/{monitor_id}")
    async def history_data(monitor_id: str):
        return service.get_history(monitor_id)

    @router.post("/monitors")
    async def upsert_monitor(payload: MonitorUpsert):
        try:
            return service.upsert_monitor(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/passive")
    async def passive_result(payload: PassiveResult):
        try:
            return service.ingest_passive_result(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/admin-override")
    async def admin_override(payload: AdminOverride):
        try:
            return service.apply_admin_override(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown monitor: {payload.monitor_id}") from exc

    @router.post("/inventory-sync")
    async def inventory_sync(payload: InventorySyncPayload):
        return service.ingest_inventory_snapshot(payload)

    return router


def register_api_routes(fastapi_app, router: APIRouter):
    api_prefix = "/api/monitoring"
    routes = list(fastapi_app.router.routes)
    existing = [r for r in routes if getattr(r, "path", "").startswith(api_prefix)]

    if not existing:
        fastapi_app.include_router(router)
        routes = list(fastapi_app.router.routes)
        existing = [r for r in routes if getattr(r, "path", "").startswith(api_prefix)]

    if not existing:
        return

    remaining = [r for r in routes if r not in existing]
    root_idx = next(
        (i for i, r in enumerate(remaining) if getattr(r, "path", None) == ""),
        len(remaining),
    )
    fastapi_app.router.routes = remaining[:root_idx] + existing + remaining[root_idx:]
    fastapi_app.openapi_schema = None
