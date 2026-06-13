"""
app/schemas/incidents.py

Pydantic request and response schemas for the Phase 4 Incident REST API.

Endpoints served:
    GET    /api/v1/incidents                       → IncidentListResponse
    GET    /api/v1/incidents/{incident_id}          → IncidentDetailResponse
    PATCH  /api/v1/incidents/{incident_id}          → IncidentUpdateResponse
    GET    /api/v1/incidents/{incident_id}/context-logs → ContextLogsResponse

All response schemas use `from_attributes = True` to allow direct
construction from SQLAlchemy ORM instances via `.model_validate()`.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────────


class IncidentStatus(str, Enum):
    """Valid incident lifecycle states."""
    open = "open"
    investigating = "investigating"
    resolved = "resolved"
    draft = "draft"
    duplicate = "duplicate"


class Severity(str, Enum):
    """Valid severity levels."""
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"
    unknown = "unknown"


class SortBy(str, Enum):
    """Allowed sort fields for incident list queries."""
    created_at = "created_at"
    last_seen_at = "last_seen_at"
    confidence_score = "confidence_score"
    occurrence_count = "occurrence_count"


class SortOrder(str, Enum):
    """Sort direction."""
    asc = "asc"
    desc = "desc"


# ── Shared sub-schemas ────────────────────────────────────────────────────────


class StackFrame(BaseModel):
    """A single frame from a parsed stack trace."""
    file: str
    line: int
    method: str
    module: Optional[str] = None


# ── List endpoint ─────────────────────────────────────────────────────────────


class IncidentListItem(BaseModel):
    """Single incident row in the paginated list response."""
    id: UUID
    tenant_id: UUID
    fingerprint: str
    status: IncidentStatus
    severity: Severity
    error_type: str
    error_message: Optional[str] = None
    service_name: str
    environment: str
    crash_file: Optional[str] = None
    crash_line: Optional[int] = None
    crash_method: Optional[str] = None
    root_cause: Optional[str] = None
    suggested_fix: Optional[str] = None
    confidence_score: Optional[float] = None
    occurrence_count: int
    is_draft: bool
    assigned_user_id: Optional[UUID] = None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class PaginationMeta(BaseModel):
    """Standard pagination metadata."""
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class IncidentListResponse(BaseModel):
    """Paginated list of incidents."""
    success: bool = True
    data: List[IncidentListItem]
    pagination: PaginationMeta


# ── Detail endpoint ───────────────────────────────────────────────────────────


class AnalysisDetail(BaseModel):
    """Full analysis record associated with an incident."""
    id: UUID
    agent_version: str
    total_tokens_used: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_latency_ms: Optional[int] = None
    node_results: Dict[str, Any]
    matched_playbook_id: Optional[UUID] = None
    created_at: datetime

    class Config:
        from_attributes = True


class IncidentDetail(BaseModel):
    """Complete incident record with all fields."""
    id: UUID
    tenant_id: UUID
    fingerprint: str
    status: IncidentStatus
    severity: Severity
    error_type: str
    error_message: Optional[str] = None
    service_name: str
    environment: str
    crash_file: Optional[str] = None
    crash_line: Optional[int] = None
    crash_method: Optional[str] = None
    stack_frames: List[StackFrame]
    root_cause: Optional[str] = None
    suggested_fix: Optional[str] = None
    confidence_score: Optional[float] = None
    occurrence_count: int
    occurrences: List[str]
    is_draft: bool
    assigned_user_id: Optional[UUID] = None
    source_log_id: Optional[UUID] = None
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class IncidentDetailResponse(BaseModel):
    """Response wrapper for a single incident with its analysis."""
    success: bool = True
    data: Dict[str, Any]
    # data = {"incident": IncidentDetail, "analysis": AnalysisDetail | None}


# ── Update endpoint ──────────────────────────────────────────────────────────


class IncidentUpdateRequest(BaseModel):
    """
    Request body for PATCH /api/v1/incidents/{incident_id}.

    Both fields are optional — the caller sends only the fields to update.
    """
    status: Optional[IncidentStatus] = None
    assigned_user_id: Optional[UUID] = None

    @field_validator("status")
    @classmethod
    def validate_status_transition(cls, v):
        """Block manual draft creation via the API."""
        if v == IncidentStatus.draft:
            raise ValueError(
                "Cannot manually set status to 'draft'. "
                "Drafts are created internally by the agent pipeline."
            )
        return v


class IncidentUpdateResponse(BaseModel):
    """Response for a successful incident update."""
    success: bool = True
    message: str = "Incident updated."
    data: Dict[str, Any]
    # data = {"id": UUID, "status": str, "assigned_user_id": UUID|None, "updated_at": datetime}


# ── Context logs endpoint ─────────────────────────────────────────────────────


class ContextLogsResponse(BaseModel):
    """Response containing a pre-signed S3 URL for context log download."""
    success: bool = True
    data: Dict[str, Any]
    # data = {"incident_id": UUID, "s3_path": str, "download_url": str, "expires_at": datetime}
