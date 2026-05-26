"""Phase 2: Create ingested_log_metadata table with RLS policy

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-26 00:00:00 UTC

Creates the ``ingested_log_metadata`` table used by the
``POST /api/v1/ingest/logs`` endpoint to store a lightweight metadata
record (S3 pointer, tenant ref, timestamps) for every successful
context-log ingestion event.

Row-Level Security is enabled so that only connections with
``app.tenant_id`` set to the matching tenant UUID can see or modify rows.
This policy is applied at the Postgres engine level independently of any
ORM-level tenant filtering.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ingested_log_metadata ─────────────────────────────────────────────────
    op.create_table(
        "ingested_log_metadata",
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment=(
                "Client-supplied UUID for the crash event. "
                "Used as the S3 key suffix and Kafka message key."
            ),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
            nullable=False,
            comment="FK to tenant_snapshots; enforced by RLS policy.",
        ),
        sa.Column(
            "service_name",
            sa.String(255),
            nullable=False,
            comment="Name of the originating service from the SDK payload.",
        ),
        sa.Column(
            "environment",
            sa.String(64),
            nullable=False,
            comment="Deployment environment label from the SDK payload.",
        ),
        sa.Column(
            "s3_path",
            sa.Text(),
            nullable=False,
            comment=(
                "S3 object key of the gzip-compressed context buffer. "
                "Format: logs/{tenant_id}/context/{incident_id}.json.gz"
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            comment="UTC timestamp recorded by the database at insert time.",
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index(
        "ix_ingested_log_metadata_tenant_id",
        "ingested_log_metadata",
        ["tenant_id"],
    )
    op.create_index(
        "ix_ingested_log_metadata_created_at",
        "ingested_log_metadata",
        ["tenant_id", "created_at"],
    )

    # ── Row-Level Security ────────────────────────────────────────────────────
    op.execute("ALTER TABLE ingested_log_metadata ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE ingested_log_metadata FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_ingested_log_metadata_tenant_isolation
        ON ingested_log_metadata
        AS PERMISSIVE
        FOR ALL
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS rls_ingested_log_metadata_tenant_isolation "
        "ON ingested_log_metadata;"
    )
    op.drop_index("ix_ingested_log_metadata_created_at", table_name="ingested_log_metadata")
    op.drop_index("ix_ingested_log_metadata_tenant_id", table_name="ingested_log_metadata")
    op.drop_table("ingested_log_metadata")