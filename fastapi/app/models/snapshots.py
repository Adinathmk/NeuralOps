"""
fastapi/app/models/snapshots.py

Read-only snapshot tables in DB-2.

These tables are NOT the source of truth for any data — they are
projections of data owned by Django (Service 1 / DB-1), kept in sync
via Kafka events published through the Debezium outbox pattern.

FastAPI reads from these tables on every ingest request instead of
making a synchronous HTTP call to Django, eliminating inter-service
coupling on the hot path.

Tables defined here:
  - tenant_snapshots       (from config.tenants Kafka topic)
  - alert_rule_snapshots   (from config.alert_rules Kafka topic)
  - playbook_snapshots     (from config.playbooks Kafka topic)

Phase 3 additions to TenantSnapshot:
  - github_repo_url
  - github_repo_owner
  - github_repo_name
  - github_installation_id     (GitHub App Installation ID)
  - github_default_branch
  - github_indexing_status
  - github_last_indexed_commit

Row-Level Security is enforced at the PostgreSQL layer for all tenant-
scoped tables.

Architecture reference:
  NeuralOps Technical Documentation — Sections 5, 7, 17
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database.base import Base

# ── TenantSnapshot ────────────────────────────────────────────────────────────


class TenantSnapshot(Base):
    """
    Local projection of tenant configuration owned by Django.

    Upserted by the Kafka consumer in app/queue/kafka/consumers/config_sync.py
    whenever a config.tenants event arrives.

    Phase 3 adds GitHub integration columns (all nullable — not every tenant
    will have a connected repository).  The github_installation_id column stores
    the ID used to fetch short-lived access tokens via the GitHub App.

    The `is_suspended` flag in this table is the eventual-consistent copy.
    The *authoritative* suspension check is the Redis key
    `tenant:{tenant_id}:suspended` which Django writes synchronously on
    suspend and deletes on reinstate, bypassing the Kafka propagation delay.
    """

    __tablename__ = "tenant_snapshots"

    tenant_id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        comment="Canonical tenant UUID — matches tenants.id in DB-1.",
    )
    plan_tier: Column = Column(
        String(32),
        nullable=True,
        comment="Billing plan tier: standard | professional | enterprise.",
    )
    vector_namespace: Column = Column(
        String(64),
        nullable=True,
        comment="Isolated pgvector namespace for this tenant.",
    )
    kafka_group_id: Column = Column(
        String(128),
        nullable=True,
        comment="Kafka consumer group assigned to this tenant.",
    )
    is_suspended: Column = Column(
        Boolean,
        nullable=False,
        default=False,
        comment=(
            "Eventual-consistent suspension flag. "
            "Always verify against the Redis key tenant:{id}:suspended first."
        ),
    )
    source_version: Column = Column(
        BigInteger,
        nullable=True,
        comment="Source entity version from DB-1 — used to reject stale events.",
    )
    synced_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Timestamp of the last successful snapshot upsert.",
    )

    # ── Phase 3: GitHub Integration columns ───────────────────────────────────
    # All nullable — only populated once a tenant connects a GitHub repository.

    github_repo_url: Column = Column(
        Text,
        nullable=True,
        comment="Full HTTPS clone URL of the connected repository.",
    )
    github_repo_owner: Column = Column(
        String(255),
        nullable=True,
        comment="GitHub organisation or user name that owns the repository.",
    )
    github_repo_name: Column = Column(
        String(255),
        nullable=True,
        comment="Repository name (without the owner prefix).",
    )
    github_installation_id: Column = Column(
        BigInteger,
        nullable=True,
        comment="GitHub App installation ID assigned when tenant installs the NeuralOps GitHub App.",
    )
    github_default_branch: Column = Column(
        String(255),
        nullable=True,
        comment="Branch that is indexed and monitored for push events.",
    )
    github_indexing_status: Column = Column(
        String(20),
        nullable=True,
        comment="Current AST indexing lifecycle state: pending | indexing | indexed | failed.",
    )
    github_last_indexed_commit: Column = Column(
        String(40),
        nullable=True,
        comment="SHA of the last successfully indexed commit.",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    alert_rule_snapshots = relationship(
        "AlertRuleSnapshot",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    playbook_snapshots = relationship(
        "PlaybookSnapshot",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TenantSnapshot tenant_id={self.tenant_id} "
            f"plan={self.plan_tier} suspended={self.is_suspended} "
            f"github_installation={self.github_installation_id}>"
        )


# ── AlertRuleSnapshot ─────────────────────────────────────────────────────────


class AlertRuleSnapshot(Base):
    """
    Local projection of alert rules owned by Django.

    Upserted on config.alert_rules Kafka events.
    FastAPI reads these when the AI agent decides whether to dispatch
    a notification after an incident is created.
    """

    __tablename__ = "alert_rule_snapshots"

    rule_id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    tenant_id: Column = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence_threshold: Column = Column(
        String(16),
        nullable=True,
    )
    severity_filter: Column = Column(
        ARRAY(String),
        nullable=True,
        comment="Only alert on these severity levels e.g. ['critical','high'].",
    )
    recipient_ids: Column = Column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="UUIDs of users who should receive this alert.",
    )
    enabled: Column = Column(Boolean, nullable=False, default=True)
    source_version: Column = Column(BigInteger, nullable=True)
    synced_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant = relationship("TenantSnapshot", back_populates="alert_rule_snapshots")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AlertRuleSnapshot rule_id={self.rule_id} tenant={self.tenant_id}>"


# ── PlaybookSnapshot ──────────────────────────────────────────────────────────


class PlaybookSnapshot(Base):
    """
    Local projection of runbooks owned by Django.

    Upserted on config.playbooks Kafka events.
    FastAPI's AI agent checks playbooks (via regex pattern matching)
    before initiating GPT-4 analysis to tailor the prompt context.
    """

    __tablename__ = "playbook_snapshots"

    playbook_id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    tenant_id: Column = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    error_pattern: Column = Column(
        Text,
        nullable=True,
        comment="Regex pattern matched against log error messages.",
    )
    instructions: Column = Column(
        Text,
        nullable=True,
        comment="Runbook instructions injected into the AI agent's analysis prompt.",
    )
    source_version: Column = Column(BigInteger, nullable=True)
    synced_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant = relationship("TenantSnapshot", back_populates="playbook_snapshots")

    def __repr__(self):
        return f"<PlaybookSnapshot(playbook_id={self.playbook_id}, tenant_id={self.tenant_id})>"


# ── APIKeySnapshot ────────────────────────────────────────────────────────────


class APIKeySnapshot(Base):
    """
    Local projection of API keys owned by Django.

    Upserted by the Kafka consumer in app/queue/kafka/consumers/config_sync.py
    whenever a config.api_keys event arrives.
    """

    __tablename__ = "api_key_snapshots"

    id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="Matches api_keys.id in DB-1.",
    )
    tenant_id: Column = Column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="Tenant this key belongs to.",
    )
    key: Column = Column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="The actual raw API key token.",
    )
    is_active: Column = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the key can still be used.",
    )

    def __repr__(self):
        return f"<APIKeySnapshot(id={self.id}, tenant_id={self.tenant_id}, active={self.is_active})>"


# ── Row-Level Security DDL ─────────────────────────────────────────────────────
# These DDL statements are executed *after* SQLAlchemy creates each table
# (or via Alembic migrations) to enforce tenant isolation at the database
# engine level.

_RLS_TABLES = [
    ("tenant_snapshots", "tenant_id"),
    ("alert_rule_snapshots", "tenant_id"),
    ("playbook_snapshots", "tenant_id"),
]


def _create_rls_policies(target, connection, **kwargs) -> None:
    """Emit RLS ENABLE and policy CREATE statements after table creation."""
    table_name = target.name
    tenant_col = next((col for t, col in _RLS_TABLES if t == table_name), None)
    if not tenant_col:
        return

    connection.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
    connection.execute(text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;"))

    policy_name = f"rls_{table_name}_tenant_isolation"
    connection.execute(text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};"))
    connection.execute(
        text(
            f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING (
                {tenant_col}::text
                = current_setting('app.tenant_id', true)
            );
            """
        )
    )


for _model in (TenantSnapshot, AlertRuleSnapshot, PlaybookSnapshot):
    event.listen(
        _model.__table__,
        "after_create",
        _create_rls_policies,
    )
