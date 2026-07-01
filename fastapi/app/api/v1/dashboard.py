from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies.tenant import ValidatedTenant
from app.services.log_search_repository import LogSearchRepository

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

class LogVolumeResponse(BaseModel):
    volume_24h: int


@router.get("/log-volume", response_model=LogVolumeResponse)
async def get_log_volume(
    tenant_context: ValidatedTenant,
    repo: LogSearchRepository = Depends(lambda: LogSearchRepository()),
):
    """
    Returns the total log volume for the tenant in the last 24 hours.
    Used for the dashboard metrics.
    """
    volume = await repo.count_volume(
        tenant_id=tenant_context.tenant_id,
        plan_tier=tenant_context.plan_tier,
        time_window="24h"
    )
    return LogVolumeResponse(volume_24h=volume)
