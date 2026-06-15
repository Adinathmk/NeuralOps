"""
NeuralOps — Log Search FastAPI Endpoint

GET /api/v1/logs/search

All filters are optional query parameters.
tenant_id is injected by the gateway (X-Tenant-ID header) — never from the client.
plan_tier is read from tenant_snapshots — never from the client.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional
from pydantic import BaseModel, Field

from app.api.dependencies.tenant import get_validated_tenant, ValidatedTenant
from app.services.log_search_repository import (
    LogSearchFilters,
    LogSearchRequest,
    LogSearchRepository,
    LogSearchResult,
)

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


# ── RESPONSE SCHEMA ────────────────────────────────────────────────────────

class LogEventResponse(BaseModel):
    log_id: str
    incident_id: str
    service_name: str
    environment: str
    severity: str
    error_type: str
    file_path: Optional[str]
    line_number: Optional[int]
    timestamp: str
    status: str
    s3_key: str


class LogSearchResponse(BaseModel):
    results: list[LogEventResponse]
    total: int
    # Opaque cursor — client sends this back as `cursor` param for next page.
    # None means no more pages.
    next_cursor: Optional[str] = None
    took_ms: int


class FilterOptionsResponse(BaseModel):
    service_names: list[str]
    severities: list[str]
    error_types: list[str]
    environments: list[str]
    statuses: list[str]


# ── SEARCH ENDPOINT ────────────────────────────────────────────────────────

@router.get("/search", response_model=LogSearchResponse)
async def search_logs(
    # ── Dependencies ──
    tenant_context: ValidatedTenant,

    # ── Filter params ──
    severity: Optional[str] = Query(None, description="ERROR | CRITICAL"),
    service_name: Optional[str] = Query(None),
    environment: Optional[str] = Query(None),
    error_type: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="open | resolved"),

    # ── Time filters ──
    # Use time_window for preset ranges (recommended for the UI)
    # Use time_from/time_to for custom ranges (e.g. date picker)
    time_window: Optional[str] = Query(
        None,
        description="Preset window: 1h | 6h | 24h | 7d | 30d",
        pattern=r"^\d+(h|d)$",
    ),
    time_from: Optional[str] = Query(None, description="ISO 8601 timestamp"),
    time_to: Optional[str] = Query(None, description="ISO 8601 timestamp"),

    # ── Pagination ──
    page_size: int = Query(50, ge=1, le=200),
    # cursor is the search_after value from the previous response.
    # It's a JSON-serialised list — decode on receipt.
    cursor: Optional[str] = Query(None),

    # ── Repo ──
    repo: LogSearchRepository = Depends(lambda: LogSearchRepository()),
):
    """
    Search log events for the authenticated tenant.

    All filters are ANDed together.
    Results are paginated using cursor-based pagination (search_after).
    """
    # Decode the cursor from JSON if provided
    search_after = None
    if cursor:
        import json
        try:
            search_after = json.loads(cursor)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid cursor format")

    filters = LogSearchFilters(
        # tenant_id comes from the gateway-injected header via tenant_context.
        # The client never sends this — it's always server-side injected.
        tenant_id=str(tenant_context.tenant_id),
        severity=severity,
        service_name=service_name,
        environment=environment,
        error_type=error_type,
        file_path=file_path,
        status=status,
        time_window=time_window,
        time_from=time_from,
        time_to=time_to,
    )

    result: LogSearchResult = await repo.search(
        LogSearchRequest(
            filters=filters,
            page_size=page_size,
            search_after=search_after,
            plan_tier=tenant_context.plan_tier,
        )
    )

    # Serialise next_cursor to JSON string for the client
    import json
    next_cursor_str = json.dumps(result.next_search_after) if result.next_search_after else None

    return LogSearchResponse(
        results=result.hits,
        total=result.total,
        next_cursor=next_cursor_str,
        took_ms=result.took_ms,
    )


# ── FILTER OPTIONS ENDPOINT ────────────────────────────────────────────────

@router.get("/search/filters", response_model=FilterOptionsResponse)
async def get_filter_options(
    tenant_context: ValidatedTenant,
    time_window: str = Query("7d", description="Window to compute options from"),
    repo: LogSearchRepository = Depends(lambda: LogSearchRepository()),
):
    """
    Returns available filter values for the search page dropdowns.
    Call this once on page load to populate the filter UI.

    Example: which service names does this tenant have errors from in the last 7 days?
    """
    options = await repo.get_filter_options(
        tenant_id=str(tenant_context.tenant_id),
        plan_tier=tenant_context.plan_tier,
        time_window=time_window,
    )
    return FilterOptionsResponse(**options)
