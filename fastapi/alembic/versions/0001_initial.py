"""Phase 1: Create outbox, tenant_snapshots, alert_rule_snapshots, playbook_snapshots tables with RLS

Revision ID: 0001
Revises:
Create Date: 2026-05-22 00:00:00 UTC

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── outbox ────────────────────────────────────────────────────────────────
    op.create_table(
        "outbox",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Globally unique event identifier. Used for consumer deduplication.",
        ),
        sa.Column(
            "topic",
            sa.String(256),
            nullable=False,
            comment="Kafka topic to which Debezium will publish this row.",
        ),
        sa.Column(
            "key",
            sa.String(256),
            nullable=False,
            comment="Kafka message key (e.g. tenant_id or incident_id).",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Event payload including envelope fields.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "published",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Set to true by Debezium after Kafka delivery.",
        ),
    )
    op.create_index("ix_outbox_published", "outbox", ["published"])
    op.create_index("ix_outbox_topic", "outbox", ["topic"])
    op.create_index("ix_outbox_created_at", "outbox", ["created_at"])

    # ── tenant_snapshots ──────────────────────────────────────────────────────
    op.create_table(
        "tenant_snapshots",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Canonical tenant UUID — matches tenants.id in DB-1.",
        ),
        sa.Column("plan_tier", sa.String(32), nullable=True),
        sa.Column("vector_namespace", sa.String(64), nullable=True),
        sa.Column("kafka_group_id", sa.String(128), nullable=True),
        sa.Column(
            "is_suspended",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    # Enable RLS
    op.execute("ALTER TABLE tenant_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenant_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_tenant_snapshots_tenant_isolation ON tenant_snapshots
        USING (tenant_id::text = current_setting('app.tenant_id', true));
        """
    )

    # ── alert_rule_snapshots ──────────────────────────────────────────────────
    op.create_table(
        "alert_rule_snapshots",
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("confidence_threshold", sa.String(16), nullable=True),
        sa.Column(
            "severity_filter",
            postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
        sa.Column(
            "recipient_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("source_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE alert_rule_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alert_rule_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_alert_rule_snapshots_tenant_isolation ON alert_rule_snapshots
        USING (tenant_id::text = current_setting('app.tenant_id', true));
        """
    )

    # ── playbook_snapshots ────────────────────────────────────────────────────
    op.create_table(
        "playbook_snapshots",
        sa.Column(
            "playbook_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("error_pattern", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("source_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE playbook_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE playbook_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_playbook_snapshots_tenant_isolation ON playbook_snapshots
        USING (tenant_id::text = current_setting('app.tenant_id', true));
        """
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.execute(
        "DROP POLICY IF EXISTS rls_playbook_snapshots_tenant_isolation ON playbook_snapshots;"
    )
    op.drop_table("playbook_snapshots")

    op.execute(
        "DROP POLICY IF EXISTS rls_alert_rule_snapshots_tenant_isolation ON alert_rule_snapshots;"
    )
    op.drop_table("alert_rule_snapshots")

    op.execute(
        "DROP POLICY IF EXISTS rls_tenant_snapshots_tenant_isolation ON tenant_snapshots;"
    )
    op.drop_table("tenant_snapshots")

    op.drop_table("outbox")
