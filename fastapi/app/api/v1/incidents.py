"""
app/api/v1/incidents.py

FastAPI router: Incident REST API endpoints.

Phase 4 scope:
    GET    /incidents                       — List incidents (filtered + paginated)
    GET    /incidents/{incident_id}          — Incident detail with analysis
    PATCH  /incidents/{incident_id}          — Update status / assignment
    GET    /incidents/{incident_id}/context-logs — Pre-signed S3 download URL

Authorization:
    All endpoints require a valid Bearer token.  The API gateway injects
    X-Tenant-ID and X-User-ID headers, which are resolved by the
    get_validated_tenant dependency.

Tenant isolation:
    Every DB query is scoped by tenant_id.  Row-Level Security on the
    incidents table provides a second layer of isolation at the database
    level.

Architecture reference:
    Phase 4 Technical Documentation — Section 4 (API Contracts)
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.tenant import get_validated_tenant
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_db
from app.models.incidents import Analysis, Incident
from app.models.outbox import write_outbox
from app.models.snapshots import TenantSnapshot
from app.schemas.incidents import (
    AnalysisDetail,
    ContextLogsResponse,
    IncidentDetail,
    IncidentDetailResponse,
    IncidentListItem,
    IncidentListResponse,
    IncidentStatus,
    IncidentUpdateRequest,
    IncidentUpdateResponse,
    PaginationMeta,
    Severity,
    SortBy,
    SortOrder,
    StackFrame,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/incidents", tags=["Incidents"])

_settings = get_settings()


# ── GET /incidents ────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=IncidentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List incidents",
    description=(
        "Returns a paginated, filterable list of incidents for the "
        "authenticated tenant.  Draft incidents are excluded by default."
    ),
    responses={
        200: {"description": "Paginated incident list."},
        400: {"description": "Invalid query parameter value."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Tenant is suspended."},
    },
)
async def list_incidents(
    request: Request,
    tenant: TenantSnapshot = Depends(get_validated_tenant),
    db: AsyncSession = Depends(get_db),
    # ── Query parameters ──────────────────────────────────────────────────
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status: open, investigating, resolved, draft",
    ),
    severity: Optional[str] = Query(
        None,
        description="Comma-separated severity filter: critical,high,medium,low,info",
    ),
    service_name: Optional[str] = Query(
        None,
        description="Exact match on service name.",
    ),
    environment: Optional[str] = Query(
        None,
        description="Exact match on environment.",
    ),
    assigned_user_id: Optional[uuid.UUID] = Query(
        None,
        description="Filter incidents assigned to this user.",
    ),
    search: Optional[str] = Query(
        None,
        description="Search by error type or file path.",
    ),
    is_draft: bool = Query(
        False,
        description="Include draft incidents. Defaults to false.",
    ),
    page: int = Query(
        1,
        ge=1,
        description="1-based page number.",
    ),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Results per page. Max 100.",
    ),
    sort_by: SortBy = Query(
        SortBy.last_seen_at,
        description="Sort field.",
    ),
    sort_order: SortOrder = Query(
        SortOrder.desc,
        description="Sort direction.",
    ),
) -> IncidentListResponse:
    tenant_id = tenant.tenant_id

    logger.debug(
        "list_incidents_start",
        tenant_id=str(tenant_id),
        page=page,
        page_size=page_size,
        sort_by=sort_by.value,
    )

    # ── Build base query ──────────────────────────────────────────────────
    base_filter = [Incident.tenant_id == tenant_id]

    # Status filter
    if status_filter:
        valid_statuses = {s.value for s in IncidentStatus}
        requested = [s.strip() for s in status_filter.split(",")]
        invalid = [s for s in requested if s not in valid_statuses]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid status value(s): {', '.join(invalid)}. "
                    f"Allowed: {', '.join(sorted(valid_statuses))}"
                ),
            )
        base_filter.append(Incident.status.in_(requested))
    elif not is_draft:
        # Exclude drafts by default unless explicitly requested
        base_filter.append(Incident.status != "draft")

    # Severity filter
    if severity:
        valid_severities = {s.value for s in Severity}
        requested_sev = [s.strip() for s in severity.split(",")]
        invalid_sev = [s for s in requested_sev if s not in valid_severities]
        if invalid_sev:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid severity value(s): {', '.join(invalid_sev)}. "
                    f"Allowed: {', '.join(sorted(valid_severities))}"
                ),
            )
        base_filter.append(Incident.severity.in_(requested_sev))

    # Service name filter
    if service_name:
        base_filter.append(Incident.service_name == service_name)

    # Environment filter
    if environment:
        base_filter.append(Incident.environment == environment)

    # Assigned User filter
    if assigned_user_id:
        base_filter.append(Incident.assigned_user_ids.any(assigned_user_id))

    # Search filter
    if search:
        search_term = f"%{search}%"
        base_filter.append(
            or_(
                Incident.error_type.ilike(search_term),
                Incident.crash_file.ilike(search_term),
            )
        )

    # ── Count total matching rows ─────────────────────────────────────────
    count_stmt = select(func.count()).select_from(Incident).where(*base_filter)
    total_result = await db.execute(count_stmt)
    total: int = total_result.scalar_one()

    total_pages = max(1, math.ceil(total / page_size))

    # ── Build sorted + paginated query ────────────────────────────────────
    sort_column = getattr(Incident, sort_by.value)
    if sort_order == SortOrder.desc:
        sort_column = sort_column.desc()
    else:
        sort_column = sort_column.asc()

    offset = (page - 1) * page_size

    query = (
        select(Incident)
        .where(*base_filter)
        .order_by(sort_column)
        .offset(offset)
        .limit(page_size)
    )

    result = await db.execute(query)
    incidents: List[Incident] = list(result.scalars().all())

    # ── Serialise ─────────────────────────────────────────────────────────
    items = [IncidentListItem.model_validate(inc) for inc in incidents]

    pagination = PaginationMeta(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )

    logger.debug(
        "list_incidents_success",
        tenant_id=str(tenant_id),
        total=total,
        returned=len(items),
    )

    return IncidentListResponse(
        success=True,
        data=items,
        pagination=pagination,
    )


# ── GET /incidents/{incident_id} ──────────────────────────────────────────────


@router.get(
    "/{incident_id}",
    response_model=IncidentDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get incident detail",
    description=(
        "Returns a single incident with its full analysis record. "
        "The analysis includes per-node execution metadata and token usage."
    ),
    responses={
        200: {"description": "Incident with analysis detail."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Tenant is suspended."},
        404: {"description": "Incident not found."},
    },
)
async def get_incident(
    incident_id: uuid.UUID,
    request: Request,
    tenant: TenantSnapshot = Depends(get_validated_tenant),
    db: AsyncSession = Depends(get_db),
) -> IncidentDetailResponse:
    tenant_id = tenant.tenant_id

    # Eagerly load the analysis relationship in the same query
    stmt = (
        select(Incident)
        .options(selectinload(Incident.analysis))
        .where(
            Incident.tenant_id == tenant_id,
            or_(
                Incident.id == incident_id,
                Incident.source_log_id == incident_id,
                Incident.occurrences.contains(
                    [f"logs/{tenant_id}/context/{incident_id}.json.gz"]
                ),
            ),
        )
    )

    result = await db.execute(stmt)
    incident: Optional[Incident] = result.scalar_one_or_none()

    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found.",
        )

    # Serialise incident
    incident_data = IncidentDetail.model_validate(incident).model_dump(mode="json")

    # Serialise analysis (may be None for very early incidents)
    analysis_data = None
    if incident.analysis is not None:
        analysis_data = AnalysisDetail.model_validate(incident.analysis).model_dump(
            mode="json"
        )

    logger.debug(
        "get_incident_success",
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        has_analysis=analysis_data is not None,
    )

    return IncidentDetailResponse(
        success=True,
        data={
            "incident": incident_data,
            "analysis": analysis_data,
        },
    )


# ── PATCH /incidents/{incident_id} ────────────────────────────────────────────


@router.patch(
    "/{incident_id}",
    response_model=IncidentUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Update incident",
    description=(
        "Update an incident's status or assignment.  Publishes an "
        "incidents.updated outbox event on successful write."
    ),
    responses={
        200: {"description": "Incident updated successfully."},
        400: {"description": "Invalid status transition."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Tenant is suspended."},
        404: {"description": "Incident not found."},
    },
)
async def update_incident(
    incident_id: uuid.UUID,
    payload: IncidentUpdateRequest,
    request: Request,
    tenant: TenantSnapshot = Depends(get_validated_tenant),
    db: AsyncSession = Depends(get_db),
) -> IncidentUpdateResponse:
    tenant_id = tenant.tenant_id

    # ── Step 1: Fetch current incident ────────────────────────────────────
    stmt = select(Incident).where(
        Incident.tenant_id == tenant_id,
        or_(
            Incident.id == incident_id,
            Incident.source_log_id == incident_id,
            Incident.occurrences.contains(
                [f"logs/{tenant_id}/context/{incident_id}.json.gz"]
            ),
        ),
    )
    result = await db.execute(stmt)
    incident: Optional[Incident] = result.scalar_one_or_none()

    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found.",
        )

    # ── Step 2: Validate status transition ────────────────────────────────
    current_status = incident.status
    if payload.status is not None:

        # Draft incidents cannot be moved to investigating/resolved directly
        if current_status == "draft" and payload.status.value in (
            "investigating",
            "resolved",
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot transition a draft incident to "
                    f"'{payload.status.value}'. Drafts must first be "
                    "promoted to 'open' via internal reprocessing."
                ),
            )

        # Enforce strict state machine transitions
        allowed_transitions = {
            "open": ["investigating"],
            "investigating": ["resolved"],
            "resolved": ["closed"],
            "closed": ["open", "investigating", "resolved"],
        }

        new_status_val = payload.status.value
        if (
            current_status in allowed_transitions
            and new_status_val not in allowed_transitions[current_status]
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition from '{current_status}' to '{new_status_val}'.",
            )

    # ── Step 3: Build update values ───────────────────────────────────────
    now = datetime.now(timezone.utc)
    update_values: Dict[str, Any] = {"updated_at": now}

    new_status = incident.status
    if payload.status is not None:
        update_values["status"] = payload.status.value
        new_status = payload.status.value

        # Auto-set resolved_at when transitioning to resolved
        if payload.status == IncidentStatus.resolved:
            update_values["resolved_at"] = now

    new_assigned = (
        list(incident.assigned_user_ids) if incident.assigned_user_ids else []
    )
    if payload.assigned_user_ids is not None:
        update_values["assigned_user_ids"] = payload.assigned_user_ids
        new_assigned = payload.assigned_user_ids

    if (
        payload.status == IncidentStatus.investigating
        and current_status == "open"
        and payload.actor_id is not None
        and payload.actor_id not in new_assigned
    ):
        # Auto-assign the actor when acknowledging the incident
        new_assigned.append(payload.actor_id)
        update_values["assigned_user_ids"] = new_assigned

    # Check if a field named assigned_user_ids was explicitly set to null
    raw_body = await request.body()
    if (
        b'"assigned_user_ids": null' in raw_body
        or b'"assigned_user_ids":null' in raw_body
    ):
        update_values["assigned_user_ids"] = []
        new_assigned = []

    # Enforce unassignment restrictions
    if payload.actor_id and incident.assigned_user_ids:
        # Rule 1: Cannot leave 0 assignees if you are unassigning yourself
        was_assigned = payload.actor_id in incident.assigned_user_ids
        is_assigned_now = payload.actor_id in new_assigned

        if was_assigned and not is_assigned_now and len(new_assigned) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot unassign yourself unless there is at least one other assigned engineer.",
            )

        # Rule 2: Cannot unassign other users
        for uid in incident.assigned_user_ids:
            if uid != payload.actor_id and uid not in new_assigned:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to unassign other responders.",
                )

    # Calculate newly assigned users to trigger notifications downstream (BEFORE update)
    newly_assigned_user_ids = []
    old_assigned = (
        set(incident.assigned_user_ids) if incident.assigned_user_ids else set()
    )
    for uid in new_assigned:
        if uid not in old_assigned:
            newly_assigned_user_ids.append(str(uid))

    # ── Step 4: Execute update ────────────────────────────────────────────
    update_stmt = (
        update(Incident).where(Incident.id == incident.id).values(**update_values)
    )
    await db.execute(update_stmt)

    # ── Step 5: Write outbox event (incidents.updated) ────────────────────
    outbox_event_id = uuid.uuid4()
    write_outbox(
        session=db,
        topic="incidents.updated",
        key=str(tenant_id),
        payload={
            "event_id": str(outbox_event_id),
            "event_type": "incident.updated",
            "version": 1,
            "idempotency_key": (
                f"tenant:{tenant_id}:incident:{incident.id}:" f"v{now.timestamp():.0f}"
            ),
            "source_version": 2,
            "occurred_at": now.isoformat(),
            "payload": {
                "incident_id": str(incident.id),
                "tenant_id": str(tenant_id),
                "status": new_status,
                "from_status": current_status,
                "assigned_user_ids": (
                    [str(uid) for uid in new_assigned] if new_assigned else []
                ),
                "newly_assigned_user_ids": newly_assigned_user_ids,
                "actor_id": str(payload.actor_id) if payload.actor_id else None,
                "note": payload.note,
                "updated_at": now.isoformat(),
            },
        },
    )

    logger.info(
        "incident_updated",
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        new_status=new_status,
        assigned_user_ids=[str(uid) for uid in new_assigned] if new_assigned else [],
    )

    # ── Step 6: Update Elasticsearch status (non-fatal) ───────────────────
    if payload.status is not None:
        try:
            from app.services.log_event_indexer import LogEventIndexer

            indexer = LogEventIndexer()
            # We don't wrap this in circuit_breaker because update_by_query
            # takes longer and we don't want to trip the global ingest circuit
            await indexer.update_incident_status(
                incident_id=str(incident.id),
                tenant_id=str(tenant_id),
                plan_tier=tenant.plan_tier,
                new_status=new_status,
            )
        except Exception as exc:
            logger.warning(
                "es_status_update_failed", incident_id=str(incident_id), error=str(exc)
            )

    return IncidentUpdateResponse(
        success=True,
        message="Incident updated.",
        data={
            "id": str(incident.id),
            "status": new_status,
            "assigned_user_ids": (
                [str(uid) for uid in new_assigned] if new_assigned else []
            ),
            "updated_at": now.isoformat(),
        },
    )


# ── GET /incidents/{incident_id}/context-logs ─────────────────────────────────


@router.get(
    "/{incident_id}/context-logs",
    response_model=ContextLogsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get context log download URL",
    description=(
        "Returns a pre-signed S3 URL for downloading the compressed "
        "context log buffer for the original incident trigger."
    ),
    responses={
        200: {"description": "Pre-signed download URL."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Tenant is suspended."},
        404: {"description": "Incident not found."},
        502: {"description": "S3 error generating pre-signed URL."},
    },
)
async def get_context_logs(
    incident_id: uuid.UUID,
    request: Request,
    tenant: TenantSnapshot = Depends(get_validated_tenant),
    db: AsyncSession = Depends(get_db),
) -> ContextLogsResponse:
    tenant_id = tenant.tenant_id

    # ── Step 1: Fetch incident to get the first S3 path ───────────────────
    stmt = select(Incident.occurrences).where(
        Incident.tenant_id == tenant_id,
        or_(
            Incident.id == incident_id,
            Incident.source_log_id == incident_id,
            Incident.occurrences.contains(
                [f"logs/{tenant_id}/context/{incident_id}.json.gz"]
            ),
        ),
    )
    result = await db.execute(stmt)
    row = result.one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found.",
        )

    occurrences: List[str] = row[0] or []
    if not occurrences:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No context logs found for this incident.",
        )

    # Use the first occurrence (original trigger)
    s3_path = occurrences[0]

    # ── Step 2: Generate pre-signed URL ───────────────────────────────────
    expiry_seconds = _settings.AWS_S3_SIGNED_URL_EXPIRY  # default: 900 (15 min)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)

    session = aioboto3.Session(
        aws_access_key_id=_settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_settings.AWS_SECRET_ACCESS_KEY,
        region_name=_settings.AWS_REGION_NAME,
    )

    try:
        async with session.client(
            "s3", endpoint_url=_settings.AWS_S3_ENDPOINT_URL
        ) as s3_client:
            download_url = await s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": _settings.AWS_S3_BUCKET_NAME,
                    "Key": s3_path,
                },
                ExpiresIn=expiry_seconds,
            )

            # Rewrite internal docker hostname to localhost for the browser
            if "http://minio:9000" in download_url:
                download_url = download_url.replace(
                    "http://minio:9000", "http://localhost:9000"
                )
    except (ClientError, BotoCoreError) as exc:
        logger.error(
            "context_logs_presign_error",
            tenant_id=str(tenant_id),
            incident_id=str(incident_id),
            s3_path=s3_path,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to generate context log download URL.",
        ) from exc

    logger.debug(
        "context_logs_url_generated",
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        s3_path=s3_path,
        expires_at=expires_at.isoformat(),
    )

    return ContextLogsResponse(
        success=True,
        data={
            "incident_id": str(incident_id),
            "s3_path": s3_path,
            "download_url": download_url,
            "expires_at": expires_at.isoformat(),
        },
    )
