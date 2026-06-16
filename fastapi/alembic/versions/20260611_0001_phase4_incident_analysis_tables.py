"""Phase 4: Create incidents, analyses, and alerts tables with RLS and indexes.

Revision ID: b7c3f2d9a1e4
Revises: a51be1689124
Create Date: 2026-06-11 00:00:00 UTC

Creates three new tables in DB-2 (FastAPI-owned PostgreSQL):
  - incidents       : primary incident records with deduplication
  - analyses        : LangGraph agent execution traces (1-to-1 with incidents)
  - alerts          : notification dispatch records

Each table has:
  - PostgreSQL Row-Level Security (RLS) enabled and forced
  - A tenant isolation policy using current_setting('app.tenant_id', true)
  - Appropriate B-tree indexes for the expected query patterns

The incidents table additionally has a partial unique index on
(tenant_id, fingerprint) WHERE status != 'resolved'
which enforces the DB-layer deduplication guarantee.

Downgrade drops all policies, indexes, and tables in reverse dependency order.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "b7c3f2d9a1e4"
down_revision: Union[str, None] = "a51be1689124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # =========================================================================
    # TABLE: incidents
    # =========================================================================
    op.create_table(
        "incidents",
        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="Globally unique incident identifier.",
        ),
        # ── Tenant isolation ──────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenant_snapshots.tenant_id",
                ondelete="CASCADE",
                name="fk_incidents_tenant_id",
            ),
            nullable=False,
            comment="Owning tenant UUID — enforced via RLS policy.",
        ),
        # ── Fingerprint & deduplication ───────────────────────────────────────
        sa.Column(
            "fingerprint",
            sa.String(64),
            nullable=False,
            comment=(
                "SHA-256 hex fingerprint of the error identity. "
                "Computed from tenant_id + service_name + error_type + "
                "crash_file + (crash_line // 5 * 5) + crash_method."
            ),
        ),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Total number of times this fingerprint has been observed.",
        ),
        sa.Column(
            "occurrences",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
            comment=(
                "S3 object keys for all context buffers belonging to this incident. "
                "Format: logs/{tenant_id}/context/{incident_id}.json.gz"
            ),
        ),
        # ── Parsed error metadata ─────────────────────────────────────────────
        sa.Column(
            "error_type",
            sa.String(255),
            nullable=False,
            server_default="UnknownError",
            comment="Exception or error class name, e.g. NullPointerException.",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Full error message string from the triggering log entry.",
        ),
        sa.Column(
            "service_name",
            sa.String(255),
            nullable=False,
            comment="Name of the originating service.",
        ),
        sa.Column(
            "environment",
            sa.String(64),
            nullable=False,
            comment="Deployment environment label, e.g. production.",
        ),
        sa.Column(
            "crash_file",
            sa.Text(),
            nullable=True,
            comment="Relative file path of the crash location.",
        ),
        sa.Column(
            "crash_line",
            sa.Integer(),
            nullable=True,
            comment="Line number of the crash location (raw, not normalised).",
        ),
        sa.Column(
            "crash_method",
            sa.String(255),
            nullable=True,
            comment="Method or function name where the crash occurred.",
        ),
        sa.Column(
            "stack_frames",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
            comment=(
                "Ordered list of stack frame objects. "
                "Each element: {file, line, method, module}."
            ),
        ),
        # ── AI analysis output ────────────────────────────────────────────────
        sa.Column(
            "root_cause",
            sa.Text(),
            nullable=True,
            comment="GPT-4 generated root cause analysis.",
        ),
        sa.Column(
            "suggested_fix",
            sa.Text(),
            nullable=True,
            comment="GPT-4 generated code fix suggestion.",
        ),
        sa.Column(
            "confidence_score",
            sa.Float(),
            nullable=True,
            comment="Agent confidence score in range [0.0, 1.0].",
        ),
        sa.Column(
            "severity",
            sa.String(32),
            nullable=False,
            server_default="unknown",
            comment="Classified severity: critical | high | medium | low | info | unknown.",
        ),
        # ── Lifecycle ─────────────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="open",
            comment=(
                "Incident lifecycle status: "
                "open | investigating | resolved | draft | duplicate."
            ),
        ),
        sa.Column(
            "is_draft",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment=(
                "True when confidence_score is below the tenant threshold. "
                "Draft incidents are stored but not published to Kafka."
            ),
        ),
        sa.Column(
            "assigned_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="UUID of the assigned engineer (references users in DB-1).",
        ),
        sa.Column(
            "source_log_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "incident_id from ingested_log_metadata that triggered this incident. "
                "Provides traceability back to the original ingest event."
            ),
        ),
        # ── Timestamps ────────────────────────────────────────────────────────
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Timestamp of the first occurrence of this fingerprint.",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Timestamp of the most recent occurrence.",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when status transitioned to resolved. NULL if open.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Row creation timestamp (server-side).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Last modification timestamp (server-side).",
        ),
    )

    # ── incidents: standard B-tree indexes ───────────────────────────────────
    op.create_index(
        "ix_incidents_tenant_id",
        "incidents",
        ["tenant_id"],
    )
    op.create_index(
        "ix_incidents_fingerprint",
        "incidents",
        ["fingerprint"],
    )
    op.create_index(
        "ix_incidents_tenant_status",
        "incidents",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_incidents_tenant_severity",
        "incidents",
        ["tenant_id", "severity"],
    )
    op.create_index(
        "ix_incidents_tenant_created",
        "incidents",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_incidents_tenant_last_seen",
        "incidents",
        ["tenant_id", sa.text("last_seen_at DESC")],
    )

    # ── incidents: PARTIAL unique index (deduplication guard) ─────────────────
    # This is the DB-layer guarantee that only one active incident per
    # fingerprint per tenant can exist at any time.
    # SQLAlchemy's create_index does not support WHERE clauses on all
    # backends, so we use raw SQL via op.execute for full control.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_incidents_tenant_fingerprint_active
        ON incidents (tenant_id, fingerprint)
        WHERE status != 'resolved';
        """
    )

    # ── incidents: Row-Level Security ─────────────────────────────────────────
    op.execute("ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE incidents FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_incidents_tenant_isolation ON incidents
        AS PERMISSIVE FOR ALL
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
        );
        """
    )

    # =========================================================================
    # TABLE: analyses
    # =========================================================================
    op.create_table(
        "analyses",
        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="Globally unique analysis identifier.",
        ),
        # ── Parent references ─────────────────────────────────────────────────
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "incidents.id",
                ondelete="CASCADE",
                name="fk_analyses_incident_id",
            ),
            nullable=False,
            unique=True,
            comment="FK to incidents.id. One analysis per incident (1-to-1).",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenant_snapshots.tenant_id",
                ondelete="CASCADE",
                name="fk_analyses_tenant_id",
            ),
            nullable=False,
            comment="Denormalised tenant_id for RLS policy enforcement.",
        ),
        # ── Agent versioning ──────────────────────────────────────────────────
        sa.Column(
            "agent_version",
            sa.String(32),
            nullable=False,
            server_default="1.0.0",
            comment="Semantic version of the LangGraph agent workflow.",
        ),
        # ── Token usage ───────────────────────────────────────────────────────
        sa.Column(
            "total_tokens_used",
            sa.Integer(),
            nullable=True,
            comment="Sum of prompt + completion tokens across all GPT-4 calls.",
        ),
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            nullable=True,
            comment="Total prompt tokens across analyzer + fix_generator nodes.",
        ),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=True,
            comment="Total completion tokens across analyzer + fix_generator nodes.",
        ),
        # ── Timing ────────────────────────────────────────────────────────────
        sa.Column(
            "total_latency_ms",
            sa.Integer(),
            nullable=True,
            comment="Wall-clock milliseconds from run_agent task start to DB commit.",
        ),
        # ── Per-node execution trace ──────────────────────────────────────────
        sa.Column(
            "node_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
            comment=(
                "Per-node execution metadata keyed by node name. "
                "Keys: classifier, code_retriever, playbook_matcher, "
                "analyzer, fix_generator, confidence_scorer, action_decision."
            ),
        ),
        # ── Raw GPT-4 outputs ─────────────────────────────────────────────────
        sa.Column(
            "raw_analysis_output",
            sa.Text(),
            nullable=True,
            comment="Raw JSON string returned by GPT-4 for root-cause analysis.",
        ),
        sa.Column(
            "raw_fix_output",
            sa.Text(),
            nullable=True,
            comment="Raw JSON string returned by GPT-4 for fix generation.",
        ),
        # ── Code context snapshot ─────────────────────────────────────────────
        sa.Column(
            "code_context_snapshot",
            sa.Text(),
            nullable=True,
            comment=(
                "The exact code snippet text assembled by the CodeRetriever "
                "and passed to GPT-4. Stored for audit and analysis replay."
            ),
        ),
        # ── Playbook reference ────────────────────────────────────────────────
        sa.Column(
            "matched_playbook_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "UUID of the PlaybookSnapshot that matched this incident. "
                "NULL if no playbook pattern matched."
            ),
        ),
        # ── Timestamp ─────────────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Timestamp when this analysis record was persisted.",
        ),
    )

    # ── analyses: indexes ─────────────────────────────────────────────────────
    op.create_index(
        "ix_analyses_tenant_id",
        "analyses",
        ["tenant_id"],
    )
    op.create_index(
        "ix_analyses_incident_id",
        "analyses",
        ["incident_id"],
    )

    # ── analyses: Row-Level Security ──────────────────────────────────────────
    op.execute("ALTER TABLE analyses ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE analyses FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_analyses_tenant_isolation ON analyses
        AS PERMISSIVE FOR ALL
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
        );
        """
    )

    # =========================================================================
    # TABLE: alerts
    # =========================================================================
    op.create_table(
        "alerts",
        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="Globally unique alert identifier.",
        ),
        # ── Parent references ─────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenant_snapshots.tenant_id",
                ondelete="CASCADE",
                name="fk_alerts_tenant_id",
            ),
            nullable=False,
            comment="Owning tenant UUID — enforced via RLS policy.",
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "incidents.id",
                ondelete="CASCADE",
                name="fk_alerts_incident_id",
            ),
            nullable=False,
            comment="FK to the incident that triggered this alert.",
        ),
        sa.Column(
            "alert_rule_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "UUID of the alert_rule_snapshot that triggered this alert. "
                "NULL if the alert was dispatched manually."
            ),
        ),
        # ── Delivery metadata ─────────────────────────────────────────────────
        sa.Column(
            "channel",
            sa.String(32),
            nullable=False,
            comment="Delivery channel: in_app | email | webhook.",
        ),
        sa.Column(
            "recipient_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="UUID of the recipient user.",
        ),
        sa.Column(
            "destination",
            sa.String(512),
            nullable=True,
            comment="Email address or webhook URL for the delivery target.",
        ),
        # ── Status ────────────────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
            comment="Delivery status: pending | delivered | failed | skipped.",
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment="Error message populated when status=failed.",
        ),
        # ── Timestamps ────────────────────────────────────────────────────────
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when the notification was successfully dispatched.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Row creation timestamp.",
        ),
    )

    # ── alerts: indexes ───────────────────────────────────────────────────────
    op.create_index(
        "ix_alerts_tenant_id",
        "alerts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_alerts_incident_id",
        "alerts",
        ["incident_id"],
    )
    op.create_index(
        "ix_alerts_tenant_incident",
        "alerts",
        ["tenant_id", "incident_id"],
    )
    # Partial index: fast scan for pending alerts by the send_notification task
    op.execute(
        """
        CREATE INDEX ix_alerts_status_pending
        ON alerts (status, created_at)
        WHERE status = 'pending';
        """
    )

    # ── alerts: Row-Level Security ────────────────────────────────────────────
    op.execute("ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alerts FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY rls_alerts_tenant_isolation ON alerts
        AS PERMISSIVE FOR ALL
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
        );
        """
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse FK dependency order:
    # alerts → analyses → incidents
    # (alerts and analyses both FK to incidents)

    # ── Drop alerts ───────────────────────────────────────────────────────────
    op.execute("DROP POLICY IF EXISTS rls_alerts_tenant_isolation ON alerts;")
    op.drop_index("ix_alerts_status_pending", table_name="alerts")
    op.drop_index("ix_alerts_tenant_incident", table_name="alerts")
    op.drop_index("ix_alerts_incident_id", table_name="alerts")
    op.drop_index("ix_alerts_tenant_id", table_name="alerts")
    op.drop_table("alerts")

    # ── Drop analyses ─────────────────────────────────────────────────────────
    op.execute("DROP POLICY IF EXISTS rls_analyses_tenant_isolation ON analyses;")
    op.drop_index("ix_analyses_incident_id", table_name="analyses")
    op.drop_index("ix_analyses_tenant_id", table_name="analyses")
    op.drop_table("analyses")

    # ── Drop incidents ────────────────────────────────────────────────────────
    op.execute("DROP POLICY IF EXISTS rls_incidents_tenant_isolation ON incidents;")
    op.drop_index(
        "uq_incidents_tenant_fingerprint_active",
        table_name="incidents",
    )
    op.drop_index("ix_incidents_tenant_last_seen", table_name="incidents")
    op.drop_index("ix_incidents_tenant_created", table_name="incidents")
    op.drop_index("ix_incidents_tenant_severity", table_name="incidents")
    op.drop_index("ix_incidents_tenant_status", table_name="incidents")
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.drop_index("ix_incidents_tenant_id", table_name="incidents")
    op.drop_table("incidents")
