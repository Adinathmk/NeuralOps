"""
fastapi/app/models/github_integration_snapshots.py

SQLAlchemy model for github_integration_snapshots.
This is a read-only projection of the GitHubIntegration table in Django (DB-1),
replicated via the config.tenants Kafka topic.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database.base import Base


class GitHubIntegrationSnapshot(Base):
    __tablename__ = "github_integration_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    repo_url = Column(Text, nullable=False)
    repo_owner = Column(String(255), nullable=False)
    repo_name = Column(String(255), nullable=False)
    installation_id = Column(BigInteger, nullable=True)
    default_branch = Column(String(255), nullable=False)
    
    indexing_status = Column(String(20), nullable=False, default="pending")
    last_indexed_commit = Column(String(40), nullable=True)
    
    source_version = Column(BigInteger, nullable=False, default=1)
    synced_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    tenant = relationship("TenantSnapshot", back_populates="github_integrations")


class ServiceRepoMappingSnapshot(Base):
    __tablename__ = "service_repo_mapping_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_snapshots.tenant_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    service_name = Column(String(255), nullable=False)
    repo_url = Column(Text, nullable=False)
    
    synced_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    tenant = relationship("TenantSnapshot", back_populates="service_mappings")
