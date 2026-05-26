"""
app/models/logs.py

DB-2 model: IngestedLogMetadata

Stores a lightweight metadata record for every successful log-context
ingestion event.  Full log content is NEVER stored in Postgres — only
the S3 pointer, tenant reference, and ingestion timestamp are persisted
here.  The raw compressed payload lives exclusively in S3.

This table is the anchor for:
  - Audit queries ("when was this incident's context ingested?")
  - Cleanup jobs that expire S3 objects after the retention window
  - Downstream Celery task lookup by incident_id

RLS enforcement:
  TenantRLSMiddleware sets ``app.tenant_id`` on every Postgres connection
  before this table is touched.  The RLS policy (applied via Alembic
  migration ``0002_ingest_logs_rls``) enforces tenant isolation at the
  database engine level, independently of any ORM-level filtering.

Architecture reference: NeuralOps Technical Documentation — Section 5
(DB-2 Schema), Section 17 (AI Agent Pipeline — Stage 1).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database.base import Base


class IngestedLogMetadata(Base):
    """
    Lightweight metadata record written atomically with the DB-2 outbox
    event on every successful call to ``POST /api/v1/ingest/logs``.

    Columns
    -------
    incident_id : UUID (PK)
        Client-supplied UUID identifying the crash event.  Also used as
        the Kafka message key and the S3 object key suffix.

    tenant_id : UUID (FK → tenant_snapshots.tenant_id)
        The tenant that owns this log ingestion.  RLS policy enforces
        that rows are only visible to the owning tenant's DB connection.

    service_name : str
        Name of the originating service (from the SDK payload).

    environment : str
        Deployment environment label (from the SDK payload).

    s3_path : str
        Full S3 object key of the compressed context buffer.
        Format: ``logs/{tenant_id}/context/{incident_id}.json.gz``

    created_at : datetime (UTC)
        Server-side timestamp recorded at insert time.
    """

    __tablename__ = "ingested_log_metadata"

    incident_id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        comment=(
            "Client-supplied UUID for the crash event. "
            "Used as the S3 key suffix and Kafka message key."
        ),
    )

    tenant_id: Column = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="FK to tenant_snapshots; enforced by RLS policy.",
    )

    service_name: Column = Column(
        String(255),
        nullable=False,
        comment="Name of the originating service from the SDK payload.",
    )

    environment: Column = Column(
        String(64),
        nullable=False,
        comment="Deployment environment label from the SDK payload.",
    )

    s3_path: Column = Column(
        Text,
        nullable=False,
        comment=(
            "S3 object key of the gzip-compressed context buffer. "
            "Format: logs/{tenant_id}/context/{incident_id}.json.gz"
        ),
    )

    created_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="UTC timestamp recorded by the database at insert time.",
    )

    # ── Relationship (back-reference to tenant snapshot) ──────────────────────
    tenant = relationship(
        "TenantSnapshot",
        foreign_keys=[tenant_id],
        # lazy="raise" prevents accidental N+1 loads; use explicit joins.
        lazy="raise",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<IngestedLogMetadata "
            f"incident_id={self.incident_id} "
            f"tenant_id={self.tenant_id} "
            f"service={self.service_name}>"
        )