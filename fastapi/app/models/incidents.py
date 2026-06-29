"""
fastapi/app/models/incidents.py

Phase 4 DB-2 ORM models: Incident, Analysis, Alert.

All three tables enforce PostgreSQL Row-Level Security via the
TenantRLSMiddleware which sets app.tenant_id on every connection.
The DDL after_create event listeners attach the RLS policies
automatically when SQLAlchemy creates the tables (or via Alembic).

Relationships use lazy="raise" throughout to prevent accidental
N+1 loads — all joins must be explicit in query code.
"""

from __future__ import annotations

import uuid as _uuid_module
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database.base import Base

# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


class Incident(Base):
    """
    Primary incident record written by the run_agent Celery task.

    One row per unique error identity (tenant_id + fingerprint).
    Duplicate occurrences of the same error increment occurrence_count
    and append the new S3 context key to occurrences[] rather than
    creating a new row.

    The partial unique index uq_incidents_tenant_fingerprint_active
    (tenant_id, fingerprint) WHERE status != 'resolved'
    enforces at the DB layer that only one active incident per
    fingerprint per tenant can exist at any time.
    """

    __tablename__ = "incidents"

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid_module.uuid4,
        nullable=False,
        comment="Globally unique incident identifier.",
    )

    # ── Tenant isolation (FK to tenant_snapshots) ─────────────────────────────
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenant_snapshots.tenant_id",
            ondelete="CASCADE",
            name="fk_incidents_tenant_id",
        ),
        nullable=False,
        index=True,
        comment="Owning tenant UUID — enforced via RLS policy.",
    )

    # ── Fingerprint & deduplication ───────────────────────────────────────────
    fingerprint = Column(
        String(64),
        nullable=False,
        index=True,
        comment=(
            "SHA-256 hex fingerprint of the error identity. "
            "Computed from tenant_id + service_name + error_type + "
            "crash_file + (crash_line // 5 * 5) + crash_method."
        ),
    )
    occurrence_count = Column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Total number of times this fingerprint has been seen.",
    )
    occurrences = Column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default="{}",
        comment=(
            "S3 keys of all compressed context buffers for this incident. "
            "Format: logs/{tenant_id}/context/{incident_id}.json.gz"
        ),
    )

    # ── Parsed error metadata ─────────────────────────────────────────────────
    error_type = Column(
        String(255),
        nullable=False,
        default="UnknownError",
        comment="Exception or error class name, e.g. NullPointerException.",
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Full error message string from the log entry.",
    )
    service_name = Column(
        String(255),
        nullable=False,
        comment="Name of the originating service.",
    )
    environment = Column(
        String(64),
        nullable=False,
        comment="Deployment environment label, e.g. production.",
    )
    crash_file = Column(
        Text,
        nullable=True,
        comment="Relative file path of the crash location.",
    )
    crash_line = Column(
        Integer,
        nullable=True,
        comment="Line number of the crash location (raw, not normalised).",
    )
    crash_method = Column(
        String(255),
        nullable=True,
        comment="Method or function name where the crash occurred.",
    )
    stack_frames = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment=(
            "Ordered list of stack frame objects. "
            "Each element: {file, line, method, module}."
        ),
    )

    # ── AI analysis output ────────────────────────────────────────────────────
    root_cause = Column(
        Text,
        nullable=True,
        comment="GPT-4 generated root cause analysis.",
    )
    suggested_fix = Column(
        Text,
        nullable=True,
        comment="GPT-4 generated code fix suggestion.",
    )
    confidence_score = Column(
        Float,
        nullable=True,
        comment="Agent confidence score in range [0.0, 1.0].",
    )
    severity = Column(
        String(32),
        nullable=False,
        default="low",
        server_default="low",
        comment="Classified severity: critical | high | medium | low.",
    )

    # ── PR / patch fields (added for patch_generator + github_pr task) ────────
    pr_url = Column(Text, nullable=True, comment="HTML URL of the GitHub PR created by NeuralOps.")
    pr_number = Column(Integer, nullable=True, comment="GitHub PR number.")
    pr_status = Column(
        String(32),
        nullable=True,
        comment="PR lifecycle status: open | skipped | no_patch | syntax_error | failed.",
    )
    pr_title = Column(Text, nullable=True, comment="GitHub PR Title.")
    pr_error = Column(Text, nullable=True, comment="GitHub PR creation error or failure reason.")
    structured_patch = Column(
        Text,
        nullable=True,
        comment="JSON string of validated search/replace patches from PatchGeneratorNode.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status = Column(
        String(32),
        nullable=False,
        default="open",
        server_default="open",
        comment=(
            "Incident lifecycle status: "
            "open | investigating | resolved | closed | draft | duplicate."
        ),
    )
    is_draft = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "True when confidence_score < tenant threshold. "
            "Draft incidents are stored but not published to Kafka."
        ),
    )
    assigned_user_ids = Column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default="{}",
        comment="UUIDs of the assigned engineers",
    )

    # ── Source log reference ──────────────────────────────────────────────────
    source_log_id = Column(
        UUID(as_uuid=True),
        nullable=True,
        comment=(
            "incident_id from ingested_log_metadata that triggered this incident. "
            "Provides a link back to the original ingest event."
        ),
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    first_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Timestamp of the first occurrence of this fingerprint.",
    )
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Timestamp of the most recent occurrence.",
    )
    resolved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when status was set to resolved. Null if still open.",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Row creation timestamp.",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Last modification timestamp.",
    )

    # ── Table-level constraints ───────────────────────────────────────────────
    __table_args__ = (
        # NOTE: The partial unique index is created in the Alembic migration
        # and via the after_create DDL listener below, NOT as a SQLAlchemy
        # UniqueConstraint, because SQLAlchemy core does not support
        # PostgreSQL partial index WHERE clauses in __table_args__.
        Index("ix_incidents_tenant_status", "tenant_id", "status"),
        Index(
            "ix_incidents_tenant_created",
            "tenant_id",
            sa.text("created_at DESC"),
        ),
        Index(
            "ix_incidents_tenant_severity",
            "tenant_id",
            "severity",
            sa.text("created_at DESC"),
        ),
        Index(
            "ix_incidents_tenant_last_seen",
            "tenant_id",
            sa.text("last_seen_at DESC"),
        ),
        {
            "comment": "Phase 4 — AI incident records. One row per unique error identity."
        },
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    analysis = relationship(
        "Analysis",
        back_populates="incident",
        uselist=False,
        lazy="raise",
        cascade="all, delete-orphan",
    )
    alerts = relationship(
        "NotificationDelivery",
        back_populates="incident",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Incident id={self.id!s} "
            f"tenant={self.tenant_id!s} "
            f"fingerprint={str(self.fingerprint)[:8]}... "
            f"status={self.status} "
            f"severity={self.severity}>"
        )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class Analysis(Base):
    """
    Full LangGraph execution trace for a single incident.

    One-to-one with Incident. Stored in a separate table to keep
    the incidents table narrow for list queries. The node_results
    JSONB column contains per-node latency, token counts, and
    intermediate outputs as documented in the Phase 4 spec.
    """

    __tablename__ = "analyses"

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid_module.uuid4,
        nullable=False,
        comment="Globally unique analysis identifier.",
    )

    # ── Parent references ─────────────────────────────────────────────────────
    incident_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "incidents.id",
            ondelete="CASCADE",
            name="fk_analyses_incident_id",
        ),
        nullable=False,
        unique=True,
        comment="FK to incidents.id. One analysis per incident.",
    )
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenant_snapshots.tenant_id",
            ondelete="CASCADE",
            name="fk_analyses_tenant_id",
        ),
        nullable=False,
        index=True,
        comment="Denormalised tenant_id for RLS policy enforcement.",
    )

    # ── Agent versioning ──────────────────────────────────────────────────────
    agent_version = Column(
        String(32),
        nullable=False,
        default="1.0.0",
        server_default="1.0.0",
        comment="Semantic version of the LangGraph agent workflow.",
    )

    # ── Token usage ───────────────────────────────────────────────────────────
    total_tokens_used = Column(
        Integer,
        nullable=True,
        comment="Sum of prompt + completion tokens across all GPT-4 calls.",
    )
    prompt_tokens = Column(
        Integer,
        nullable=True,
        comment="Total prompt tokens across analyzer + fix_generator nodes.",
    )
    completion_tokens = Column(
        Integer,
        nullable=True,
        comment="Total completion tokens across analyzer + fix_generator nodes.",
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    total_latency_ms = Column(
        Integer,
        nullable=True,
        comment="Wall-clock ms from run_agent task start to DB commit.",
    )

    # ── Per-node execution trace ──────────────────────────────────────────────
    node_results = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment=(
            "Per-node execution metadata keyed by node name. "
            "Keys: classifier, code_retriever, playbook_matcher, "
            "analyzer, fix_generator, confidence_scorer, action_decision. "
            "Each value contains latency_ms and node-specific fields."
        ),
    )

    # ── Raw GPT-4 outputs ─────────────────────────────────────────────────────
    raw_analysis_output = Column(
        Text,
        nullable=True,
        comment="Raw JSON string returned by GPT-4 for root-cause analysis.",
    )
    raw_fix_output = Column(
        Text,
        nullable=True,
        comment="Raw JSON string returned by GPT-4 for fix generation.",
    )

    # ── Code context snapshot ─────────────────────────────────────────────────
    code_context_snapshot = Column(
        Text,
        nullable=True,
        comment=(
            "The exact code snippet text assembled by the CodeRetriever "
            "and passed to GPT-4. Stored for audit and replay."
        ),
    )

    # ── Playbook reference ────────────────────────────────────────────────────
    matched_playbook_id = Column(
        UUID(as_uuid=True),
        nullable=True,
        comment=(
            "UUID of the PlaybookSnapshot that matched this incident. "
            "NULL if no playbook matched."
        ),
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Timestamp when this analysis was persisted.",
    )

    __table_args__ = (
        Index("ix_analyses_tenant_id", "tenant_id"),
        Index("ix_analyses_incident_id", "incident_id"),
        {"comment": "Phase 4 — LangGraph agent execution traces."},
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    incident = relationship(
        "Incident",
        back_populates="analysis",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Analysis id={self.id!s} "
            f"incident_id={self.incident_id!s} "
            f"agent_version={self.agent_version} "
            f"total_tokens={self.total_tokens_used}>"
        )


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------


class NotificationDelivery(Base):
    """
    Notification dispatch record created by the action_decision node
    when confidence_score >= tenant threshold.

    One incident can have multiple alert rows (one per recipient /
    per channel). The send_notification Celery task (Phase 5) reads
    rows with status='pending' and marks them delivered or failed.
    """

    __tablename__ = "notification_deliveries"

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid_module.uuid4,
        nullable=False,
        comment="Globally unique alert identifier.",
    )

    # ── Parent references ─────────────────────────────────────────────────────
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenant_snapshots.tenant_id",
            ondelete="CASCADE",
            name="fk_notif_deliveries_tenant_id",
        ),
        nullable=False,
        index=True,
        comment="Owning tenant UUID — enforced via RLS policy.",
    )
    incident_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "incidents.id",
            ondelete="CASCADE",
            name="fk_notif_deliveries_incident_id",
        ),
        nullable=False,
        index=True,
        comment="FK to the incident that triggered this alert.",
    )
    alert_rule_id = Column(
        UUID(as_uuid=True),
        nullable=True,
        comment=(
            "FK to alert_rule_snapshots.rule_id. "
            "NULL if alert was dispatched manually."
        ),
    )

    # ── Delivery metadata ─────────────────────────────────────────────────────
    destination_type = Column(
        String(32),
        nullable=False,
        comment="Delivery type: in_app | email | pagerduty | slack.",
    )
    destination_config = Column(
        JSONB,
        nullable=False,
        comment="Snapshot of the destination config used (e.g. webhook URL or user ID).",
    )
    http_status_code = Column(
        Integer,
        nullable=True,
        comment="HTTP status code from the external delivery attempt.",
    )
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Number of delivery attempts made.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status = Column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="Delivery status: pending | delivered | failed | skipped.",
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if status=failed.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    dispatched_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the notification was successfully dispatched.",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Row creation timestamp.",
    )

    __table_args__ = (
        Index("ix_notif_deliveries_tenant_incident", "tenant_id", "incident_id"),
        Index(
            "ix_notif_deliveries_status_pending",
            "status",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        {"comment": "Phase 4 — Notification dispatch records."},
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    incident = relationship(
        "Incident",
        back_populates="alerts",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Alert id={self.id!s} "
            f"incident_id={self.incident_id!s} "
            f"channel={self.channel} "
            f"status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Row-Level Security DDL listeners
# ---------------------------------------------------------------------------
# These fire when SQLAlchemy/Alembic creates each table and attach the
# PostgreSQL RLS policies that enforce tenant isolation at the DB layer.
# The pattern mirrors the existing implementation in app/models/snapshots.py.


def _attach_rls_policy(target, connection, **kwargs) -> None:
    """
    Enable RLS and create the tenant isolation policy on the target table.

    Uses current_setting('app.tenant_id', true) — the second argument
    'true' means missing_ok, so the function returns '' rather than raising
    an error when the setting is absent (e.g. during migrations).
    This ensures unauthenticated DB connections see zero rows rather than
    raising a PostgreSQL configuration error.
    """
    table_name: str = target.name
    policy_name: str = f"rls_{table_name}_tenant_isolation"

    connection.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
    connection.execute(text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;"))
    # Idempotent: drop before create so migration reruns do not fail.
    connection.execute(text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};"))
    connection.execute(
        text(
            f"""
            CREATE POLICY {policy_name} ON {table_name}
            AS PERMISSIVE FOR ALL
            USING (
                tenant_id::text = current_setting('app.tenant_id', true)
            );
            """
        )
    )


def _attach_incident_partial_index(target, connection, **kwargs) -> None:
    """
    Create the partial unique index on incidents after the table is created.

    SQLAlchemy's Index() in __table_args__ does not support the
    PostgreSQL-specific WHERE clause required for partial indexes, so
    we create this index via a raw DDL statement in the after_create hook
    in addition to the Alembic migration (which handles the production path).
    The IF NOT EXISTS guard makes this idempotent.
    """
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_incidents_tenant_fingerprint_active
            ON incidents (tenant_id, fingerprint)
            WHERE status != 'resolved';
            """
        )
    )


# Register listeners for all three models
for _model_cls in (Incident, Analysis, NotificationDelivery):
    event.listen(
        _model_cls.__table__,
        "after_create",
        _attach_rls_policy,
    )

# Register the partial unique index listener specifically for Incident
event.listen(
    Incident.__table__,
    "after_create",
    _attach_incident_partial_index,
)
